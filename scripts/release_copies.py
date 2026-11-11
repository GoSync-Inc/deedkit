"""Database copies of the deploy tool (ANYM-588, production.md "Database copies"), moved verbatim from production.py
(W5-1c). A tool file (TOOL_FILES): production.py loads it from its own directory and sets `p` to a view of its names
(ToolNames) before this code runs, so every name of production.py is read through `p` at call time, and production.py
imports every name defined here. `production.<name>` stays the one name callers use and tests patch."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import selectors
import shutil
import stat
import subprocess
import time

p = globals().get("p")  # production.py's names, set by its tool_module() before this module runs
if p is None:
    raise ImportError("release_copies.py is loaded by production.py (tool_module), never imported on its own")


# Database copies (ANYM-588, production.md "Database copies"): an exactly checked copy of a live database on this host,
# restored in a capped scratch PostgreSQL. No helper's output is kept or printed (stderr is discarded): a failure is one
# fixed code. `<id>/owner.json` names every resource before it starts; only `released.json` finishes them.
COPIES = "migration-copies"
COPY_ROOTS = "copy-roots.json"
COPY_RETENTION = 48 * 3600
COPY_POLL = 2
COPY_RESERVE = 10 << 30
COPY_TIMEOUT = 1800
COPY_OUTPUT = 16 << 20
COPY_STOP_READBACK = 30
COPY_DATA = "/deedkit-copy"
# The scratch database the reference schema alone is restored into when the restore prints its expressions otherwise.
SCHEMA_ROUND_TRIP = "deedkit_copy_reference"
COPY_CODES = ("dump_failed", "restore_failed", "count_mismatch", "schema_mismatch", "disk_reserve", "deadline")
COPY_NAME = re.compile(r"[a-z][a-z0-9]{0,11}")
COPY_ID = re.compile(r"[0-9a-f]{12}-[a-z][a-z0-9]{0,11}-([0-9]{8}T[0-9]{6}Z)")
SQL_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
DOCKER_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
SNAPSHOT_ID = re.compile(r"[0-9A-F]+-[0-9A-F]+-[0-9]+")
LOCALE = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
# Every table outside the system schemas, one JSON line each: the same text counts the live snapshot and the restore.
COUNTS = r"""SELECT format('SELECT json_build_object(''table'', %L, ''rows'', count(*)) FROM %s', t, t)
  FROM (SELECT format('%I.%I', n.nspname, c.relname) AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)') AS tables ORDER BY t \gexec"""
INSPECT_COPY = ('{{json .Image}}\n{{json .Mounts}}\n'
                '{{range .Config.Env}}{{if eq (index (split . "=") 0) "PGDATA"}}{{json .}}{{end}}{{end}}')
copy_clock = time.monotonic


class CopyFailed(p.Refused):
    """A database copy ended with one of COPY_CODES; nothing a helper printed is in it."""

    def __init__(self, copy_id, code):
        super().__init__(f"database copy {copy_id} failed: {code}")
        self.code = code


class CopyStuck(p.Refused):
    """A resource of a database copy could not be confirmed gone: `deploy` pauses."""

    def __init__(self, copy_id):
        super().__init__(f"a database copy could not be stopped: {copy_id}")


def settings_digest(settings):
    """The plan A configuration digest receipts record. The `migration` section (ANYM-588 C7) and the `backup` section
    (ANYM-744) are not in it: adding either changes no prepared release."""
    return p.digest({key: value for key, value in settings.items()
                   if key not in ("migration", "backup", "session_window", "session_timezone")})


def copy_source(settings, name):
    """`migration.databases.<name>` and the copy deadline, or a refusal: without the section nothing is copied."""
    section = settings.get("migration")
    if not isinstance(section, dict) or not isinstance(section.get("databases"), dict):
        raise p.Refused("the production configuration has no migration section; no database is copied")
    source = section["databases"].get(name) if COPY_NAME.fullmatch(name or "") else None
    if (not isinstance(source, dict) or set(source) != {"container", "owner", "database"}
            or not all(isinstance(value, str) for value in source.values())
            or not DOCKER_NAME.fullmatch(source["container"])
            or not all(SQL_NAME.fullmatch(source[key]) for key in ("owner", "database"))):
        raise p.Refused("migration.databases must name the database's container, owner and database")
    timeout = section.get("copy_timeout_s", COPY_TIMEOUT)
    if type(timeout) is not int or not 60 <= timeout <= 86400:
        raise p.Refused("migration.copy_timeout_s must be 60 to 86400 seconds")
    return dict(source), timeout


def filesystem(path):
    """(free bytes, size bytes) of the filesystem holding `path`."""
    found = os.statvfs(path)
    return found.f_bavail * found.f_frsize, found.f_blocks * found.f_frsize


def copy_reserve(size):
    return max(COPY_RESERVE, size // 10)


def new_file(path):
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def write_record(path, value):
    """`value` as JSON, fsynced with its directory; an earlier record of the name is replaced in one rename."""
    temporary = path.with_name(path.name + ".tmp")
    with suppress(FileNotFoundError):
        temporary.unlink()
    with os.fdopen(new_file(temporary), "w") as handle:
        handle.write(json.dumps(value, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    p.fsync_directory(path.parent)


def device(path):
    """The filesystem `path` is on (st_dev)."""
    return os.stat(path).st_dev


def trusted_parents(root, state):
    """A configured root's parents: each a real directory (no symlink) owned by root or this user and writable by no
    one else unless sticky, and the last one on another filesystem than the state's, so an unmounted volume whose
    mount point is still there never takes a copy onto the state's disk (Astra r1 on #807)."""
    for parent in reversed(root.parents):
        try:
            found = os.lstat(parent)
        except FileNotFoundError as exc:
            raise p.Refused(f"the parent of {root} does not exist; no database is copied") from exc
        except OSError as exc:
            raise p.Refused(f"cannot read {parent}; no database is copied") from exc
        if (not stat.S_ISDIR(found.st_mode) or found.st_uid not in (0, os.geteuid())
                or found.st_mode & 0o022 and not found.st_mode & stat.S_ISVTX):
            raise p.Refused(f"{parent} above migration.copy_root is a symlink or writable by another user; "
                          "no database is copied")
    if p.device(root.parent) == p.device(state):
        raise p.Refused(f"migration.copy_root {root} is on the state's filesystem (is its volume mounted?); "
                      "no database is copied")


def copies_root(state, *, create=False, root=None):
    """`<state>/migration-copies` (or `root`, `migration.copy_root`), None when there is none. A configured root is
    read or created only below trusted parents on another filesystem (`trusted_parents`)."""
    if root is not None:
        trusted_parents(Path(root), Path(state))
    root = Path(root) if root is not None else Path(state) / COPIES
    if create:
        with suppress(FileExistsError):
            root.mkdir(mode=0o700)
    try:
        found = os.lstat(root)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.geteuid() or found.st_mode & 0o077:
        raise p.Refused(f"{root} must be a private operator-owned directory")
    return root


def copy_root(settings):
    """`migration.copy_root`, the directory new copies go to (2026-11-09: the root filesystem was too small for the
    dump and the restore of messaging), or None for `<state>/migration-copies`."""
    section = settings.get("migration")
    named = section.get("copy_root") if isinstance(section, dict) else None
    if named is None:
        return None
    if not isinstance(named, str) or not named.startswith("/") or posixpath.normpath(named) != named or named == "/":
        raise p.Refused("migration.copy_root must be an absolute normalized directory path")
    return Path(named)


def recorded_roots(state):
    """The configured roots a copy was made in (`<state>/copy-roots.json`), in the order they were first used."""
    record = Path(state) / COPY_ROOTS
    if not record.exists():
        return []
    found = p.read(record).get("roots")
    if not isinstance(found, list) or not all(isinstance(item, str) and item.startswith("/") for item in found):
        raise p.Refused(f"{record} is malformed; no copy is reconciled")
    return [Path(item) for item in found]


def record_root(state, root):
    """Remember `root` before the first copy goes there: a later run tends it even after the key changes."""
    known = recorded_roots(state)
    if root not in known:
        p.write_record(Path(state) / COPY_ROOTS, {"roots": [str(item) for item in [*known, root]]})


def copy_roots(settings):
    """Every copy root to tend: the configured one, every recorded one and the state's own, whose older copies are still
    released and expired after a move. A recorded root that is gone (an unmounted volume) refuses: its copies would
    otherwise never be released or expired. A recorded root that holds no copy any more, and is not the configured
    one, is forgotten."""
    state, configured = Path(settings["state_directory"]), p.copy_root(settings)
    recorded = recorded_roots(state)
    roots, seen = [], set()
    for root in dict.fromkeys([*([configured] if configured else []), *recorded]):
        exists = p.present(root) if p.present(root.parent) else None
        found = copies_root(state, root=root) if exists else None
        if found is None and root in recorded:
            raise p.Refused(f"the copy root {root} that {state / COPY_ROOTS} records is missing; mount it, or remove it "
                          "from that file once no copy of it remains")
        if found is not None and found not in seen:
            seen.add(found)
            roots.append(found)
    if (default := copies_root(state)) is not None:
        roots.append(default)
    if (keep := [root for root in recorded if root == configured or copy_directories(root)]) != recorded:
        p.write_record(state / COPY_ROOTS, {"roots": [str(item) for item in keep]})
    return roots


def copy_directories(root):
    """The copy directories under `root` (a planted symlink or file is no copy), oldest first."""
    found = []
    for path in sorted(root.iterdir()) if root else ():
        if COPY_ID.fullmatch(path.name) and stat.S_ISDIR(os.lstat(path).st_mode):
            found.append(path)
    return found


def copy_exec(copy, step, container, *argv):
    """A helper session of the copy: its application name starts with the copy's prefix (C4)."""
    return ["docker", "exec", "-i", "-e", f"PGAPPNAME={copy['application_name']}-{step}", container, *argv]


def copy_room(copy, paths, database_bytes):
    """Before the first dump: the copy's filesystem (`paths[0]`) holds the dumps and the restore, so it needs twice the
    database and the reserve; the live data's filesystem, when another one (`migration.copy_root`), only the reserve:
    the copy writes no file there. What its snapshot keeps there (WAL, row versions vacuum may not reuse yet) is
    watched against the reserve every COPY_POLL seconds while the snapshot is held, queries included (Keeper)."""
    for path in paths:
        free, size = p.filesystem(path)
        if free < (2 * database_bytes if same_filesystem(path, paths[0]) else 0) + copy_reserve(size):
            raise CopyFailed(copy["id"], "disk_reserve")


def same_filesystem(path, other):
    """Whether two paths share a filesystem; one that cannot be read counts as shared, so the larger need applies."""
    try:
        return p.device(path) == p.device(other)
    except OSError:
        return True


def check_reserve(copy, paths):
    for path in paths:
        free, size = p.filesystem(path)
        if free < copy_reserve(size):
            raise CopyFailed(copy["id"], "disk_reserve")


def watched(copy, argv, *, timeout, paths, failure, stdin=None, stdout=None):
    """Run one helper to its end: free space on `paths` is read when it starts and every COPY_POLL seconds; a breach of
    the reserve or the deadline kills it. Its stderr is discarded; a non-zero exit is `failure`."""
    deadline, checked = p.copy_clock() + timeout, None
    process = subprocess.Popen(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                               stdout=stdout if stdout is not None else subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while True:
            moment = p.copy_clock()
            if moment >= deadline:
                raise CopyFailed(copy["id"], "deadline")
            if checked is None or moment - checked >= COPY_POLL:
                checked = moment
                check_reserve(copy, paths)
            with suppress(subprocess.TimeoutExpired):
                if process.wait(timeout=0.2):
                    raise CopyFailed(copy["id"], failure)
                return
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def bounded(argv, *, timeout, text=""):
    """(exit status, stdout) of a short helper reading `text`: stderr is discarded, stdout read up to COPY_OUTPUT
    bytes. More output or the deadline kills it: (None, "")."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdin and process.stdout
    deadline, output = p.copy_clock() + timeout, b""
    try:
        with suppress(OSError):
            process.stdin.write(text.encode())
        with suppress(OSError):
            process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while (remaining := deadline - p.copy_clock()) > 0:
                if selector.select(min(remaining, 1.0)):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        return process.wait(max(1, deadline - p.copy_clock())), output.decode(errors="replace")
                    output += chunk
                    if len(output) > COPY_OUTPUT:
                        break
        return None, ""
    except subprocess.TimeoutExpired:
        return None, ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def helper_output(copy, argv, *, timeout, failure, text=""):
    """The stdout of a short helper reading `text`; it holds only names, counts and versions, never a row."""
    status, output = p.bounded(argv, timeout=timeout, text=text)
    if status is None:
        raise CopyFailed(copy["id"], "deadline")
    if status:
        raise CopyFailed(copy["id"], failure)
    return output


def counts(copy, lines, failure):
    try:
        rows = [json.loads(line) for line in lines if line.strip()]
        return {row["table"]: row["rows"] for row in rows}
    except (ValueError, KeyError, TypeError) as exc:
        raise CopyFailed(copy["id"], failure) from exc


class Keeper:
    """The psql session that holds the copy's exported snapshot until the dumps have read it (C1)."""

    MARK = "deedkit-copy-read"

    def __init__(self, copy, argv, deadline, paths=()):
        self.copy, self.deadline, self.paths, self.checked = copy, deadline, paths, None
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        assert self.process.stdin and self.process.stdout
        self.stdin, self.stdout, self.pending = self.process.stdin, self.process.stdout, b""

    def query(self, text):
        """The output lines of `text`; an error ends psql (ON_ERROR_STOP), and that is `dump_failed`."""
        try:
            self.stdin.write(f"{text}\n\\echo {self.MARK}\n".encode())
            self.stdin.flush()
        except OSError as exc:
            raise CopyFailed(self.copy["id"], "dump_failed") from exc
        lines = []
        with selectors.DefaultSelector() as selector:
            selector.register(self.stdout, selectors.EVENT_READ)
            while True:
                while b"\n" in self.pending:
                    line, self.pending = self.pending.split(b"\n", 1)
                    if line.decode() == self.MARK:
                        return lines
                    lines.append(line.decode())
                moment = p.copy_clock()
                remaining = self.deadline - moment
                if remaining <= 0:
                    raise CopyFailed(self.copy["id"], "deadline")
                if self.paths and (self.checked is None or moment - self.checked >= COPY_POLL):
                    # The snapshot keeps the live database's old row versions: its filesystem is watched (Astra r1).
                    self.checked = moment
                    check_reserve(self.copy, self.paths)
                if selector.select(min(remaining, 1.0)):
                    chunk = os.read(self.stdout.fileno(), 65536)
                    if not chunk:
                        raise CopyFailed(self.copy["id"], "dump_failed")
                    self.pending += chunk

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        with suppress(OSError):
            self.stdin.close()
        self.stdout.close()


def data_filesystem(inspected):
    """The host path of the mount holding the database container's PostgreSQL data directory."""
    env = dict(item.split("=", 1) for item in inspected.get("Config", {}).get("Env") or [] if "=" in item)
    data = posixpath.normpath(env.get("PGDATA", "/var/lib/postgresql/data"))
    mounts = [mount for mount in inspected.get("Mounts") or []
              if data == mount["Destination"] or data.startswith(mount["Destination"].rstrip("/") + "/")]
    if not mounts:
        raise p.Refused("the database container's data directory is not a mount; its free space is unknown")
    return max(mounts, key=lambda mount: len(mount["Destination"]))["Source"]


def file_digest(path):
    hashed, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            hashed.update(chunk)
            size += len(chunk)
    return {"sha256": hashed.hexdigest(), "bytes": size}


def schema_lines(path):
    """A schema-only dump without its random `\\restrict` key lines (probe 2)."""
    return [line for line in Path(path).read_text().splitlines()
            if not line.startswith(("\\restrict ", "\\unrestrict "))]


def dumped(copy, argv, path, **watch):
    descriptor = new_file(path)
    try:
        p.watched(copy, argv, stdout=descriptor, **watch)
    finally:
        os.close(descriptor)


def restored(copy, argv, path, **watch):
    with open(path, "rb") as handle:
        p.watched(copy, argv, stdin=handle, **watch)


def claim(copy, kind, name):
    """Name a container or network a caller (ANYM-449's rehearsal) creates, before it creates it. It must carry the
    label `deedkit-copy=<id>`: `release_copy` removes only what carries it."""
    if kind not in ("container", "network") or not DOCKER_NAME.fullmatch(name):
        raise p.Refused("a copy resource is a named container or network")
    copy["resources"].append({"kind": kind, "name": name})
    p.write_record(Path(copy["directory"]) / "owner.json", copy)


def copy_dumps(copy, keeper, timeout, catalog, container, owner, database, directory, paths, watch):
    """The snapshot transaction on the live database: its identity, free space, counts, catalog and the three dumps;
    returns the snapshot facts, the reference counts and the catalog lines (None without `catalog`)."""
    keeper.query(f"SET idle_in_transaction_session_timeout = '{timeout}s'; SET statement_timeout = '{timeout}s';"
                 " BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;")
    found = json.loads(keeper.query(
        "SELECT json_build_object('snapshot', pg_export_snapshot(), 'at', now(), 'version',"
        " current_setting('server_version'), 'bytes', pg_database_size(d.oid), 'owner', pg_get_userbyid(d.datdba),"
        " 'encoding', pg_encoding_to_char(d.encoding), 'collate', d.datcollate, 'ctype', d.datctype,"
        " 'bootstrap', (SELECT rolname FROM pg_roles WHERE oid = 10))"
        " FROM pg_database d WHERE d.datname = current_database();")[0])
    if (not SNAPSHOT_ID.fullmatch(found["snapshot"])
            or not all(SQL_NAME.fullmatch(found[key] or "") for key in ("owner", "bootstrap"))
            or not all(LOCALE.fullmatch(found[key]) for key in ("encoding", "collate", "ctype"))):
        raise CopyFailed(copy["id"], "dump_failed")
    copy_room(copy, paths, found["bytes"])
    reference = counts(copy, keeper.query(COUNTS), "dump_failed")
    shape = keeper.query(catalog) if catalog else None
    snapshot = "--snapshot=" + found["snapshot"]
    dumped(copy, copy_exec(copy, "schema", container, "pg_dump", "--schema-only", snapshot, "-U", owner,
                           "-d", database), directory / "reference.schema", failure="dump_failed", **watch)
    dumped(copy, copy_exec(copy, "dump", container, "pg_dump", "-Fc", snapshot, "-U", owner, "-d", database),
           directory / f"{database}.dump", failure="dump_failed", **watch)
    dumped(copy, copy_exec(copy, "roles", container, "pg_dumpall", "--roles-only", "--no-role-passwords",
                           "-U", owner, "-l", database), directory / "roles.sql", failure="dump_failed", **watch)
    keeper.query("COMMIT;")
    return found, reference, shape


def copy_scratch(copy, image, directory, timeout, paths, superuser):
    """Start the scratch PostgreSQL and wait until its final server answers; returns its container name and the
    client arguments to reach it. Its bootstrap superuser is `superuser`, the live server's (incident 11-06): pg_dumpall
    writes `GRANTED BY <bootstrap>` on the memberships that role granted, and PostgreSQL 16+ refuses such a grant
    from any other role without ADMIN OPTION; the same name also keeps the copy's grantors equal to the live ones,
    which the rehearsal's catalog compares."""
    scratch, scratch_container = directory / "scratch", copy["container"]
    scratch.mkdir(mode=0o700)
    # A root operator lets the image's entrypoint own the data directory; anyone else runs PostgreSQL as itself, so
    # the cleanup can remove what it wrote.
    user = ["--user", f"{os.geteuid()}:{os.getegid()}"] if os.geteuid() else []
    p.run(["docker", "run", "-d", "--rm", "--name", scratch_container, "--label", "deedkit-copy=" + copy["id"],
         "--network", "none", "--log-driver", "none", "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
         "--shm-size", "256m", *user, "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=" + superuser,
         "-e", "POSTGRES_DB=postgres",
         "-e", "PGDATA=" + COPY_DATA, "-v", f"{scratch}:{COPY_DATA}", image], timeout=120)
    scratch_sql = ("-h", "127.0.0.1", "-U", superuser)
    deadline = p.copy_clock() + timeout
    # The entrypoint's initialisation server listens on no TCP address: 127.0.0.1 answers only the final server.
    while subprocess.run(copy_exec(copy, "ready", scratch_container, "pg_isready", "-q", *scratch_sql, "-d",
                                   "postgres"), stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode:
        if p.copy_clock() >= deadline:
            raise CopyFailed(copy["id"], "deadline")
        check_reserve(copy, paths)
        time.sleep(0.5)
    psql = ("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", *scratch_sql)
    return scratch_container, scratch_sql, psql


def sql_lines(data):
    """The lines of `pg_dumpall` output, each with whether it starts outside every string literal, quoted identifier
    and comment: `'...'` and `"..."` (a doubled quote stays inside; `pg_dumpall` sets standard_conforming_strings),
    `--` to the end of the line."""
    state, found = None, []
    for line in data.splitlines(keepends=True):
        found.append((line, state is None))
        index = 0
        while index < len(line):
            char = line[index:index + 1]
            if state is None and line.startswith(b"--", index):
                break
            if state is None and char in (b"'", b'"'):
                state = char
            elif char == state:
                state = None
            index += 1
    return found


def scratch_roles(copy, source, target, bootstrap):
    """`roles.sql` without its `CREATE ROLE` of the bootstrap superuser, whom the scratch server already has (its
    ALTER ROLE still applies); `pg_dumpall` writes that statement for every role. Only that one statement, a line of
    its own at the top level (`sql_lines`), is left out; every other byte stays."""
    own, kept, dropped = {f"CREATE ROLE {bootstrap};".encode(), f'CREATE ROLE "{bootstrap}";'.encode()}, [], False
    try:
        for line, top in sql_lines(Path(source).read_bytes()):
            if top and not dropped and line.rstrip(b"\n") in own:
                dropped = True
                continue
            kept.append(line)
        with os.fdopen(new_file(target), "wb") as handle:
            handle.writelines(kept)
    except OSError as exc:
        raise CopyFailed(copy["id"], "restore_failed") from exc


def discard(copy, path, failed):
    """Remove a working file of the copy; its error is `restore_failed`, unless the step already failed."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if not failed:
            raise CopyFailed(copy["id"], "restore_failed") from exc


def copy_verify(copy, directory, database, found, reference, scratch_container, scratch_sql, psql, timeout, watch):
    """Restore the dumps into the scratch server and compare counts and schema with the live reference."""
    roles = directory / "scratch-roles.sql"
    try:
        scratch_roles(copy, directory / "roles.sql", roles, found["bootstrap"])
        restored(copy, copy_exec(copy, "roles", scratch_container, *psql, "-d", "postgres"), roles,
                 failure="restore_failed", **watch)
    except BaseException:
        discard(copy, roles, True)
        raise
    discard(copy, roles, False)
    def create(name):
        helper_output(copy, copy_exec(copy, "create", scratch_container, *psql, "-d", "postgres"), timeout=timeout,
                      failure="restore_failed",
                      text=f"CREATE DATABASE \"{name}\" OWNER \"{found['owner']}\" TEMPLATE template0 ENCODING"
                           f" '{found['encoding']}' LC_COLLATE '{found['collate']}' LC_CTYPE '{found['ctype']}';")
    create(database)
    restored(copy, copy_exec(copy, "restore", scratch_container, "pg_restore", "--exit-on-error",
                             "--single-transaction", *scratch_sql, "-d", database), directory / f"{database}.dump",
             failure="restore_failed", **watch)
    copied = counts(copy, helper_output(copy, copy_exec(copy, "count", scratch_container, *psql, "-A", "-t",
                                                        "-d", database), timeout=timeout, failure="restore_failed",
                                        text=COUNTS).splitlines(), "restore_failed")
    if copied != reference:
        raise CopyFailed(copy["id"], "count_mismatch")
    dumped(copy, copy_exec(copy, "verify", scratch_container, "pg_dump", "--schema-only", *scratch_sql,
                           "-d", database), directory / "restored.schema", failure="restore_failed", **watch)
    if schema_lines(directory / "restored.schema") != schema_lines(directory / "reference.schema"):
        # Parsing an expression pg_dump printed does not always print it back the same (incident 11-06: a BETWEEN
        # inside an AND reads back as one flat AND). The reference schema alone, restored into an empty database of
        # the scratch server, then takes the same round trip; the restore must print exactly that.
        name = SCHEMA_ROUND_TRIP if database != SCHEMA_ROUND_TRIP else SCHEMA_ROUND_TRIP + "_2"
        round_trip = directory / "round-trip.schema"
        try:
            try:
                create(name)
                restored(copy, copy_exec(copy, "reference", scratch_container, *psql, "-d", name),
                         directory / "reference.schema", failure="restore_failed", **watch)
                dumped(copy, copy_exec(copy, "round", scratch_container, "pg_dump", "--schema-only", *scratch_sql,
                                       "-d", name), round_trip, failure="restore_failed", **watch)
                helper_output(copy, copy_exec(copy, "drop", scratch_container, *psql, "-d", "postgres"),
                              timeout=timeout, failure="restore_failed", text=f"DROP DATABASE \"{name}\";")
                if schema_lines(directory / "restored.schema") != schema_lines(round_trip):
                    raise CopyFailed(copy["id"], "schema_mismatch")
            except (OSError, UnicodeDecodeError) as exc:
                raise CopyFailed(copy["id"], "restore_failed") from exc
        except BaseException:
            discard(copy, round_trip, True)
            raise
        discard(copy, round_trip, False)
    (directory / "restored.schema").unlink()


def snapshot_copy(copy, timeout, keep, catalog=None):
    """C1-C3: the snapshot, its dumps and their verified restore; writes `copy.json`. With `keep` the scratch container
    stays up for the caller; with `catalog` the snapshot's catalog lines are returned (`catalog`) and their digest is
    recorded."""
    source, directory = copy["source"], Path(copy["directory"])
    container, owner, database = source["container"], source["owner"], source["database"]
    # Only the image, the mounts and PGDATA: the container's other variables hold its password.
    image, mounts, data = (p.run(["docker", "inspect", "--type", "container", "--format", INSPECT_COPY, container],
                               timeout=60).splitlines() + ["", "", ""])[:3]
    inspected = {"Image": json.loads(image), "Mounts": json.loads(mounts),
                 "Config": {"Env": [json.loads(data)] if data else []}}
    paths = [str(directory), data_filesystem(inspected)]
    watch = {"timeout": timeout, "paths": paths}
    keeper = Keeper(copy, copy_exec(copy, "keeper", container, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                                    "-U", owner, "-d", database), p.copy_clock() + timeout, paths)
    try:
        found, reference, shape = copy_dumps(copy, keeper, timeout, catalog, container, owner, database, directory, paths, watch)
    finally:
        keeper.close()
    files = {name: p.file_digest(directory / name) for name in (f"{database}.dump", "roles.sql", "reference.schema")}
    image = inspected["Image"]
    scratch_container, scratch_sql, psql = copy_scratch(copy, image, directory, timeout, paths, found["bootstrap"])
    copy_verify(copy, directory, database, found, reference, scratch_container, scratch_sql, psql, timeout, watch)
    record = {"id": copy["id"], "verified": True, "database": database, "counts": reference, "files": files,
              "snapshot_at": found["at"], "server_version": found["version"], "database_bytes": found["bytes"],
              "image": image, "scratch_container": scratch_container if keep else None,
              "scratch_user": found["bootstrap"], "verified_at": p.now()}
    if shape is not None:
        record["catalog_sha256"] = hashlib.sha256("\n".join(shape).encode()).hexdigest()
    p.write_record(directory / "copy.json", record)
    if not keep:
        stop_copy_containers(copy)
    return record if shape is None else {**record, "catalog": shape}


def copy_sql(source, step, prefix, text):
    """A count or termination statement about the copy's own sessions on the live database."""
    status, output = p.bounded(["docker", "exec", "-i", "-e", f"PGAPPNAME={prefix}-{step}", source["container"], "psql",
                              "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", source["owner"], "-d",
                              source["database"], "-c",
                              text.format(f"starts_with(application_name, '{prefix}-') AND pid <> pg_backend_pid()")],
                             timeout=60)
    if status != 0:
        raise p.Refused("the copy's sessions could not be read")
    return output.strip()


def labelled(copy_id, kind="container"):
    """The IDs of the copy's containers (any state) or networks. Only what carries `deedkit-copy=<id>` is the copy's,
    whatever a name says, so a name collision never reaches another container."""
    listing = ["docker", "ps", "-a", "-q"] if kind == "container" else ["docker", "network", "ls", "-q"]
    return p.run([*listing, "--filter", "label=deedkit-copy=" + copy_id], timeout=60).split()


def stop_copy_containers(copy):
    # An error or a lost answer is decided by the read back, never by these calls. `docker rm` after the stop also
    # removes a container a crash left created but never started.
    with suppress(Exception):
        for container in labelled(copy["id"]):
            with suppress(Exception):
                p.run(["docker", "stop", "-t", "10", container], timeout=60)
            with suppress(Exception):
                p.run(["docker", "rm", container], timeout=60)


def copy_gone(copy):
    """True when no session, container or network of the copy remains (read back, never assumed)."""
    try:
        return (copy_sql(copy["source"], "check", copy["application_name"],
                         "SELECT count(*) FROM pg_stat_activity WHERE {}") == "0"
                and not labelled(copy["id"]) and not labelled(copy["id"], "network"))
    except Exception:
        return False


def release_copy(copy):
    """C4: stop every session and resource `owner.json` names, confirm each gone, remove the scratch data and write
    `released.json`. The dump files stay for the retention (C6)."""
    directory = Path(copy["directory"])
    with suppress(Exception):
        copy_sql(copy["source"], "cancel", copy["application_name"],
                 "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE {}")
    stop_copy_containers(copy)
    with suppress(Exception):
        for network in labelled(copy["id"], "network"):
            with suppress(Exception):
                p.run(["docker", "network", "rm", network], timeout=60)
    deadline = p.copy_clock() + p.COPY_STOP_READBACK
    while not copy_gone(copy):
        if p.copy_clock() >= deadline:
            raise CopyStuck(copy["id"])
        time.sleep(1)
    if os.path.lexists(directory / "scratch"):
        shutil.rmtree(directory / "scratch")
    p.write_record(directory / "released.json", {"id": copy["id"], "released_at": p.now()})


def owned_copy(directory):
    """The copy `owner.json` in `directory` names, its fields checked before any of them reaches a command."""
    copy = p.read(directory / "owner.json")
    source = copy.get("source") or {}
    if (copy.get("id") != directory.name or copy.get("container") != "deedkit-copy-" + directory.name
            or copy.get("application_name") != copy["container"]
            or not isinstance(source, dict) or set(source) != {"container", "owner", "database"}
            or not all(isinstance(value, str) for value in source.values())
            or not DOCKER_NAME.fullmatch(source["container"])
            or not all(SQL_NAME.fullmatch(source[key]) for key in ("owner", "database"))
            or not isinstance(copy.get("resources"), list)
            or not all(isinstance(item, dict) and item.get("kind") in ("container", "network")
                       and isinstance(item.get("name"), str) and DOCKER_NAME.fullmatch(item["name"])
                       for item in copy["resources"])):
        raise CopyStuck(directory.name)
    return {**copy, "directory": str(directory)}


def reconcile_copies(state, roots=None):
    """C4: release every copy a crashed or killed run left without `released.json`, each on its own: (the ids released,
    the ids that could not be). A container that appeared after its copy was released (a start a crash left in
    flight) carries the label of a released copy and is removed too. `roots`: copy_roots, else the state's own."""
    roots = [copies_root(state)] if roots is None else roots
    released, stuck, done = [], [], set()
    found, ambiguous = {}, set()
    for directory in [directory for root in roots for directory in copy_directories(root)]:
        if directory.name in found:
            ambiguous.add(directory.name)
        found[directory.name] = directory
    for name in sorted(ambiguous):  # the same ID in two roots: neither record is trusted (Astra r1 on #807)
        del found[name]
        stuck.append(name)
    for directory in found.values():
        if not (directory / "owner.json").exists():
            continue
        if (directory / "released.json").exists():
            done.add(directory.name)
            continue
        try:
            p.release_copy(owned_copy(directory))
            released.append(directory.name)
        except Exception:
            stuck.append(directory.name)
    if not done and not released:
        return released, stuck
    try:
        # A container or (ANYM-449's rehearsal) network a crash left in flight may appear after the release.
        late = set(p.run(["docker", "ps", "-a", "--filter", "label=deedkit-copy", "--format",
                        '{{.Label "deedkit-copy"}}'], timeout=60).split()) | set(p.run(
            ["docker", "network", "ls", "--filter", "label=deedkit-copy", "--format", '{{.Label "deedkit-copy"}}'],
            timeout=60).split())
        unconfirmed = sorted(late & (done | set(released)))
    except Exception:
        # Docker cannot list: no released copy is confirmed free of late containers.
        unconfirmed = sorted(done | set(released))
        late = set()
    for copy_id in unconfirmed:
        # The same confirmed release; unconfirmed, the copy counts as unreleased again, so expiry keeps its owner.json
        # and the next run retries it.
        try:
            if copy_id not in late:
                raise p.Refused("the copy's containers could not be listed")
            p.release_copy(owned_copy(found[copy_id]))
        except Exception:
            with suppress(FileNotFoundError):
                (found[copy_id] / "released.json").unlink()
            stuck.append(copy_id)
            if copy_id in released:
                released.remove(copy_id)
    return released, stuck


def settle_copies(state, roots=None):
    """Reconcile, then expire whatever happened (C6 never waits for a stuck copy); a stuck copy then refuses."""
    _, stuck = reconcile_copies(state, roots)
    expire_copies(state, roots=roots)
    if stuck:
        raise CopyStuck(stuck[0])


def expire_copies(state, *, at=None, roots=None):
    """C6: delete every copy older than COPY_RETENTION, whatever its state. Only the `owner.json` of a copy whose
    resources are not confirmed released stays, so a later run can still stop them."""
    at, expired = at or datetime.now(timezone.utc), []
    roots = [copies_root(state)] if roots is None else roots
    names = [directory.name for root in roots for directory in copy_directories(root)]
    for directory in [directory for root in roots for directory in copy_directories(root)
                      if names.count(directory.name) == 1]:
        stamp = COPY_ID.fullmatch(directory.name)
        made = datetime.strptime(stamp[1] if stamp else "", "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if (at - made).total_seconds() <= COPY_RETENTION:
            continue
        if (directory / "released.json").exists() or not (directory / "owner.json").exists():
            shutil.rmtree(directory)
        else:
            for entry in directory.iterdir():
                if entry.name != "owner.json":
                    shutil.rmtree(entry) if stat.S_ISDIR(os.lstat(entry).st_mode) else entry.unlink()
        expired.append(directory.name)
    return expired


@contextmanager
def database_copy(settings, name, sha, *, keep=False, catalog=None):
    """ANYM-588: a verified copy of `migration.databases.<name>` taken for release `sha`; its `copy.json` record. With
    `keep` the scratch container serves the block and the record carries the copy's owner record (`copy`, for
    `claim`); with `catalog` (ANYM-449) also the lines that query printed in the copy's snapshot. Every call ends with
    its resources released, pass or fail."""
    source, timeout = copy_source(settings, name)
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise p.Refused("an exact lowercase 40-character commit SHA is required")
    state = Path(settings["state_directory"])
    if (configured := p.copy_root(settings)) is not None:
        trusted_parents(configured, state)
        record_root(state, configured)
    root = copies_root(state, create=True, root=configured)
    assert root
    settle_copies(state, copy_roots(settings))
    copy_id = f"{sha[:12]}-{name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    directory = root / copy_id
    directory.mkdir(mode=0o700)
    copy = {"id": copy_id, "directory": str(directory), "container": "deedkit-copy-" + copy_id,
            "application_name": "deedkit-copy-" + copy_id, "source": source, "resources": [], "created_at": p.now()}
    p.write_record(directory / "owner.json", copy)
    try:
        found = p.snapshot_copy(copy, timeout, keep, catalog)
        yield {**found, "copy": copy} if keep else found
    finally:
        p.release_copy(copy)


def tend_copies(settings, *, locked):
    """`deploy`'s copy housekeeping (C4, C6). Unlocked (a paused run) it takes the plan A lock only when it is free."""
    state = Path(settings["state_directory"])
    if not (roots := copy_roots(settings)):
        return
    with ExitStack() as held:
        if not locked:
            try:
                held.enter_context(p.lock(state))
            except p.Busy:
                return
        settle_copies(state, roots)
