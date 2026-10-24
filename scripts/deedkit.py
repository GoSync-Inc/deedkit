#!/usr/bin/env python3
"""Resolve explicit module deployments. Never discover/import provider code."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    with Path(path).open("rb") as source:
        return tomllib.load(source)


def resolve(profile, root=ROOT):
    if profile.get("version") != 1:
        raise ValueError("unsupported profile version")
    manifests = {}
    for path in sorted((root / "config/modules").glob("*.toml")):
        value = read(path)
        if value.get("version") != 1 or value["id"] != path.stem:
            raise ValueError("invalid module manifest")
        manifests[value["id"]] = value
    selected = profile["deployment"]["modules"]
    external = set(profile["deployment"].get("external", []))
    connectors = profile["deployment"].get("connectors", [])
    if set(selected) - manifests.keys() or external - manifests.keys():
        raise ValueError("unknown module")
    if len(selected) != len(set(selected)):
        raise ValueError("duplicate module")
    features = {
        name: data
        for manifest in manifests.values()
        for name, data in manifest.get("connectors", {}).items()
    }
    if set(connectors) - features.keys():
        raise ValueError("unknown connector")
    required = set(selected) | {"gateway"}
    dependencies = {name: list(m.get("requires", [])) for name, m in manifests.items()}
    for connector in connectors:
        feature = features[connector]
        if feature["module"] not in required:
            raise ValueError("connector module not selected")
        dependencies[feature["module"]] += feature.get("requires", [])
    visiting, visited, ordered = set(), set(), []

    def visit(name):
        if name not in manifests:
            raise ValueError("unknown dependency")
        if name in visiting:
            raise ValueError("module dependency cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in sorted(required):
        visit(name)
    if external - visited or "gateway" in external:
        raise ValueError("external module must be selected; gateway must be managed")
    result = []
    for name in ordered:
        m = manifests[name]
        services = list(m["services"])
        compose_files = [m["compose"]]
        required_files = list(m.get("required_files", []))
        for connector in connectors:
            feature = features[connector]
            if feature["module"] == name:
                services += feature.get("services", [])
                compose_files += feature.get("compose", [])
                required_files += feature.get("required_files", [])
        result.append(
            {
                "module": name,
                "mode": "external" if name in external else "managed",
                "project": m["project"],
                "compose": m["compose"],
                "compose_files": list(dict.fromkeys(compose_files)),
                "services": sorted(set(services)),
                "required_files": required_files
                if name not in external
                else [],
                "env_file": m.get("env_file"),
            }
        )
    return {
        "version": 1,
        "profile": profile["id"],
        "modules": result,
        "tools": profile["exposure"]["tools"],
    }


def commands(plan, action, root=ROOT):
    result = []
    modules = list(reversed(plan["modules"])) if action == "stop" else plan["modules"]
    for module in modules:
        if module["mode"] == "external":
            continue
        argv = [
            "docker",
            "compose",
            "--project-name",
            module["project"],
            "-f",
            str(root / module["compose"]),
        ]
        for overlay in module.get("compose_files", [module["compose"]])[1:]:
            argv += ["-f", str(root / overlay)]
        if module.get("env_file"):
            argv += ["--env-file", str(root / module["env_file"])]
        argv += {
            "up": ["up", "-d", "--wait"],
            "stop": ["stop"],
            "status": ["ps", "--format", "json"],
        }[action]
        result.append(argv + module["services"])
    return result


def validate_files(plan, root=ROOT):
    # Only selected managed modules: an external/disabled backend needs no local secrets.
    for module in plan["modules"]:
        for relative in module["required_files"]:
            path = root / relative
            if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError(
                    "missing or unsafe private configuration for " + module["module"]
                )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["admin"]:
        # `deedkit admin ...` (ANYM-491): host administration, separate from module deployment.
        from deedkit_admin import main as admin

        sys.exit(admin(argv[1:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["resolve", "up", "status", "stop"])
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = resolve(read(args.profile))
        if args.action == "resolve" or args.dry_run:
            if args.action != "resolve":
                plan["commands"] = commands(plan, args.action)
            print(json.dumps(plan, indent=2))
            return
        if args.action == "up":
            validate_files(plan)
        for command in commands(plan, args.action):
            subprocess.run(command, check=True, cwd=ROOT)
    except (KeyError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(
            1,
            "deployment failed: "
            + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
