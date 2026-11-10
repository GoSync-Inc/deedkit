"""Private command receipts, device reservations, and bounded file storage."""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .config import Refused

MAX_FILE = 256 * 1024 * 1024
MAX_STORAGE = 512 * 1024 * 1024


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise Refused("invalid_identifier", 422) from None


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.files = self.directory / "files"
        self.files.mkdir(mode=0o700, exist_ok=True)
        self.lock_fd = os.open(self.directory / "service.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            raise ValueError("state_directory_already_in_use") from None
        self.db = sqlite3.connect(self.directory / "state.sqlite3")
        (self.directory / "state.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                owner TEXT, id TEXT, fingerprint TEXT, state TEXT, result TEXT,
                PRIMARY KEY(owner, id));
            CREATE TABLE IF NOT EXISTS files (
                owner TEXT, id TEXT, complete INTEGER, PRIMARY KEY(owner, id));
            CREATE TABLE IF NOT EXISTS reservations (
                device TEXT PRIMARY KEY, owner TEXT, token TEXT, expires REAL);
        """)
        # A crashed process may already have acted on the phone. Never replay it.
        self.db.execute("UPDATE operations SET state='unknown' WHERE state='running'")
        self.db.commit()

    def close(self):
        self.db.close()
        os.close(self.lock_fd)

    def reserve(self, owner, device, action, token=None, seconds=300):
        row = self.db.execute("SELECT * FROM reservations WHERE device=?", (device,)).fetchone()
        active = row and row["expires"] > time.time()
        if action == "acquire":
            if active:
                raise Refused("device_reserved")
            token = str(uuid.uuid4())
        elif not active or row["owner"] != owner or row["token"] != token:
            raise Refused("reservation_expired_or_forbidden")
        if action == "release":
            self.db.execute("DELETE FROM reservations WHERE device=?", (device,))
            self.db.commit()
            return {"device_id": device, "released": True}
        expires = time.time() + seconds
        self.db.execute("INSERT OR REPLACE INTO reservations VALUES(?,?,?,?)", (device, owner, token, expires))
        self.db.commit()
        return {"device_id": device, "session_id": token, "expires_at": expires}

    def check_reservation(self, owner, device, token):
        row = self.db.execute("SELECT * FROM reservations WHERE device=?", (device,)).fetchone()
        if not row or row["expires"] <= time.time() or row["owner"] != owner or row["token"] != token:
            raise Refused("reservation_expired_or_forbidden")

    def operation(self, owner, request_id):
        row = self.db.execute("SELECT * FROM operations WHERE owner=? AND id=?", (owner, request_id)).fetchone()
        if row is None:
            raise Refused("not_found", 404)
        return {"request_id": request_id, "state": row["state"], **json.loads(row["result"] or "{}")}

    def admit(self, owner, request_id, payload):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM operations WHERE owner=? AND id=?", (owner, request_id)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise Refused("request_id_conflict")
            return False
        self.db.execute("INSERT INTO operations VALUES(?,?,?,'running','{}')", (owner, request_id, fingerprint))
        self.db.commit()
        return True

    def finish(self, owner, request_id, state, result):
        self.db.execute("UPDATE operations SET state=?, result=? WHERE owner=? AND id=?",
                        (state, json.dumps(result), owner, request_id))
        self.db.commit()

    def path(self, owner, file_id):
        owner_dir = self.files / hashlib.sha256(owner.encode()).hexdigest()
        owner_dir.mkdir(mode=0o700, exist_ok=True)
        return owner_dir / identifier(file_id)

    def file(self, owner, file_id, complete=True):
        file_id = identifier(file_id)
        row = self.db.execute("SELECT complete FROM files WHERE owner=? AND id=?", (owner, file_id)).fetchone()
        if row is None or (complete and not row["complete"]):
            raise Refused("file_not_found_or_incomplete", 404)
        return self.path(owner, file_id)

    def quota(self, additional):
        used = sum(p.stat().st_size for p in self.files.glob("*/*") if p.is_file())
        if used + additional > MAX_STORAGE:
            raise Refused("storage_full", 507)

    def upload(self, owner, file_id, offset, data, final):
        try:
            content = base64.b64decode(data, validate=True)
        except ValueError:
            raise Refused("invalid_base64", 422) from None
        if len(content) > 32768 or offset + len(content) > MAX_FILE:
            raise Refused("file_too_large", 413)
        path = self.path(owner, file_id)
        row = self.db.execute("SELECT complete FROM files WHERE owner=? AND id=?", (owner, file_id)).fetchone()
        if row is None:
            if offset != 0:
                raise Refused("invalid_offset")
            self.db.execute("INSERT INTO files VALUES(?,?,0)", (owner, file_id))
            self.db.commit()
        size = path.stat().st_size if path.exists() else 0
        complete = bool(row and row["complete"])
        if offset > size or (final and offset + len(content) < size):
            raise Refused("invalid_offset")
        existing = b""
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(offset)
                existing = stream.read(len(content))
        if existing != content[:len(existing)] or (complete and offset + len(content) > size):
            raise Refused("file_content_conflict")
        # Resume an identical chunk even if a crash left only its prefix on disk.
        remaining = content[len(existing):]
        self.quota(len(remaining))
        with path.open("ab") as stream:
            stream.write(remaining)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        self.db.execute("UPDATE files SET complete=? WHERE owner=? AND id=?",
                        (int(final or complete), owner, file_id))
        self.db.commit()
        return {"file_id": file_id, "size": path.stat().st_size, "complete": final or complete}

    def save(self, owner, content):
        self.quota(len(content))
        file_id = str(uuid.uuid4())
        path = self.path(owner, file_id)
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        self.db.execute("INSERT INTO files VALUES(?,?,1)", (owner, file_id))
        self.db.commit()
        return {"file_id": file_id, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    def save_path(self, owner, source):
        size = source.stat().st_size
        self.quota(size)
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        file_id = str(uuid.uuid4())
        target = self.path(owner, file_id)
        os.replace(source, target)
        target.chmod(0o600)
        self.db.execute("INSERT INTO files VALUES(?,?,1)", (owner, file_id))
        self.db.commit()
        return {"file_id": file_id, "size": size, "sha256": digest.hexdigest()}

    def delete_file(self, owner, file_id):
        # Repeating a cleanup after a lost response is safe.
        self.path(owner, file_id).unlink(missing_ok=True)
        self.db.execute("DELETE FROM files WHERE owner=? AND id=?", (owner, file_id))
        self.db.commit()
        return {"file_id": file_id, "deleted": True}
