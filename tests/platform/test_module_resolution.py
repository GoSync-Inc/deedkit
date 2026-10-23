import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "deedkit_launcher", ROOT / "scripts/deedkit.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def profile(modules, connectors=(), external=()):
    return {
        "version": 1,
        "id": "synthetic",
        "deployment": {
            "modules": list(modules),
            "connectors": list(connectors),
            "external": list(external),
        },
        "exposure": {"tools": ["platform_capabilities"]},
    }


def names(plan):
    return {m["module"] for m in plan["modules"]}


def test_calendar_and_phone_do_not_require_messaging():
    assert names(launcher.resolve(profile(["calendar"]))) == {"gateway", "calendar"}
    assert names(launcher.resolve(profile(["calls"], ["calls-phone"]))) == {
        "gateway",
        "calls",
    }
    assert names(launcher.resolve(profile(["calls"], ["calls-telegram"]))) == {
        "gateway",
        "calls",
        "messaging",
    }
    assert names(launcher.resolve(profile(["calls"], ["calls-archive"]))) == {
        "gateway",
        "calls",
        "messaging",
    }


def test_external_backend_never_starts_or_demands_provider_credentials():
    plan = launcher.resolve(
        profile(["calendar", "messaging"], external=["calendar", "messaging"])
    )
    argv = launcher.commands(plan, "up")
    assert len(argv) == 1
    assert argv[0][-3:] == ["-d", "--wait", "gateway"]
    assert all(
        not m["required_files"] for m in plan["modules"] if m["mode"] == "external"
    )
    stop = launcher.commands(plan, "stop")[0]
    assert stop[-2:] == ["stop", "gateway"]
    assert "down" not in stop and "-v" not in stop


@pytest.mark.parametrize(
    "value",
    [
        profile(["unknown"]),
        profile(["calendar"], ["calls-phone"]),
        profile(["calendar"], external=["calls"]),
        profile(["calendar", "calendar"]),
    ],
)
def test_unknown_invalid_and_duplicate_selections_fail(value):
    with pytest.raises(ValueError):
        launcher.resolve(value)


def test_dependency_cycles_fail_before_process_creation(tmp_path):
    target = tmp_path / "config/modules"
    target.mkdir(parents=True)
    for name, dependency in [("gateway", "calendar"), ("calendar", "gateway")]:
        (target / (name + ".toml")).write_text(
            f'version=1\nid="{name}"\nrequires=["{dependency}"]\n'
        )
    with pytest.raises(ValueError, match="cycle"):
        launcher.resolve(profile(["calendar"]), tmp_path)


def test_missing_or_public_secret_fails_only_for_selected_managed_module(tmp_path):
    plan = launcher.resolve(profile(["calendar"], external=["calendar"]))
    with pytest.raises(ValueError, match="gateway"):
        launcher.validate_files(plan, tmp_path)
    for relative in next(
        m["required_files"] for m in plan["modules"] if m["module"] == "gateway"
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
        path.chmod(0o600)
    launcher.validate_files(plan, tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        launcher.validate_files(plan, tmp_path)


# spec: ANYM-424
def test_gateway_preflight_checks_the_policy_file_the_gateway_reads(tmp_path):
    unit = yaml.safe_load((ROOT / "deploy/compose.gateway.yaml").read_text())["services"]["gateway"]
    policy = unit["command"][unit["command"].index("--policy") + 1]
    mounts = [v.split(":") for v in unit["volumes"] if Path(policy).parent == Path(v.split(":")[1])]
    assert len(mounts) == 1 and mounts[0][2] == "ro"
    read = str(Path("deploy") / mounts[0][0] / Path(policy).name)
    plan = launcher.resolve(profile(["calendar"], external=["calendar"]))
    gateway = next(m for m in plan["modules"] if m["module"] == "gateway")
    assert Path(read) in {Path(f) for f in gateway["required_files"]}
    for relative in gateway["required_files"]:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("synthetic")
        (tmp_path / relative).chmod(0o600)
    # Only the new layout exists: no legacy single policy file beside it.
    assert not (tmp_path / "deploy/private/gateway-policy.json").exists()
    launcher.validate_files(plan, tmp_path)
    path = tmp_path / read
    path.chmod(0o640)
    with pytest.raises(ValueError, match="gateway"):
        launcher.validate_files(plan, tmp_path)
    path.unlink()
    with pytest.raises(ValueError, match="gateway"):
        launcher.validate_files(plan, tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text("synthetic")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(ValueError, match="gateway"):
        launcher.validate_files(plan, tmp_path)
