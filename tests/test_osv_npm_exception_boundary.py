"""An npm advisory in osv-scanner.toml is filtered in every lockfile, so its entry holds only while each copy of
the package it names is a dev (build-only) dependency in a lockfile its reason names (ANYM-1002)."""
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _npm_entries():
    config = tomllib.loads((ROOT / "osv-scanner.toml").read_text())
    return [entry for entry in config.get("IgnoredVulns", ()) if entry["id"].startswith("GHSA-")]


# reads: packages/deedkit/package-lock.json
# reads: packages/npm/package-lock.json
# reads: services/calls/frontend/package-lock.json
# reads: whatsapp/package-lock.json
def _copies(lock, package):
    packages = json.loads((ROOT / lock).read_text()).get("packages", {})
    return {path: meta for path, meta in packages.items() if path.split("node_modules/")[-1] == package}


# spec: ANYM-137
def test_an_npm_allowlist_entry_covers_only_dev_copies_in_the_lockfiles_it_names():
    locks = subprocess.run(["git", "ls-files", "*package-lock.json"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split()
    assert locks
    for entry in _npm_entries():
        # The reason starts with the package and the version it accepts.
        package, version = entry["reason"].split()[:2]
        found = False
        for lock in locks:
            copies = _copies(lock, package)
            if not copies:
                continue
            found = True
            # A copy in a lockfile the reason does not name, a production copy or another version is not covered
            # by the reachability evidence: the scan must fail on it instead.
            assert lock in entry["reason"], (entry["id"], lock)
            for path, meta in copies.items():
                assert meta.get("dev") is True, (entry["id"], lock, path)
                assert meta.get("version") == version, (entry["id"], lock, path, meta.get("version"))
        assert found, entry["id"]


# spec: ANYM-137
def test_the_calls_image_ships_only_the_built_frontend():
    # The build-only claim of an entry for the calls frontend rests on the image copying dist, not node_modules,
    # from the node stage, and on Vite running only `vite build` with its default patterns.
    if not any("services/calls/frontend/package-lock.json" in entry["reason"] for entry in _npm_entries()):
        return
    dockerfile = (ROOT / "services/calls/Dockerfile").read_text()
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY --from=frontend")]
    assert copies == ["COPY --from=frontend /frontend/dist ./frontend/dist"]
    package = json.loads((ROOT / "services/calls/frontend/package.json").read_text())
    assert set(package["scripts"]) == {"build", "test"}
    assert package["scripts"]["build"] == "vite build"
    assert not list((ROOT / "services/calls/frontend").glob("vite.config.*"))
