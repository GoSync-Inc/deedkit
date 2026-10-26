"""`agent_api.py --check-registry`: the admin CLI validates a candidate in the running calls image (ANYM-496)."""
import hashlib
import json

from agent_api import check_registry

DIGEST = hashlib.sha256(b"synthetic-calls-key").hexdigest()


def registry(path, **extra):
    entry = {"id": "synthetic-agent", "key_sha256": DIGEST, "enabled": True,
             "expires_at": "2099-01-01T00:00:00Z", "scopes": ["calls:read"], **extra}
    path.write_text(json.dumps({"version": 1, "revision": 4, "principals": [entry]}))
    path.chmod(0o600)
    return path


# spec: ANYM-496
def test_check_registry_accepts_what_the_service_reads_and_prints_no_hash(tmp_path, capsys):
    assert check_registry(["--check-registry", str(registry(tmp_path / "agents.json"))]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"valid": True, "revision": 4, "principals": 1}
    assert DIGEST not in out


# spec: ANYM-496
def test_check_registry_refuses_an_unknown_field_an_unknown_scope_and_an_unsafe_file(tmp_path, capsys):
    unknown = registry(tmp_path / "unknown.json", newer_only_field=True)
    scope = registry(tmp_path / "scope.json", scopes=["calls:everything"])
    unsafe = registry(tmp_path / "unsafe.json")
    unsafe.chmod(0o644)
    for path in (unknown, scope, unsafe):
        assert check_registry(["--check-registry", str(path)]) == 1
        assert json.loads(capsys.readouterr().out)["valid"] is False
