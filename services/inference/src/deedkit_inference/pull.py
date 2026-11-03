"""The only download path (ANYM-683): `deedkit-inference pull`, run as the account that owns the model store.

It downloads each pinned file of a Hub repository at its pinned revision, verifies the whole phase directory exactly as
the server will, and publishes it by one rename. The service process never imports this module (a source test keeps
every network client out of the serving path); `cli.py` loads it inside the `pull` command only."""

import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .models import CHUNK, ModelError, phase_dir, verify_all, verify_artifact, verify_phase

HOST = "huggingface.co"
#: The Hub sends large files to its CDN (cdn-lfs*.huggingface.co, cas-bridge.xethub.hf.co, ...). A redirect may go
#: to these domains and their subdomains only; the sha256 pin decides what the bytes are, this decides who is asked.
REDIRECT_DOMAINS = ("huggingface.co", "hf.co")


def allowed_host(url):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in REDIRECT_DOMAINS)


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects stay on https and on the Hub's own domains."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_host(newurl):
            raise urllib.error.URLError("redirect_not_allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def hub_fetch(repo, revision, name, destination, limit):
    """Download one pinned file of a Hub repository to `destination`, at most `limit` bytes."""
    url = f"https://{HOST}/{repo}/resolve/{revision}/{name}"
    opener = urllib.request.build_opener(_HttpsRedirects)
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "deedkit-inference-pull"}), timeout=60) as reply:
        if reply.status != 200:
            raise ModelError("download_failed", name)
        total = 0
        with open(destination, "wb") as out:
            while True:
                chunk = reply.read(CHUNK)
                if not chunk:
                    return
                total += len(chunk)
                if total > limit:
                    raise ModelError("download_too_large", name)
                out.write(chunk)


def pull(models, store, *, fetch=hub_fetch, only=None, min_free_gb=15.0, log=None):
    """Download, verify and publish each pinned phase directory by one rename. Nothing is served until the whole
    directory verified; an interrupted pull leaves only a `.pull-*` directory that nothing reads."""
    log = log or (lambda _line: None)
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    for stale in store.glob(".pull-*"):
        shutil.rmtree(stale, ignore_errors=True)
    todo = []
    for key in sorted(models["phases"]):
        alias, phase = key.split("/")
        if only and key not in only and alias not in only:
            continue
        try:
            verify_phase(models, store, alias, phase)
            log(f"{key}: already verified")
        except ModelError:
            todo.append((alias, phase, models["phases"][key]))
    need = sum(f["size"] for _, _, artifacts in todo for e in artifacts.values() for f in e["files"].values())
    if todo and shutil.disk_usage(store).free < max(need * 1.2, min_free_gb * 1024**3):
        raise ModelError("disk_space_low")
    for alias, phase, artifacts in todo:
        work = store / f".pull-{uuid.uuid4().hex}"
        try:
            for name, entry in artifacts.items():
                target = work / name
                target.mkdir(parents=True)
                for file_name, pinned in entry["files"].items():
                    log(f"{alias}/{phase}/{name}: {file_name}")
                    fetch(entry["repo"], entry["revision"], file_name, target / file_name, pinned["size"])
                    os.chmod(target / file_name, 0o644)
                os.chmod(target, 0o755)
            os.chmod(work, 0o755)
            # Verify the downloaded tree exactly as the server will, from the temporary place.
            for name, entry in artifacts.items():
                verify_artifact(work / name, entry)
            final = phase_dir(store, alias, phase)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(final.parent, 0o755)
            if final.exists() or final.is_symlink():
                trash = store / f".pull-trash-{uuid.uuid4().hex}"
                os.rename(final, trash)
                os.rename(work, final)
                shutil.rmtree(trash, ignore_errors=True)
            else:
                os.rename(work, final)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return [key for key, problem in verify_all(models, store) if problem is None]
