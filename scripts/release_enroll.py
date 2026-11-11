"""Module enrollment of the deploy tool (ANYM-745, production.md "Enrollment"), moved verbatim from production.py
(W5-1b). A tool file (TOOL_FILES): production.py loads it from its own directory and sets `p` to a view of its names
(ToolNames) before this code runs, so every name of production.py is read through `p` at call time, and production.py
imports every name defined here. `production.<name>` stays the one name callers use and tests patch."""
from __future__ import annotations

from contextlib import ExitStack, suppress
import copy
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

p = globals().get("p")  # production.py's names, set by its tool_module() before this module runs
if p is None:
    raise ImportError("release_enroll.py is loaded by production.py (tool_module), never imported on its own")


# Enrollment of a new module (ANYM-745, production.md "Enrollment"): `enroll --module M --sha S --plan` reads the
# reviewed manifest deploy/modules/M.json from the verified main commit S, checks every precondition of a hand
# enrollment and prints the exact edits with a `plan_digest`. It writes no production state (its scratch files live in
# private temporary directories) and prints no secret: env files, private configurations and key files are read for
# presence, mode, owner and digests only.
MODULE_DIRECTORY = "deploy/modules/"
ENROLL_OWNER = 0  # the env file's owner on the host: root
ENROLL_FORBIDDEN_SERVICES = frozenset({"db", "calendar-db", "database", "postgres", "operator"})
VERIFY_SCRIPT = re.compile(r"scripts/[a-z0-9_]+_egress_check\.py")
BACKUP_FILES = re.compile(r"services/messaging/messaging_service/backup_(runner|profiles)\.py|deploy/[^/]*backup[^/]*\.(service|timer)")
MODULE_NAME = re.compile(r"[a-z][a-z0-9-]{0,40}")
NETWORK_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
BRIDGE_NAME = re.compile(r"[a-z][a-z0-9-]{0,14}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
KEY_PATH = re.compile(r"[A-Za-z0-9_]+(\[\*\])?(\.[A-Za-z0-9_]+(\[\*\])?)*")
ENROLL_CODES = (
    "manifest_missing", "manifest_invalid", "manifest_unknown_key", "manifest_repo_path", "manifest_relative_path",
    "manifest_forbidden_service", "manifest_grant_wildcard", "manifest_unknown_principal", "manifest_egress_incomplete",
    "manifest_verify_script", "manifest_live_actions", "sha_not_verified", "sha_already_prepared", "module_enrolled",
    "policy_module_present", "range_sql_inventory", "range_tool_pending", "range_backup_changed",
    "env_file_missing", "env_file_mode", "env_variable_unset", "compose_config_failed", "call_active",
    "calls_unknown", "paused", "egress_env_missing", "egress_verify_failed", "key_file_invalid",
    "policy_unreadable", "policy_check_failed", "private_config_unreadable", "private_config_forbidden_key",
    "no_current_release", "gateway_project_missing", "source_unavailable", "command_timeout", "egress_target_invalid",
    "egress_blocked_incomplete", "egress_control_missing", "credential_file_invalid",
    # ANYM-746: `enroll --apply` and `--undo`
    "plan_changed", "enrollment_unfinished", "enrollment_not_applied", "active_call", "overlay_conflict",
    "network_unsafe", "egress_admission_not_required", "confinement_check_failed", "isolation_check_failed",
    "configuration_changed", "policy_changed")
# The blocked targets a manifest must cover, by address class (a host listener, a tailnet node's listener).
EGRESS_CLASSES = {"host": ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"), "tailnet": ("100.64.0.0/10",)}
MANIFEST_KEYS = {"name", "project", "gateway", "egress", "private_config", "owner_steps", "live_actions"}


class EnrollRefused(p.Refused):
    """One failed precondition of an enrollment, with its code (ENROLL_CODES) and a detail that never holds a secret."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class EnrollRefusals(p.Refused):
    """Every failed precondition of one plan."""

    def __init__(self, failures):
        super().__init__("; ".join(f"{item['code']}: {item['detail']}" if item["detail"] else item["code"]
                                   for item in failures))
        self.failures = failures


def manifest_keys(value, keys, where, required=None):
    if not isinstance(value, dict):
        raise EnrollRefused("manifest_invalid", f"{where} must be an object")
    if unknown := sorted(set(value) - keys):
        raise EnrollRefused("manifest_unknown_key", f"{where}: {', '.join(unknown)}")
    if missing := sorted((keys if required is None else required) - set(value)):
        raise EnrollRefused("manifest_invalid", f"{where} lacks {', '.join(missing)}")
    return value


def manifest_text(value, where, pattern=None):
    if not isinstance(value, str) or not value or (pattern and not pattern.fullmatch(value)):
        raise EnrollRefused("manifest_invalid", f"{where} is not an accepted string")
    return value


def manifest_list(value, where, item=manifest_text):
    if not isinstance(value, list) or not value:
        raise EnrollRefused("manifest_invalid", f"{where} must be a non-empty list")
    return [item(entry, where) for entry in value]


def repo_path(value, where):
    path = manifest_text(value, where)
    if path.startswith("/") or ".." in Path(path).parts or not path.startswith("deploy/"):
        raise EnrollRefused("manifest_repo_path", f"{where}: {path} is not a repository path under deploy/")
    return path


def host_path(value, where):
    path = manifest_text(value, where)
    if not path.startswith("/") or ".." in Path(path).parts:
        raise EnrollRefused("manifest_relative_path", f"{where}: {path} is not an absolute host path")
    return path


def check_manifest(value, name):
    """The manifest `value` of module `name`, or the EnrollRefused of its first defect (a code per cause). A field of an
    unexpected type is a refusal too, never an exception: an unrelated malformed manifest must not halt a release."""
    try:
        return p.checked_manifest(value, name)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise EnrollRefused("manifest_invalid", f"a field has an unexpected type ({type(exc).__name__})") from None


def check_networks(networks):
    """`project.networks`: the internal Compose networks the module needs created once (`apply` creates a missing one
    with its bridge name and never removes one); `gateway_port` marks a network the gateway joins and the isolation
    probe checks after promotion."""
    if not isinstance(networks, list):
        raise EnrollRefused("manifest_invalid", "project.networks must be a list")
    seen = set()
    for item in networks:
        item = manifest_keys(item, {"name", "bridge", "gateway_port"}, "project.networks", required={"name", "bridge"})
        manifest_text(item["name"], "project.networks.name", NETWORK_NAME)
        manifest_text(item["bridge"], "project.networks.bridge", BRIDGE_NAME)
        port = item.get("gateway_port", 1)
        if type(port) is not int or not 0 < port < 65536 or item["name"] in seen:
            raise EnrollRefused("manifest_invalid", "project.networks: a duplicate name or a bad gateway_port")
        seen.add(item["name"])


def checked_manifest(value, name):
    manifest_keys(value, MANIFEST_KEYS, "manifest")
    if manifest_text(value["name"], "name", MODULE_NAME) != name:
        raise EnrollRefused("manifest_invalid", "name differs from the file name")
    # `networks` is optional: a manifest key the installed host tool does not know is a "malformed manifest" to it, and a
    # deploy then pauses even for a module that is already running (incident 11-04, #632).
    project = manifest_keys(value["project"], {"name", "files", "env_file", "env_required", "services", "networks"}, "project",
                            required={"name", "files", "env_file", "env_required", "services"})
    check_networks(project.get("networks", []))
    manifest_text(project["name"], "project.name", re.compile(r"[a-z0-9][a-z0-9_-]*"))
    manifest_list(project["files"], "project.files", p.repo_path)
    p.host_path(project["env_file"], "project.env_file")
    manifest_list(project["env_required"], "project.env_required", lambda item, where: manifest_text(item, where, ENV_NAME))
    services = project["services"]
    if not isinstance(services, dict) or not services:
        raise EnrollRefused("manifest_invalid", "project.services must name a service and its build target")
    for service, target in services.items():
        if not isinstance(service, str) or service in ENROLL_FORBIDDEN_SERVICES:
            raise EnrollRefused("manifest_forbidden_service", f"{service} is not a promotable application service")
        if not isinstance(target, str) or target not in p.BUILDS:
            raise EnrollRefused("manifest_invalid", f"project.services.{service} names no known build target")
    gateway = manifest_keys(value["gateway"], {"overlay", "policy"}, "gateway")
    if gateway["overlay"] is not None:
        p.repo_path(gateway["overlay"], "gateway.overlay")
    policy = manifest_keys(gateway["policy"], {"module", "backend_url", "tools", "principals", "key_dir", "key_file_pattern",
                                               "key_uid"}, "gateway.policy")
    manifest_text(policy["module"], "gateway.policy.module", MODULE_NAME)
    manifest_text(policy["backend_url"], "gateway.policy.backend_url", re.compile(r"http://[a-z0-9.-]+:[0-9]{2,5}"))
    manifest_list(policy["tools"], "gateway.policy.tools", lambda item, where: manifest_text(item, where, re.compile(r"[a-z][a-z0-9_]{0,63}")))
    p.host_path(policy["key_dir"], "gateway.policy.key_dir")
    if "{principal}" not in manifest_text(policy["key_file_pattern"], "gateway.policy.key_file_pattern") or not policy["key_file_pattern"].startswith("/"):
        raise EnrollRefused("manifest_invalid", "gateway.policy.key_file_pattern must be an absolute path with {principal}")
    if type(policy["key_uid"]) is not int or policy["key_uid"] < 0:
        raise EnrollRefused("manifest_invalid", "gateway.policy.key_uid must be a user id")
    principals = policy["principals"]
    if not isinstance(principals, dict) or not principals:
        raise EnrollRefused("manifest_invalid", "gateway.policy.principals must name a principal and its grants")
    for principal, section in principals.items():
        grants = manifest_keys(section, {"grants"}, f"gateway.policy.principals.{principal}")["grants"]
        for grant in manifest_list(grants, f"gateway.policy.principals.{principal}.grants"):
            if "*" in grant:
                raise EnrollRefused("manifest_grant_wildcard", f"{principal} is granted {grant}")
    egress = value["egress"]
    if isinstance(egress, dict) and egress.get("outside") is True:
        keys = {"outside", "admission_env", "canary", "verify", "verify_env", "open", "blocked", "controls"}
        if set(egress) < keys:
            raise EnrollRefused("manifest_egress_incomplete", "outside egress needs " + ", ".join(sorted(keys - set(egress))))
        manifest_keys(egress, keys, "egress")
        admission = manifest_keys(egress["admission_env"], {"blocked", "open"}, "egress.admission_env")
        for key in admission.values():
            manifest_text(key, "egress.admission_env", ENV_NAME)
        manifest_text(egress["canary"], "egress.canary", re.compile(r"[0-9.]+:[0-9]{1,5}"))
        verify = manifest_list(egress["verify"], "egress.verify")
        if not VERIFY_SCRIPT.fullmatch(verify[0]):
            raise EnrollRefused("manifest_verify_script", f"{verify[0]} is not a scripts/*_egress_check.py file")
        manifest_list(egress["verify_env"], "egress.verify_env", lambda item, where: manifest_text(item, where, ENV_NAME))
        manifest_list(egress["open"], "egress.open")
        for item in manifest_list(egress["blocked"], "egress.blocked"):
            if item not in EGRESS_CLASSES:
                raise EnrollRefused("manifest_invalid", f"egress.blocked: {item} is not one of {', '.join(sorted(EGRESS_CLASSES))}")
        controls = manifest_keys(egress["controls"], {"dns_env"}, "egress.controls")
        manifest_text(controls["dns_env"], "egress.controls.dns_env", ENV_NAME)
    else:
        manifest_keys(egress, {"outside"}, "egress")
        if egress["outside"] is not False:
            raise EnrollRefused("manifest_invalid", "egress.outside must be true or false")
    private = manifest_keys(value["private_config"], {"mount", "file", "forbidden_keys", "credential_files"}, "private_config")
    p.host_path(private["mount"], "private_config.mount")
    manifest_text(private["file"], "private_config.file", re.compile(r"[A-Za-z0-9_.-]+"))
    manifest_list(private["forbidden_keys"], "private_config.forbidden_keys", lambda item, where: manifest_text(item, where, KEY_PATH))
    manifest_list(private["credential_files"], "private_config.credential_files", lambda item, where: manifest_text(item, where, KEY_PATH))
    if not isinstance(value["owner_steps"], list) or not all(isinstance(step, str) for step in value["owner_steps"]):
        raise EnrollRefused("manifest_invalid", "owner_steps must be a list of strings")
    if value["live_actions"] != []:
        raise EnrollRefused("manifest_live_actions", "a manifest never carries a live action")
    return value


def repo_file(mirror, sha, path):
    return p.run(["git", "-C", str(mirror), "cat-file", "blob", f"{sha}:{path}"], binary=True)


def repo_blob_id(mirror, sha, path):
    return p.run(["git", "-C", str(mirror), "rev-parse", f"{sha}:{path}"]).strip()


def read_manifest(mirror, sha, name):
    path = f"{MODULE_DIRECTORY}{name}.json"
    try:
        text = p.repo_file(mirror, sha, path)
        blob_id = repo_blob_id(mirror, sha, path)
    except p.Refused:
        raise EnrollRefused("manifest_missing", f"{path} is not in {sha[:12]}") from None
    try:
        value = json.loads(text)
    except ValueError:
        raise EnrollRefused("manifest_invalid", f"{path} is not JSON") from None
    return check_manifest(value, name), blob_id


def read_env_file(path):
    """`(variables, content digest)` of the project's env file: a regular file, mode 0600, owned by ENROLL_OWNER.
    Values stay in memory: a plan prints names and digests only."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise EnrollRefused("env_file_missing", path) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600 or info.st_uid != p.ENROLL_OWNER:
            raise EnrollRefused("env_file_mode", f"{path} must be a regular file, mode 0600, owner {p.ENROLL_OWNER}")
        content = os.read(fd, 1 << 20)
    finally:
        os.close(fd)
    variables = {}
    for line in content.decode("utf-8", "replace").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and ENV_NAME.fullmatch(key):
            variables[key] = value.strip().strip("'\"")
    return variables, hashlib.sha256(content).hexdigest()


def private_config_nodes(document, pattern):
    """The values at the manifest key path `pattern` (`archive`, `accounts[*].archive_namespace`) in `document`."""
    nodes = [document]
    for segment in pattern.split("."):
        key, each = segment.removesuffix("[*]"), segment.endswith("[*]")
        step = [node[key] for node in nodes if isinstance(node, dict) and key in node]
        nodes = [item for node in step for item in (node if isinstance(node, list) else list(node.values())
                                                    if isinstance(node, dict) else [])] if each else step
    return nodes


def private_config_keys(document, patterns):
    """The manifest key paths that are present in `document`; the presence of a key only, never a value."""
    return [pattern for pattern in patterns if private_config_nodes(document, pattern)]


def credential_file_checks(value, private, source, owner):
    """The credential files `value` (the private configuration) names at the manifest's `credential_files` paths: each
    resolves through the Compose mount to a regular, non-empty file of the configuration's owner that only the owner can
    read. Mode, owner and size only; a content byte is never read."""
    for pattern in private["credential_files"]:
        parent, _, last = pattern.rpartition(".")
        if parent and not last.endswith("[*]"):
            # Every entry the parent path matches must carry the key: one account without it fails the whole configuration.
            references, entries = [], private_config_nodes(value, parent)
            for entry in entries:
                if not isinstance(entry, dict) or last not in entry:
                    raise EnrollRefused("credential_file_invalid", f"{pattern} is missing in an entry of {parent}")
                references.append(entry[last])
        else:
            references = private_config_nodes(value, pattern)
        if not references:
            raise EnrollRefused("credential_file_invalid", f"the private configuration has no value at {pattern}")
        for reference in references:
            try:
                inside = PurePosixPath(reference).relative_to(private["mount"])
            except (TypeError, ValueError):
                inside = None
            if not isinstance(reference, str):
                inside = None
            if inside is None or ".." in inside.parts or not inside.parts:
                raise EnrollRefused("credential_file_invalid", f"{pattern} names a file outside {private['mount']}")
            path = Path(source) / Path(*inside.parts)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                raise EnrollRefused("credential_file_invalid", f"{path} is missing") from None
            try:
                info = os.fstat(fd)
            finally:
                os.close(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != owner or info.st_size == 0
                    or info.st_size > 8192):
                raise EnrollRefused("credential_file_invalid",
                                    f"{path} must be a non-empty regular file, owner {owner}, no group or other access")


def serving_gateway_images():
    """Every distinct image a running gateway container (plan A's or an HTTP generation's) uses."""
    shown = p.run(["docker", "ps", "--no-trunc", "--format", '{{.Image}}\t{{.Label "com.docker.compose.service"}}'])
    images = {image for image, _, service in (line.partition("\t") for line in shown.splitlines())
              if service == "gateway" or service.startswith("http-gateway-")}
    return sorted(images)


def additions_only(old, new):
    """`new` holds all of `old`: only object keys and list items were added, nothing was changed or removed."""
    if isinstance(old, dict):
        return isinstance(new, dict) and all(key in new and additions_only(value, new[key]) for key, value in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and len(new) >= len(old) and all(map(additions_only, old, new))
    return old == new


def candidate_policy(live, manifest, key_hashes):
    """The live gateway policy plus the module: one revision higher, additions only. `(candidate, added paths)`."""
    fragment = manifest["gateway"]["policy"]
    module, new, added = fragment["module"], copy.deepcopy(live), []
    new["revision"] = live["revision"] + 1
    added.append(f"revision {live['revision']} -> {new['revision']}")
    deployment = new["deployment"]
    if module in deployment["modules"] or module in deployment["backends"]:
        raise EnrollRefused("policy_module_present", f"the live policy already names {module}")
    deployment["modules"].append(module)
    deployment["backends"][module] = {"url": fragment["backend_url"]}
    added += [f"deployment.modules + {module}", f"deployment.backends.{module} = {fragment['backend_url']}"]
    by_id = {principal["id"]: principal for principal in new["principals"]}
    for principal, section in fragment["principals"].items():
        if principal not in by_id:
            raise EnrollRefused("manifest_unknown_principal", f"the live policy defines no principal {principal}")
        entry = by_id[principal]
        if module in entry["grants"] or module in entry["credentials"]:
            raise EnrollRefused("policy_module_present", f"{principal} already has {module}")
        entry["grants"][module] = list(section["grants"])
        entry["credentials"][module] = {"key_file": fragment["key_file_pattern"].format(principal=principal),
                                        "key_sha256": key_hashes[principal]}
        added += [f"principals.{principal}.grants.{module} = {', '.join(section['grants'])}",
                  f"principals.{principal}.credentials.{module} = key_file {entry['credentials'][module]['key_file']}, "
                  f"key_sha256 {key_hashes[principal]}"]
        profile = new["profiles"][entry["profile"]]
        for tool in fragment["tools"]:
            if tool not in profile["tools"]:
                profile["tools"].append(tool)
                added.append(f"profiles.{entry['profile']}.tools + {tool}")
    if not additions_only({**live, "revision": new["revision"]}, new):
        raise EnrollRefused("policy_check_failed", "the candidate policy is not additions only")
    return new, added


def key_digest(manifest, principal):
    """The sha256 of a principal's gateway key file: present, a regular file, mode 0600, owned by the manifest's uid."""
    fragment = manifest["gateway"]["policy"]
    path = Path(fragment["key_dir"]) / Path(fragment["key_file_pattern"].format(principal=principal)).name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise EnrollRefused("key_file_invalid", f"{path} is missing") from None
    try:
        info = os.fstat(fd)
        content = os.read(fd, 1025)
    finally:
        os.close(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600 or info.st_uid != fragment["key_uid"]
            or not content.strip() or len(content) > 1024):
        raise EnrollRefused("key_file_invalid", f"{path} must be a non-empty regular file, mode 0600, owner {fragment['key_uid']}")
    return hashlib.sha256(content.strip()).hexdigest()


def check_policy_candidate(candidate, directory):
    """`deedkit-gateway --check` of the candidate in every serving gateway image, from a private scratch file."""
    path = Path(directory) / "policy.json"
    fd = p.new_file(path)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(candidate))
    try:
        images = serving_gateway_images()
    except (p.Refused, subprocess.TimeoutExpired):
        raise EnrollRefused("policy_check_failed", "the serving gateway images cannot be listed") from None
    if not images:
        raise EnrollRefused("policy_check_failed", "no serving gateway image was found")
    for image in images:
        try:
            shown = p.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--user", "0:0",
                         "--entrypoint", "deedkit-gateway", "-v", f"{path}:/policy.json:ro", image, "--check", "--policy",
                         "/policy.json", "--public-url", "https://check.invalid"], timeout=120)
            valid = json.loads(shown.strip().splitlines()[-1]).get("valid") is True
        except (p.Refused, ValueError, IndexError, AttributeError, subprocess.TimeoutExpired):
            valid = False
        if not valid:
            raise EnrollRefused("policy_check_failed", f"the gateway check of the candidate failed in {image}")


def compose_document(project, directory, files):
    """The resolved Compose document of `project` with exactly `files` and its env files, from scratch copies."""
    shown = p.run(p.compose({**project, "directory": directory, "files": files}) + ["config", "--format", "json"])
    return json.loads(shown)


def enroll_plan(settings, http, name, sha, mirror, state=None):
    """The enrollment plan of module `name` at `sha` (a dict ending in `plan_digest`), or EnrollRefusals. Writes nothing
    but private scratch files; the manifest, the live policy, the configurations and the env file are read-only."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise EnrollRefused("sha_not_verified", "an exact lowercase 40-character commit SHA is required")
    try:
        evidence = p.promotable(sha)
    except (p.Refused, subprocess.TimeoutExpired) as exc:
        raise EnrollRefused("sha_not_verified", str(exc)[:300]) from exc
    state = Path(settings["state_directory"])
    manifest, manifest_id = read_manifest(mirror, sha, name)
    failures = []

    def attempt(check, *args):
        try:
            return check(*args)
        except EnrollRefused as exc:
            failures.append({"code": exc.code, "detail": exc.detail})
        return None

    project, fragment, private = manifest["project"], manifest["gateway"]["policy"], manifest["private_config"]
    if any(item["name"] == project["name"] for item in settings["projects"]):
        raise EnrollRefused("module_enrolled", f"{project['name']} is already a project of the configuration")
    if (state / sha).exists():
        failures.append({"code": "sha_already_prepared", "detail": f"{state / sha} exists"})
    if p.PAUSE.exists():
        failures.append({"code": "paused", "detail": "the autodeploy pause file is present"})
    try:
        if active := p.active_calls(settings):
            failures.append({"code": "call_active", "detail": f"{len(active)} active call(s)"})
    except p.CallsUnknown as exc:
        failures.append({"code": "calls_unknown", "detail": str(exc)[:200]})
    current = p.read(state / "current.json") if (state / "current.json").exists() else {}
    if not re.fullmatch(r"[0-9a-f]{40}", current.get("sha", "")):
        failures.append({"code": "no_current_release", "detail": "no applied release is recorded"})
    else:
        installed = {**p.running_tool(p.http_release_tool()), **{path: p.SYSTEMD_DIRECTORY / Path(path).name for path in p.UNITS}}
        sql, tools = p.range_changes(mirror, current["sha"], sha, installed)
        if sql:
            failures.append({"code": "range_sql_inventory", "detail": ", ".join(sql[:5])})
        if tools:
            failures.append({"code": "range_tool_pending", "detail": ", ".join(tools[:5])})
        changed = p.run(["git", "-C", str(mirror), "diff", "--name-only", "--no-renames", "-z", current["sha"], sha]).split("\0")
        if backup := [path for path in changed if BACKUP_FILES.fullmatch(path)]:
            failures.append({"code": "range_backup_changed", "detail": ", ".join(backup[:5])})
    gateway = next((item for item in settings["projects"] if "gateway" in item["services"].values()), None)
    gateway_http = next((item for item in http["projects"] if gateway and item["name"] == gateway["name"]), None)
    if gateway is None or gateway_http is None:
        raise EnrollRefused("gateway_project_missing", "both configurations need the gateway project")
    directory = gateway["directory"]
    overlays, reference = [], None
    try:
        reference = os.stat(gateway["files"][0])
    except OSError:
        failures.append({"code": "source_unavailable", "detail": "the gateway project's first file cannot be read"})
    wanted = [*project["files"], *([manifest["gateway"]["overlay"]] if manifest["gateway"]["overlay"] else [])]
    contents = {}
    for path in wanted:
        try:
            contents[path] = p.repo_file(mirror, sha, path)
        except p.Refused:
            failures.append({"code": "source_unavailable", "detail": f"{path} is not in {sha[:12]}"})
    for path, content in contents.items():
        overlays.append({"repo_path": path, "host_path": f"{directory}/{Path(path).name}",
                         "sha256": hashlib.sha256(content).hexdigest(), "blob": repo_blob_id(mirror, sha, path),
                         **({"owner": reference.st_uid, "group": reference.st_gid, "mode": oct(reference.st_mode & 0o777)}
                            if reference else {})})
    host = {item["repo_path"]: item["host_path"] for item in overlays}
    new_project = {"name": project["name"], "directory": directory, "files": [host.get(path, "") for path in project["files"]],
                   "env_files": [project["env_file"]], "services": dict(project["services"])}
    overlay_host = host.get(manifest["gateway"]["overlay"])
    gateway_files = [*gateway["files"], *([overlay_host] if overlay_host and overlay_host not in gateway["files"] else [])]
    gateway_http_files = [*gateway_http["files"],
                          *([overlay_host] if overlay_host and overlay_host not in gateway_http["files"] else [])]
    variables, env_digest = {}, None
    env = attempt(p.read_env_file, project["env_file"])
    if env is not None:
        variables, env_digest = env
        for key in project["env_required"]:
            if not variables.get(key):
                failures.append({"code": "env_variable_unset", "detail": key})
    documents = {}
    with tempfile.TemporaryDirectory(prefix="enroll-") as scratch:
        os.chmod(scratch, 0o700)
        scratch_files = {}
        for path, content in contents.items():
            target = Path(scratch) / Path(path).name
            with os.fdopen(p.new_file(target), "wb") as handle:
                handle.write(content)
            scratch_files[path] = str(target)
        if env is not None and contents.keys() >= set(wanted):  # a path listed twice has one entry in `contents`
            overlay = manifest["gateway"]["overlay"]
            for label, item, files in (("project", new_project, [scratch_files[path] for path in project["files"]]),
                                       ("gateway", gateway, [*gateway["files"], *([scratch_files[overlay]] if overlay else [])]),
                                       ("gateway_http", gateway_http,
                                        [*gateway_http["files"], *([scratch_files[overlay]] if overlay else [])])):
                try:
                    documents[label] = compose_document(item, item["directory"], files)
                except (p.Refused, ValueError, subprocess.TimeoutExpired) as exc:
                    failures.append({"code": "compose_config_failed", "detail": f"{item['name']}: {type(exc).__name__}"})
        private_digest = None
        if "project" in documents:
            private_digest = attempt(check_private_config, documents["project"], project, private)
            attempt(check_external_networks, project, documents["project"])
        egress = manifest["egress"]
        if egress["outside"] and env is not None:
            attempt(check_egress, mirror, sha, egress, variables, scratch)
        live_path = Path(http["http_release"]["gateway_policy"])
        live = live_digest = candidate = added = None
        try:
            live_bytes = live_path.read_bytes()
            live, live_digest = json.loads(live_bytes), hashlib.sha256(live_bytes).hexdigest()
        except (OSError, ValueError):
            failures.append({"code": "policy_unreadable", "detail": str(live_path)})
        if live is not None:
            defined = {entry.get("id") for entry in live.get("principals", [])}
            unknown = [principal for principal in fragment["principals"] if principal not in defined]
            for principal in unknown:
                failures.append({"code": "manifest_unknown_principal", "detail": f"the live policy defines no principal {principal}"})
            hashes = {}
            for principal in ([] if unknown else fragment["principals"]):
                hashes[principal] = attempt(key_digest, manifest, principal)
            if hashes and all(hashes.values()):
                try:
                    candidate, added = p.candidate_policy(live, manifest, hashes)
                    check_policy_candidate(candidate, scratch)
                except EnrollRefused as exc:
                    failures.append({"code": exc.code, "detail": exc.detail})
    if "project" not in documents and not failures:  # the private configuration and the networks were not checked
        failures.append({"code": "compose_config_failed", "detail": f"{project['name']}: no resolved document"})
    if failures:
        raise EnrollRefusals(failures)
    grants = {principal: sorted(section["grants"]) for principal, section in fragment["principals"].items()}
    inputs = {"sha": sha, "manifest": manifest_id, "production": p.digest(settings), "http": p.digest(http),
              "live_policy": live_digest, "overlays": {item["repo_path"]: item["sha256"] for item in overlays},
              "grants": grants, "verify": egress.get("verify"), "canary": egress.get("canary"),
              "env": env_digest, "private_config": private_digest, "candidate_policy": p.digest(candidate),
              "compose": {label: p.digest(document) for label, document in sorted(documents.items())}}
    return {"status": "plan", "module": name, "sha": sha, "ci": evidence, "manifest_blob": manifest_id, "overlays": overlays,
            "production_json": {"append_project": new_project,
                                "gateway_project_files": gateway_files},
            "http_production_json": {"gateway_project_files": gateway_http_files},
            "policy": {"base_revision": live["revision"], "revision": candidate["revision"], "added": added,
                       "grants": grants, "candidate_digest": inputs["candidate_policy"]},
            "owner_steps": manifest["owner_steps"], "live_actions": [], "inputs": inputs,
            "plan_digest": p.digest(inputs)}


def check_external_networks(project, document):
    """Refuses an external network of the resolved Compose document the manifest does not declare. Its Docker name is
    `name` or, when absent, the Compose key. Reads the document only: `enroll --plan` and `check()` run it before a write."""
    declared = {item["name"] for item in project.get("networks", [])}
    external = {network.get("name") or key for key, network in (document.get("networks") or {}).items()
                if isinstance(network, dict) and network.get("external")}
    if undeclared := sorted(external - declared):
        raise EnrollRefused("network_unsafe", ", ".join(undeclared) + " is external but not declared in the manifest")


def check_private_config(document, project, private):
    """The digest of the private configuration the container mounts, resolved from the Compose document; refuses
    while a manifest forbidden key is present (a first start is on-demand reads only)."""
    mounts = [volume["source"] for service in project["services"] for volume in document["services"].get(service, {}).get("volumes", [])
              if isinstance(volume, dict) and volume.get("target") == private["mount"] and volume.get("source")]
    if len(mounts) != 1:
        raise EnrollRefused("private_config_unreadable", f"the Compose document mounts {private['mount']} {len(mounts)} times")
    path = Path(mounts[0]) / private["file"]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            owner = os.fstat(fd).st_uid
            content = os.read(fd, 1 << 20)
        finally:
            os.close(fd)
        value = json.loads(content)
    except (OSError, ValueError):
        raise EnrollRefused("private_config_unreadable", str(path)) from None
    if present := private_config_keys(value, private["forbidden_keys"]):
        raise EnrollRefused("private_config_forbidden_key", present[0])
    credential_file_checks(value, private, mounts[0], owner)
    return hashlib.sha256(content).hexdigest()


def blocked_address(item):
    """The IP address of a blocked target the way the recordings admission reads it: `IP:PORT` (brackets around an IPv6
    literal allowed) or `dns:IP`, `dns:IP:PORT`, `dns:[IPV6]` and `dns:[IPV6]:PORT`; None when malformed."""
    try:
        if item.startswith("dns:"):
            body = item[4:]
            if body.startswith("["):
                host, closed, rest = body[1:].partition("]")
                if not closed:
                    return None
                port = rest.removeprefix(":") or "53"
            elif body.count(":") == 1:
                host, port = body.split(":")
            else:
                host, port = body, "53"
        else:
            host, _, port = item.rpartition(":")
            host = host.strip("[]")
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return address if port.isdecimal() and 0 < int(port) < 65536 else None


def check_egress(mirror, sha, egress, variables, scratch):
    """The admission variables and the canary are in the env file, and the repository's own verify script (run from
    the pinned source) prints `ok`: the host firewall rules are in force. Nothing is edited."""
    blocked = [item for item in variables.get(egress["admission_env"]["blocked"], "").split(",") if item]
    opened = [item for item in variables.get(egress["admission_env"]["open"], "").split(",") if item]
    if egress["canary"] not in blocked or not set(egress["open"]) <= set(opened):
        raise EnrollRefused("egress_env_missing", "the env file lacks the canary or an open target")
    covered = set()
    for item in blocked:
        if item == egress["canary"]:
            continue
        address = blocked_address(item)
        if address is None:
            raise EnrollRefused("egress_target_invalid", f"{item} is not an IP:PORT or dns:IP blocked target")
        if not item.startswith("dns:"):
            covered |= {name for name, networks in EGRESS_CLASSES.items()
                        if any(address in ipaddress.ip_network(network) for network in networks)}
    if missing := sorted(set(egress["blocked"]) - covered):
        raise EnrollRefused("egress_blocked_incomplete", "no blocked target (with a known listener) of class " + ", ".join(missing))
    resolver = variables.get(egress["controls"]["dns_env"], "")
    if f"dns:{resolver}" not in opened:
        raise EnrollRefused("egress_control_missing", f"the open targets lack dns:<{egress['controls']['dns_env']}>")
    archive = p.run(["git", "-C", str(mirror), "archive", "--format=tar", sha, "scripts"], binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(scratch, filter="data")
    if unset := [key for key in egress["verify_env"] if not variables.get(key)]:
        raise EnrollRefused("egress_env_missing", "the env file lacks " + ", ".join(unset))
    # The verifier reads its inputs from `--env-file`, not from the environment: it gets a private file holding exactly
    # the values this plan checked.
    verify_env = Path(scratch) / "verify.env"
    with os.fdopen(p.new_file(verify_env), "w") as handle:
        handle.write("".join(f"{key}={variables[key]}\n" for key in egress["verify_env"]))
    try:
        shown = p.run([sys.executable, "-I", str(Path(scratch) / egress["verify"][0]), *egress["verify"][1:], "--env-file",
                     str(verify_env)], env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, timeout=120)
    except (p.Refused, subprocess.TimeoutExpired):
        raise EnrollRefused("egress_verify_failed", "the verify script exited with an error or timed out") from None
    lines = shown.strip().splitlines()
    if not lines or lines[-1].strip() != "ok":
        raise EnrollRefused("egress_verify_failed", (lines[-1] if lines else "no output")[:200])


# ANYM-746: `enroll --apply` and `enroll --undo` (production.md, "Enrollment"). The plan is ANYM-745's; this block performs
# exactly the plan whose digest the caller names, through the release machinery `deploy` uses, and recovers what it changed.
ENROLL_RECEIPT = "enrollment.json"
ENROLL_HEALTH_SECONDS, ENROLL_HEALTH_POLL = 240, 3
GATEWAY_SERVICE = re.compile(r"gateway|http-gateway-[a-z0-9-]+")
# Run inside a gateway container: connect to its own backend address (the positive control of the isolation probe).
CONNECT_CODE = "import socket,sys;socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=3).close()"


def run_input(argv, data, *, timeout=300):
    """`argv` with `data` (bytes) on stdin; its stdout. As `run`, nothing of the output goes into an exception."""
    result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise p.Refused(f"{Path(argv[0]).name} failed (exit {result.returncode}); production was not accepted")
    return result.stdout.decode(errors="replace")


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_file(path, content, uid, gid, mode):
    """Atomically replace `path` with `content` (bytes) of the given owner and mode, fsynced, and read it back."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.enroll")
    with suppress(FileNotFoundError):
        temporary.unlink()
    try:
        with os.fdopen(p.new_file(temporary), "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(content).hexdigest():
        raise p.Refused(f"{path} does not read back as written")


def replace_like(path, content):
    """`replace_file` with the owner and mode `path` has now."""
    info = os.stat(path)
    p.replace_file(path, content, info.st_uid, info.st_gid, info.st_mode & 0o777)


def enroll_calls(settings):
    """Refuses with `active_call` (`calls_unknown` when the calls database cannot be read) while a call may run; every
    mutation of an enrollment is preceded by it (ANYM-746 E3)."""
    try:
        active = p.active_calls(settings)
    except p.CallsUnknown as exc:
        raise EnrollRefused("calls_unknown", str(exc)[:200]) from None
    if active:
        raise EnrollRefused("active_call", f"{len(active)} active call(s)")


def settle_halt(reason, status="halted", *, code, **fields):
    # An enrollment stops on its own receipt and pause; the code describes only autodeploy's pause file.
    return {"status": status, "reason": reason, **fields}


def settle_finish(status, **fields):
    return {"status": status, **fields}


def settle_retry(exc, message):
    return settle_finish("error", reason=message + "; nothing changed")


class Enrollment:
    """One `enroll --apply` run under the host locks and the backup wait. `receipt` is `enrollment.json`, rewritten after
    every step (digests, revisions and step names only)."""

    def __init__(self, settings_path, http_path, module, sha, expected, mirror):
        self.paths = {"production": str(settings_path), "http": str(http_path)}
        self.module, self.sha, self.expected, self.mirror = module, sha, expected, mirror
        self.settings = p.config(settings_path)
        self.s = p.http_release_tool()
        self.http = self.s.validate_settings(p.config(http_path))
        self.state, self.http_state = Path(self.settings["state_directory"]), Path(self.http["state_directory"])
        self.path, self.receipt, self.marker = self.state / ENROLL_RECEIPT, None, None

    # -- receipt
    def save(self, **fields):
        self.receipt.update(fields)
        p.write(self.path, self.receipt)

    def step(self, name):
        self.receipt["steps"].append(name)
        self.save()

    def reload(self):
        self.settings = p.config(self.paths["production"])
        self.http = self.s.validate_settings(p.config(self.paths["http"]))

    # -- before the first write
    def check(self):
        if self.path.exists() and p.read(self.path).get("status") != "undone":
            raise EnrollRefused("enrollment_unfinished", f"{self.path} holds an enrollment that is not undone; inspect it")
        self.plan = p.enroll_plan(self.settings, self.http, self.module, self.sha, self.mirror)
        if self.plan["plan_digest"] != self.expected:
            raise EnrollRefused("plan_changed", "the recomputed plan digest differs from --plan-digest; plan again and review it")
        self.manifest, blob = read_manifest(self.mirror, self.sha, self.module)
        if blob != self.plan["manifest_blob"]:
            raise EnrollRefused("plan_changed", "the manifest changed since the plan")
        if reason := p.unresolved(self.state, self.http_state):
            raise EnrollRefused("enrollment_unfinished", reason)
        current = p.read(self.state / "current.json") if (self.state / "current.json").exists() else {}
        serving = p.read(self.http_state / "http-current.json") if (self.http_state / "http-current.json").exists() else {}
        if current.get("status") != "applied" or serving.get("sha") != current.get("sha"):
            raise EnrollRefused("no_current_release", "plan A and plan B do not serve the same applied release")
        if drift := p.configuration_drift(self.settings, self.http, current, serving):
            raise EnrollRefused("configuration_changed", drift)
        # The undeclared external networks are `enroll_plan`'s refusal, on the document only it holds: `--plan` prints
        # its result, and a resolved document carries env values.
        self.check_networks()
        self.base = current["sha"]

    def begin(self):
        """The pause (this run's marker), the receipt and the copies; from here a failure keeps the pause."""
        body = json.dumps({"status": "enrolling", "at": p.now(), "reason": f"enroll {self.module} at {self.sha[:12]} "
                           f"is running or stopped; inspect {self.path}"}) + "\n"
        fd = os.open(p.PAUSE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
        self.marker = body
        self.receipt = {"version": 1, "module": self.module, "sha": self.sha, "plan_digest": self.expected,
                        "status": "running", "started_at": p.now(), "steps": [], "files": {},
                        "project": self.plan["production_json"]["append_project"]}
        self.save()

    def runtime_images(self):
        """The image identity of each selected service of the projects that ran before the enrollment (plan A rolls
        these back); `before_images` of the receipt holds the pre-run answer."""
        before = self.receipt.get("before_images")
        return {project["name"]: {service: identity["image"] for service, identity in p.snapshot(project)[1].items()}
                for project in self.settings["projects"] if before is None or project["name"] in before}

    def backup(self):
        self.step("backup")
        directory = self.state / f"enrollment-{self.module}-{self.sha[:12]}"
        directory.mkdir(mode=0o700)
        self.save(before_images=self.runtime_images())
        # The gateway project is the one the enrollment edits: plan A's own snapshots are taken from the edited files, so
        # the pre-run resolved configuration is kept here, with the images that run now, to put the gateway back.
        gateway = next(project for project in self.settings["projects"] if "gateway" in project["services"].values())
        p.check_env_references()
        document, running = p.snapshot(gateway)
        before = p.evidence_document(gateway, document)[0]
        for service in before["services"].values():
            service.pop("build", None)
        for service, identity in running.items():
            before["services"][service]["image"] = identity["image"]
        path = directory / "gateway.pre-run.json"
        with os.fdopen(p.new_file(path), "w") as handle:
            json.dump(p.escaped_rendered(before), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Read back the way `restore_gateway` will: exactly the pre-run resolved configuration with today's images.
        expected = copy.deepcopy(document)
        for service in expected["services"].values():
            service.pop("build", None)
        for service, identity in running.items():
            expected["services"][service]["image"] = identity["image"]
        if json.loads(p.run(["docker", "compose", "--profile", "*", "-f", str(path), "config", "--format", "json"])) != expected:
            raise p.Refused("the pre-run Compose snapshot did not round-trip exactly")
        self.receipt["pre_run"] = {"project": gateway["name"], "file": str(path),
                                   "images": {service: identity["image"] for service, identity in running.items()}}
        sources = {"production": self.paths["production"], "http": self.paths["http"],
                   "policy": self.http["http_release"]["gateway_policy"]}
        for name, source in sources.items():
            content = Path(source).read_bytes()
            target = directory / name
            with os.fdopen(p.new_file(target), "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self.receipt["files"][name] = {"path": str(source), "backup": str(target),
                                           "before_sha256": hashlib.sha256(content).hexdigest()}
        self.save()

    def original(self, name):
        item = self.receipt["files"][name]
        content = Path(item["backup"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != item["before_sha256"]:
            raise p.Refused(f"the copy of {item['path']} was modified")
        return content

    # -- mutations
    def overlays(self):
        self.step("overlays")
        for item in self.plan["overlays"]:
            content = p.repo_file(self.mirror, self.sha, item["repo_path"])
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise EnrollRefused("plan_changed", f"{item['repo_path']} differs from the plan")
            target = Path(item["host_path"])
            if os.path.lexists(target):
                if target.is_symlink() or file_sha256(target) != item["sha256"]:
                    raise EnrollRefused("overlay_conflict", f"{target} exists with other content")
                continue
            p.replace_file(target, content, item["owner"], item["group"], int(item["mode"], 8))

    def start_document(self):
        project = self.plan["production_json"]["append_project"]
        return project, compose_document(project, project["directory"], project["files"])

    def check_networks(self):
        """Read-only: refuses a declared network that exists and is not internal; the declared networks still missing."""
        missing = []
        for item in self.manifest["project"].get("networks", []):
            name = item["name"]
            if not p.run(["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"]).split():
                missing.append(item)
            elif p.run(["docker", "network", "inspect", "--format", "{{.Internal}}", name]).strip() != "true":
                raise EnrollRefused("network_unsafe", f"{name} exists but is not internal")
        return missing

    def networks(self, document):
        """Creates the missing networks. The same reads as `check()` run again on the installed files: a network that
        turned unsafe since then stops the run here, under the pause."""
        check_external_networks(self.manifest["project"], document)
        for item in self.check_networks():
            p.run(["docker", "network", "create", "--internal", "-o", f"com.docker.network.bridge.name={item['bridge']}",
                 "-o", "com.docker.network.bridge.gateway_mode_ipv4=isolated", item["name"]])

    def tag_images(self):
        for target in sorted(set(self.manifest["project"]["services"].values())):
            verified = p.published(target, self.sha)
            _, dockerfile, _, _ = p.BUILDS[target]
            _, recipe = p.build_recipe(target, self.sha)
            p.run(["docker", "pull", "--quiet", verified["reference"]])
            image = p.run(["docker", "image", "inspect", "--format", "{{.Id}}", verified["reference"]]).strip()
            if image not in (verified["digest"], verified["config_digest"]) or not p.names_revision(
                    image, self.sha, dockerfile, recipe):
                raise p.Refused(f"the pulled {target} image is not the verified image of {self.sha[:12]}")
            tag = f"deedkit-{target}:enroll-{self.sha}"
            existing = p.run(["docker", "image", "ls", "--quiet", "--no-trunc", tag]).split()
            if existing and set(existing) != {image}:
                raise p.Refused(f"{tag} names another image")
            if not existing:
                p.run(["docker", "tag", verified["reference"], tag])
                if p.run(["docker", "image", "ls", "--quiet", "--no-trunc", tag]).split() != [image]:
                    raise p.Refused(f"{tag} does not name the verified image")

    def start_checks(self, document, project):
        """Immediately before `up`: the private configuration holds no forbidden key and the admission is `required`."""
        check_private_config(document, self.manifest["project"], self.manifest["private_config"])
        if self.manifest["egress"]["outside"]:
            values = [str(value) for service in project["services"]
                      for key, value in (document["services"].get(service, {}).get("environment") or {}).items()
                      if key.endswith("_EGRESS_ADMISSION")]
            if not values or set(values) != {"required"}:
                raise EnrollRefused("egress_admission_not_required", "the resolved Compose configuration must set the admission to required")

    def health(self, project):
        deadline = time.monotonic() + p.ENROLL_HEALTH_SECONDS
        for service in project["services"]:
            while True:
                shown = p.run(["docker", "ps", "--filter", "label=com.docker.compose.project=" + project["name"], "--filter",
                             "label=com.docker.compose.service=" + service, "--format", "{{.Status}}"]).strip().splitlines()
                if len(shown) == 1 and "(healthy)" in shown[0]:
                    break
                if time.monotonic() >= deadline or any("(unhealthy)" in line for line in shown):
                    raise p.Refused(f"{service} is not healthy")
                time.sleep(p.ENROLL_HEALTH_POLL)

    def confinement(self, project):
        """The confinement check from inside the container, and each blocked target's positive control from the host."""
        egress = self.manifest["egress"]
        if not egress["outside"]:
            return
        variables, _ = p.read_env_file(project["env_files"][0])
        script = p.repo_file(self.mirror, self.sha, egress["verify"][0])
        blocked = [item for item in variables[egress["admission_env"]["blocked"]].split(",") if item]
        inside = []
        for target in egress["open"]:
            inside += ["--open", target]
        inside += ["--open-dns", variables[egress["controls"]["dns_env"]]]
        for target in blocked:
            inside += ["--blocked-dns", target[4:]] if target.startswith("dns:") else ["--blocked", target]
        controls = [item for item in blocked if item != egress["canary"] and not item.startswith("dns:")]
        service = next(iter(project["services"]))
        try:
            reports = [json.loads(p.run_input(p.compose(project) + ["exec", "-T", service, "python", "-I", "-", "check", *inside], script))]
            if controls:
                reports.append(json.loads(p.run_input([sys.executable, "-I", "-", "check", *[a for t in controls for a in ("--control", t)]], script)))
            passed = all(report.get("passed") is True for report in reports)
        except (p.Refused, ValueError, AttributeError, subprocess.TimeoutExpired):
            passed = False
        if not passed:
            raise EnrollRefused("confinement_check_failed", "the egress confinement or a positive control failed")

    def first_start(self):
        self.calls()
        self.step("first_start")
        project, document = self.start_document()
        self.networks(document)
        self.tag_images()
        project, document = self.start_document()  # the files and the private configuration as they are now
        self.start_checks(document, project)
        self.receipt["started"] = True
        self.save()
        p.run(p.compose(project) + ["up", "-d", "--no-build", "--pull", "never", "--no-deps", *project["services"]],
            env={**os.environ, "DEEDKIT_VERSION": f"enroll-{self.sha}"})
        self.health(project)
        self.confinement(project)

    def calls(self):
        enroll_calls(self.settings)

    def edit(self):
        self.calls()
        self.step("configuration")
        gateway_files = {"production": self.plan["production_json"]["gateway_project_files"],
                         "http": self.plan["http_production_json"]["gateway_project_files"]}
        for name in ("production", "http"):
            item = self.receipt["files"][name]
            raw = Path(item["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["before_sha256"]:
                raise EnrollRefused("plan_changed", f"{item['path']} changed after the copy")
            value = json.loads(raw)
            gateway = next(project for project in value["projects"] if "gateway" in project["services"].values())
            gateway["files"] = gateway_files[name]
            if name == "production":
                value["projects"].append(self.plan["production_json"]["append_project"])
            content = (json.dumps(value, indent=2) + "\n").encode()
            item["after_sha256"] = hashlib.sha256(content).hexdigest()  # recorded first: a failed write is restored too
            self.save()
            replace_like(item["path"], content)
        self.step("policy")
        item = self.receipt["files"]["policy"]
        raw = Path(item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["before_sha256"]:
            raise EnrollRefused("plan_changed", "the live policy changed after the copy")
        fragment = self.manifest["gateway"]["policy"]
        candidate, _ = p.candidate_policy(json.loads(raw), self.manifest,
                                        {principal: key_digest(self.manifest, principal) for principal in fragment["principals"]})
        if p.digest(candidate) != self.plan["policy"]["candidate_digest"]:
            raise EnrollRefused("plan_changed", "the candidate policy differs from the plan")
        with tempfile.TemporaryDirectory(prefix="enroll-") as scratch:
            os.chmod(scratch, 0o700)
            check_policy_candidate(candidate, scratch)
        content = (json.dumps(candidate, indent=2) + "\n").encode()
        item.update(after_sha256=hashlib.sha256(content).hexdigest(), revision=candidate["revision"])
        self.save()
        replace_like(item["path"], content)
        self.reload()

    def release(self):
        """Plan A through the wrapper `deploy` uses (health wait and automatic rollback), then plan B."""
        self.calls()
        self.step("plan_a")
        release = self.state / self.sha
        halted = p.deploy_plan_a(self.settings, self.http, None, self.mirror, self.base, {"sha": self.base}, self.sha, release,
                               0, 0, settle_halt, settle_finish, settle_retry)
        if halted is not None:
            raise EnrollStop(halted.get("reason", "plan A did not finish"), halted)
        self.step("plan_b")
        halted, served = p.deploy_plan_b(self.http, self.s, self.http_state, self.sha, settle_halt, settle_finish, settle_retry)
        if halted is not None:
            raise EnrollStop(halted.get("reason", "plan B did not finish"), halted)
        self.step("status")
        self.save(http=public_http(self.s.status(self.http, self.sha)))

    def isolation(self):
        """The isolation step: any failure of it, a negative probe or an error reading Docker, is `isolation_check_failed`."""
        self.step("isolation")
        try:
            self.probe_isolation()
        except EnrollRefused as exc:
            raise exc if exc.code == "isolation_check_failed" else EnrollRefused("isolation_check_failed", exc.code) from None
        except Exception as exc:
            raise EnrollRefused("isolation_check_failed", type(exc).__name__) from None

    def probe_isolation(self):
        """After promotion: every serving gateway generation on a gateway-joined module network cannot reach the module's
        backend address (probed only, never written to a configuration)."""
        project = self.plan["production_json"]["append_project"]
        egress, service = self.manifest["egress"], next(iter(project["services"]))
        script = p.repo_file(self.mirror, self.sha, egress["verify"][0]) if egress["outside"] else None
        for network in (item for item in self.manifest["project"].get("networks", []) if "gateway_port" in item):
            shown = p.run(["docker", "ps", "--filter", "network=" + network["name"], "--format",
                         '{{.ID}}\t{{.Label "com.docker.compose.service"}}']).splitlines()
            gateways = [line.split("\t")[0] for line in shown if GATEWAY_SERVICE.fullmatch(line.partition("\t")[2])]
            if not gateways:
                raise EnrollRefused("isolation_check_failed", f"no serving gateway is on {network['name']}")
            for container in gateways:
                address = ipaddress.ip_address(p.run(["docker", "inspect", "--format", '{{(index .NetworkSettings.Networks "'
                                                    + network["name"] + '").IPAddress}}', container]).strip())
                target = f"{address}:{network['gateway_port']}"
                try:
                    p.run(["docker", "exec", container, "python", "-I", "-c", CONNECT_CODE, str(address), str(network["gateway_port"])])
                    shown = json.loads(p.run_input(p.compose(project) + ["exec", "-T", service, "python", "-I", "-", "check",
                                                                     "--blocked", target], script or b""))
                    isolated = shown.get("passed") is True
                except (p.Refused, ValueError, AttributeError, subprocess.TimeoutExpired):
                    isolated = False
                if not isolated:
                    raise EnrollRefused("isolation_check_failed", f"the module can reach a gateway on {network['name']}")
        with tempfile.TemporaryDirectory(prefix="enroll-") as scratch:
            os.chmod(scratch, 0o700)
            check_policy_candidate(json.loads(Path(self.receipt["files"]["policy"]["path"]).read_bytes()), scratch)

    def complete(self):
        self.step("checks")
        current = p.read(self.state / "current.json") if (self.state / "current.json").exists() else {}
        serving = p.read(self.http_state / "http-current.json") if (self.http_state / "http-current.json").exists() else {}
        if current.get("sha") != self.sha or current.get("status") != "applied" or serving.get("sha") != self.sha:
            raise p.Refused("plan A or plan B does not report the new release as current")
        if reason := p.unresolved(self.state, self.http_state):
            raise p.Refused(reason)
        if drift := p.configuration_drift(self.settings, self.http, current, serving):
            raise p.Refused(drift)
        if not p.PAUSE.exists() or p.PAUSE.read_text() != self.marker:
            raise p.Refused("the pause is not the one this run wrote; it stays")
        self.step("complete")
        self.save(status="applied", finished_at=p.now())
        p.PAUSE.unlink()

    # -- the run
    def apply(self):
        self.check()
        self.begin()
        try:
            self.backup()
            self.overlays()
            self.first_start()
            self.edit()
            self.release()
            self.isolation()
            self.complete()
        except Exception as exc:
            return self.stopped(exc)
        return {"status": "enrolled", "module": self.module, "sha": self.sha, "steps": self.receipt["steps"],
                "receipt": str(self.path), "undo": f"production.py enroll --module {self.module} --undo"}

    def promoted(self):
        path = self.http_state / self.sha / "http-attempted.json"
        try:
            return path.exists() and p.read(path).get("status") != "startup-failed"
        except (OSError, ValueError):
            return True

    def restore(self, name, revision=None):
        """Put the original bytes of a file back; the policy gets a revision above anything a gateway may have loaded."""
        item = self.receipt["files"][name]
        content = self.original(name)
        if file_sha256(item["path"]) == item["before_sha256"]:
            return
        if name == "policy":
            value = json.loads(content)
            value["revision"] = max(value["revision"], revision or 0) + 1
            content = (json.dumps(value, indent=2) + "\n").encode()
        replace_like(item["path"], content)

    def restore_gateway(self):
        """Recreate the gateway services from the pre-run resolved configuration and image (the way `rollback` restores
        a project), without a call running."""
        item = self.receipt["pre_run"]
        command = ["docker", "compose", "--profile", "*", "--project-name", item["project"], "-f", item["file"]]
        p.wait_for_calls(self.settings, time.monotonic() + p.CALL_WAIT)
        p.run(command + ["stop", *item["images"]])
        p.run(command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "--wait", "--wait-timeout", "180",
                       *item["images"]])

    def stopped(self, exc):
        message = str(exc) if isinstance(exc, p.Refused) else type(exc).__name__
        code = getattr(exc, "code", None)
        step = self.receipt["steps"][-1] if self.receipt["steps"] else "begin"
        self.save(failed_step=step, reason=message, **({"code": code} if code else {}))
        undo = f"production.py enroll --module {self.module} --undo"
        try:
            if self.promoted():
                if step == "isolation" and self.receipt.get("started"):
                    project = self.plan["production_json"]["append_project"]
                    p.run(p.compose(project) + ["stop", *project["services"]])  # the module only, never the gateway
                try:
                    http = public_http(self.s.status(self.http, self.sha))
                except Exception as again:
                    http = {"status": "unreadable: " + type(again).__name__}
                self.save(status="http-manual", http=http)
                return self.result("stopped", message, code, "no further change was made; the pause and the module stay as they are; "
                                   "HTTP recovery is manual, inspect it (seamless-release.md)")
            release = self.state / self.sha
            attempted = (release / "attempted.json").exists()
            outcome = p.read(release / "result.json") if (release / "result.json").exists() else {}
            if attempted and outcome.get("status") == "applied":
                p.rollback(self.settings, self.sha, self.http)
            elif attempted and not (outcome.get("status") == "unhealthy" and (release / "rollback.json").exists()):
                self.save(status="unknown")
                return self.result("stopped", message, code, "production state is unknown, inspect it; nothing more was changed")
            if attempted:
                # The rollback snapshots were prepared from the edited configuration: the gateway is put back from the
                # pre-run one, and every existing project must run its pre-run image.
                self.restore_gateway()
                if self.runtime_images() != self.receipt["before_images"]:
                    raise p.Refused("an existing project does not run its pre-run image after the rollback")
            files = self.receipt["files"]
            if "after_sha256" in files.get("policy", {}):
                self.restore("policy", files["policy"].get("revision"))
            for name in ("production", "http"):
                if "after_sha256" in files.get(name, {}):
                    self.restore(name)
            if self.receipt.get("started"):
                project = self.plan["production_json"]["append_project"]
                p.run(p.compose(project) + ["stop", *project["services"]])
        except Exception as again:
            self.save(status="unknown", recovery_error=str(again) if isinstance(again, p.Refused) else type(again).__name__)
            return self.result("stopped", message, code, "recovery failed; production state is unknown, inspect it")
        self.save(status="stopped")
        return self.result("stopped", message, code, f"the module was stopped and the configuration restored; the pause stays; undo is not needed ({undo} refuses)"
                           + ("; the gateway was recreated from its pre-run configuration"
                              if (self.state / self.sha / "attempted.json").exists() else ""))

    def result(self, status, message, code, recovery):
        return {"status": status, "module": self.module, "sha": self.sha, "failed_step": self.receipt.get("failed_step"),
                **({"code": code} if code else {}), "reason": message, "recovery": recovery, "receipt": str(self.path)}


class EnrollStop(p.Refused):
    """A plan A or plan B wrapper ended without finishing; `outcome` is what it returned."""

    def __init__(self, message, outcome):
        super().__init__(message)
        self.outcome = outcome
        self.code = "active_call" if "call_waits" in outcome else None


def public_http(value):
    return {key: value[key] for key in ("sha", "generation", "status", "retirement_reason") if isinstance(value, dict) and key in value}


def enroll_apply(settings_path, http_path, module, sha, expected, mirror):
    """`enroll --apply`: the plan named by `expected`, performed under the host locks and the backup wait."""
    enrollment = Enrollment(settings_path, http_path, module, sha, expected, mirror)
    with ExitStack() as held:
        held.enter_context(p.host_locks(enrollment.settings, enrollment.http, enrollment.s))
        held.enter_context(p.backup_gate(enrollment.settings, p.backup_wait(enrollment.settings)))
        return enrollment.apply()


def enroll_undo(settings_path, http_path, module):
    """`enroll --undo`: the configuration files byte for byte, a policy of a higher revision without the module, the
    module's services stopped, autodeploy left paused. Nothing is removed."""
    settings, s = p.config(settings_path), p.http_release_tool()
    http = s.validate_settings(p.config(http_path))
    state = Path(settings["state_directory"])
    path = state / ENROLL_RECEIPT
    with p.host_locks(settings, http, s), p.backup_gate(settings, p.backup_wait(settings)):
        receipt = p.read(path) if path.exists() else {}
        if receipt.get("module") != module or receipt.get("status") != "applied":
            raise EnrollRefused("enrollment_not_applied", f"{path} holds no applied enrollment of {module}")
        enroll_calls(settings)
        files = receipt["files"]
        for name in ("production", "http", "policy"):
            if file_sha256(files[name]["path"]) != files[name]["after_sha256"]:
                raise EnrollRefused("configuration_changed" if name != "policy" else "policy_changed",
                                    f"{files[name]['path']} changed since the enrollment; undo it by hand")
            if file_sha256(files[name]["backup"]) != files[name]["before_sha256"]:
                raise EnrollRefused("configuration_changed", f"the copy of {files[name]['path']} was modified")
        if not p.PAUSE.exists():
            fd = os.open(p.PAUSE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"status": "enrolling", "at": p.now(), "reason": f"enroll --undo of {module}: resume "
                                         "deploys at the next newer green main SHA"}) + "\n")
        p.write(path, {**receipt, "status": "undoing", "undo_started_at": p.now()})
        try:
            live = json.loads(Path(files["policy"]["path"]).read_bytes())
            restored = json.loads(Path(files["policy"]["backup"]).read_bytes())
            restored["revision"] = live["revision"] + 1
            with tempfile.TemporaryDirectory(prefix="enroll-") as scratch:
                os.chmod(scratch, 0o700)
                check_policy_candidate(restored, scratch)
            replace_like(files["policy"]["path"], (json.dumps(restored, indent=2) + "\n").encode())
            for name in ("production", "http"):
                replace_like(files[name]["path"], Path(files[name]["backup"]).read_bytes())
            project = receipt["project"]
            p.run(p.compose(project) + ["stop", *project["services"]])
        except Exception as exc:
            reason = str(exc) if isinstance(exc, p.Refused) else type(exc).__name__
            p.write(path, {**receipt, "status": "undo-failed", "reason": reason})
            return {"status": "stopped", "module": module, "reason": reason, "receipt": str(path),
                    "recovery": "no further change was made; the pause stays; inspect the files and the receipt"}
        p.write(path, {**receipt, "status": "undone", "undone_at": p.now()})
    return {"status": "undone", "module": module, "receipt": str(path),
            "policy_revision": restored["revision"],
            "note": "autodeploy stays paused: a SHA prepared with the module can never be prepared again; remove the pause "
                    "when a newer green main SHA exists (production.md, Undo)"}


def enroll_command(args):
    """`enroll --module M --plan|--apply --plan-digest D|--undo`: the plan, the enrollment or its undo, or
    `{"status": "refused", "refusals": [...]}` (exit 2) with nothing written by the refused step."""
    if [args.plan, args.apply, args.undo].count(True) != 1:
        raise p.Refused("enroll needs exactly one of --plan, --apply or --undo")
    if not args.module:
        raise p.Refused("enroll needs --module")
    if args.apply and not re.fullmatch(r"[0-9a-f]{64}", args.plan_digest or ""):
        raise p.Refused("enroll --apply needs --plan-digest, the plan_digest of the plan a reviewer approved")
    if args.plan_digest and not args.apply:
        raise p.Refused("--plan-digest belongs to --apply")
    try:
        if args.undo:
            return enroll_undo(args.config, args.http_config, args.module)
        settings = p.config(args.config)
        mirror = p.main_history(Path(settings["state_directory"]))
        if args.apply:
            return enroll_apply(args.config, args.http_config, args.module, args.sha, args.plan_digest, mirror)
        http = p.http_release_tool().validate_settings(p.config(args.http_config))
        return p.enroll_plan(settings, http, args.module, args.sha, mirror)
    except EnrollRefusals as exc:
        return {"status": "refused", "refusals": exc.failures}
    except EnrollRefused as exc:
        return {"status": "refused", "refusals": [{"code": exc.code, "detail": exc.detail}]}
    except subprocess.TimeoutExpired as exc:
        return {"status": "refused", "refusals": [{"code": "command_timeout", "detail": f"{Path(str(exc.cmd if isinstance(exc.cmd, str) else exc.cmd[0])).name} timed out"}]}


def enroll_required(module, sha):
    return f"enroll_required: {module}: production.py enroll --module {module} --sha {sha} --plan"


class ManifestInvalid(str):
    """A module name from `enrollment_pending` whose changed manifest does not parse while the module runs; `code` is the
    fixed reason (`module_manifests`). Enrolling cannot help it."""
    code: str

    def __new__(cls, module, code):
        value = super().__new__(cls, module)
        value.code = code
        return value


def enrollment_halt(module, sha):
    """The pause text and its extra fields for one module of `enrollment_pending`."""
    if isinstance(module, ManifestInvalid):
        return f"manifest_invalid: {module}: fix {MODULE_DIRECTORY}{module}.json; enroll is not needed", {"manifest_code": module.code}
    return p.enroll_required(module, sha), {}


MANIFEST_FILE = re.compile(MODULE_DIRECTORY + r"[a-z][a-z0-9-]*\.json")


def module_manifests(mirror, sha, only=None):
    """`{module: manifest or None}` for the deploy/modules/*.json at `sha` (those named in `only`, when given), and
    `{module: code}` for each None: the `EnrollRefused` code or `invalid`, never a word of the file. A manifest nobody
    asked for is not parsed."""
    listing = p.run(["git", "-C", str(mirror), "ls-tree", "-r", "--name-only", "-z", sha, MODULE_DIRECTORY]).split("\0")
    found, reasons = {}, {}
    for path in (path for path in listing if MANIFEST_FILE.fullmatch(path) and (only is None or Path(path).stem in only)):
        module = Path(path).stem
        try:
            found[module] = check_manifest(json.loads(p.repo_file(mirror, sha, path)), module)
        except EnrollRefused as exc:
            found[module], reasons[module] = None, exc.code
        except (ValueError, p.Refused):
            found[module], reasons[module] = None, "invalid"
    return found, reasons


def enrollment_pending(mirror, base, head, settings):
    """The modules whose manifest `base..head` adds or changes while the configuration runs no project of it. A changed
    manifest that does not parse is pending too, unless `base`'s manifest of that module parses and names a running
    project: then the module comes back as `ManifestInvalid`. `base` is read only for a broken manifest."""
    changed = p.run(["git", "-C", str(mirror), "diff", "--name-only", "--no-renames", "-z", base, head]).split("\0")
    wanted = {Path(path).stem for path in changed if MANIFEST_FILE.fullmatch(path)}
    running = {project["name"] for project in settings["projects"]}
    manifests, reasons = module_manifests(mirror, head, wanted) if wanted else ({}, {})
    before = module_manifests(mirror, base, reasons.keys())[0] if reasons else {}
    return sorted(ManifestInvalid(module, reasons[module])
                  if module in reasons and before.get(module) and before[module]["project"]["name"] in running else module
                  for module in wanted & manifests.keys()
                  if manifests[module] is None or manifests[module]["project"]["name"] not in running)


def without_enrollments(settings, manifests):
    """`settings` with the edits of each enrolled module's manifest reversed: its project and its gateway overlay."""
    names = {manifest["project"]["name"] for manifest in manifests}
    overlays = {Path(manifest["gateway"]["overlay"]).name for manifest in manifests if manifest["gateway"]["overlay"]}
    reverted = copy.deepcopy(settings)
    reverted["projects"] = [{**project, "files": [path for path in project["files"] if Path(path).name not in overlays]}
                            for project in reverted["projects"] if project["name"] not in names]
    return reverted


def enrollment_only(mirror, head, settings, http, current, serving):
    """The modules whose enrollment alone explains why the configuration differs from the recorded digests: reversing
    every enrolled manifest of `head` reproduces both. Empty when something else differs too."""
    running = {project["name"] for project in settings["projects"]}
    manifests = [manifest for manifest in module_manifests(mirror, head)[0].values()
                 if manifest is not None and manifest["project"]["name"] in running]
    if not manifests:
        return []
    prepared = p.read(Path(settings["state_directory"]) / current["sha"] / "prepared.json").get("config_digest")
    if (p.settings_digest(without_enrollments(settings, manifests)) != prepared
            or p.digest(without_enrollments(http, manifests)) != serving.get("config_digest")):
        return []
    return sorted(manifest["name"] for manifest in manifests)



# The first start of a brand-new session holder (whatsapp-native.md, Session holder cutover, step 5): prepare reads the
# container of every selected service, so a holder that never ran has none and each prepare refuses ("exactly one running
# container"). `create-holder` creates that container from the verified main commit's holder image and starts nothing:
# no provider session begins here. The holder's first process start is the pair step of the next apply, which recreates a
# holder that is not running at once (ANYM-896, kind `recovery`, reason `stopped`), its collector stopped first.
def create_holder(settings, service, sha):
    """Create, never start, the one container of the session holder `service` on the image `prepare --sha sha` builds
    (the same tag, reused there). Refused unless autodeploy is paused, the configuration marks `service` as a session
    holder, its Compose files define it and it has no container at all. Returns the container, its image and the undo."""
    if not p.PAUSE.exists():
        raise p.Refused("create-holder runs only under the autodeploy pause (whatsapp-native.md, Session holder cutover)")
    project = next((item for item in settings["projects"] if service in (item.get("sessions") or {})), None)
    if project is None:
        raise p.Refused(f"{service} is not a session holder in the production configuration: add its services entry first")
    if service not in json.loads(p.run(p.compose(project) + ["config", "--format", "json"]))["services"]:
        raise p.Refused(f"the Compose files of {project['name']} define no {service}: add its overlay to files first")
    def containers():
        return p.run(["docker", "ps", "--all", "--filter", "label=com.docker.compose.oneoff=False",
                      "--filter", "label=com.docker.compose.project=" + project["name"],
                      "--filter", "label=com.docker.compose.service=" + service, "--format", "{{.ID}}"]).split()
    if containers():
        raise p.Refused(f"{project['name']}/{service} already has a container; prepare reads it as it is")
    state = Path(settings["state_directory"])
    p.check_host_space(state)
    docker_root = p.run(["docker", "info", "--format", "{{.DockerRootDir}}"]).strip()
    if not docker_root or not Path(docker_root).is_dir():
        raise p.Refused("create-holder requires the local production Docker engine")
    p.check_image_space(docker_root, p.containerd_image_store(), state)
    p.promotable(sha)
    scratch = Path(tempfile.mkdtemp(prefix=".create-holder-", dir=state))
    try:
        source = scratch / "source"  # as prepare fetches it: the verified commit, public modes for the image build
        p.run(["git", "init", "--quiet", str(source)])
        p.run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/" + p.REPOSITORY + ".git"])
        p.run(["git", "-C", str(source), "fetch", "--depth=1", "origin", sha])
        if p.run(["git", "-C", str(source), "rev-parse", "FETCH_HEAD"]).strip() != sha:
            raise p.Refused("fetched source is not the verified candidate commit")
        p.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", sha], umask=0o022)
        image = p.build_image(source, sha, project["services"][service])
        override = scratch / "image.json"
        override.write_text(json.dumps({"services": {service: {"image": image}}}))
        p.run(p.compose(project) + ["-f", str(override), "up", "--no-start", "--no-deps", "--no-build", "--pull", "never",
                                    service])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    undo = shlex.join(p.compose(project) + ["rm", "--stop", service])  # file and env-file paths only, never a value
    ids = containers()
    actual = json.loads(p.run(["docker", "inspect", ids[0]]))[0] if len(ids) == 1 else {}
    if actual.get("Image") != image or (actual.get("State") or {}).get("Status") != "created":
        raise p.Refused(f"{project['name']}/{service} is not one created container on {image}; inspect it, undo: {undo}")
    return {"status": "created", "project": project["name"], "service": service, "container": ids[0], "image": image,
            "sha": sha, "undo": undo}
