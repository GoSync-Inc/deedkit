"""A stateful stand-in for the Tiden CLI 0.33.0 and `gh api`, shared by the PR check and main sync tests.

State lives in one JSON file, so a rerun sees what an earlier run left on the "server". Each command accepts exactly
the flags the real CLI accepts (`spec update`, `test links` and `test link-spec` reject `--product-id`), and every call
is logged, so a test can prove that a read-only path wrote nothing.
"""
import json
from pathlib import Path
import subprocess
import sys
import textwrap

SECRET = "tfy_sentinel_secret_value"
READS = {("spec", "list"), ("branch", "list"), ("branch", "merge-preview"), ("test", "list"), ("test", "links")}
FAKE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, uuid
    path = os.environ["FAKE_TIDEN_STATE"]
    state = json.load(open(path))
    args = sys.argv[1:]
    state.setdefault("log", []).append(args)
    def save():
        json.dump(state, open(path, "w"))
    save()
    FLAGS = {{
        ("spec", "list"): {{"--branch", "--include-sources", "--product-id", "--format"}},
        ("branch", "list"): {{"--product-id", "--format"}},
        ("branch", "merge-preview"): {{"--name", "--product-id", "--format"}},
        ("branch", "merge"): {{"--name", "--product-id", "--format"}},
        ("test", "list"): {{"--all", "--view", "--product-id", "--format"}},
        ("test", "links"): {{"--format"}},
        ("test", "link-spec"): {{"--spec-id", "--format"}},
        ("spec", "update"): {{"--id", "--sources-file", "--status", "--format"}},
    }}
    command = tuple(args[:2])
    if command not in FLAGS:
        sys.exit(64)
    rest = args[2:]
    positional = []
    if command in {{("test", "links"), ("test", "link-spec")}}:
        positional, rest = rest[:1], rest[1:]
    options, index = {{}}, 0
    while index < len(rest):
        flag = rest[index]
        if flag not in FLAGS[command]:
            sys.stderr.write("unknown flag " + flag + "\\n"); sys.exit(2)
        if flag in ("--include-sources", "--all"):
            options[flag] = True; index += 1
        else:
            options[flag] = rest[index + 1]; index += 2
    fault = state.get("fault", {{}}).get(" ".join(command))
    if fault == "exit":
        sys.stderr.write("request body {SECRET}\\n"); print("{SECRET}"); sys.exit(3)
    if fault == "garbage":
        print("not json {SECRET}"); sys.exit(0)
    def view(branch):
        specs = {{spec["seqNum"]: spec for spec in state["main"]}}
        if branch != "main":
            specs.update({{spec["seqNum"]: spec for spec in state.get("branch_specs", {{}}).get(branch, [])}})
        rows = []
        for spec in specs.values():
            row = dict(spec)
            row.setdefault("sources", [])
            row["sourceCount"] = len(row["sources"])
            if not options.get("--include-sources"):
                row.pop("sources"); row.pop("sourceCount")
            rows.append(row)
        return rows
    def find(spec_id):
        for spec in state["main"]:
            if spec["id"] == spec_id:
                return spec
        sys.exit(4)
    if command == ("spec", "list"):
        rows = view(options.get("--branch", "main"))
        print(json.dumps(state.get("listing") or {{"requirements": rows,
                                                  "pagination": {{"nextPageToken": "", "totalCount": len(rows)}}}}))
    elif command == ("branch", "list"):
        print(json.dumps({{"branches": state.get("branches", [])}}))
    elif command == ("branch", "merge-preview"):
        print(json.dumps(state["previews"][options["--name"]]))
    elif command == ("branch", "merge"):
        name = options["--name"]
        if fault == "noop":
            save(); print(json.dumps({{"merged": True}})); sys.exit(0)
        merged = {{spec["seqNum"]: spec for spec in state.get("branch_specs", {{}}).get(name, [])}}
        state["main"] = [spec for spec in state["main"] if spec["seqNum"] not in merged] + list(merged.values())
        for branch in state["branches"]:
            if branch["name"] == name:
                branch["status"] = "merged"
        save(); print(json.dumps({{"merged": True}}))
    elif command == ("test", "list"):
        rows = state.get("tests", [])
        print(json.dumps({{"view": "identity", "identities": rows,
                          "pagination": {{"nextPageToken": "", "totalCount": len(rows)}}}}))
    elif command == ("test", "links") and fault == "malformed":
        print(json.dumps({{"links": [{{"requirementId": []}}]}}))
    elif command == ("test", "links"):
        print(json.dumps({{"links": [{{"id": str(uuid.uuid4()), "testId": positional[0], "requirementId": spec}}
                                    for spec in state.get("links", {{}}).get(positional[0], [])], "readOnly": False}}))
    elif command == ("test", "link-spec"):
        if fault != "noop":
            state.setdefault("links", {{}}).setdefault(positional[0], []).append(options["--spec-id"])
        save(); print(json.dumps({{"id": str(uuid.uuid4())}}))
    elif command == ("spec", "update"):
        spec = find(options["--id"])
        if "--status" in options and fault != "noop":
            spec["status"] = options["--status"]
        if "--sources-file" in options:
            sources = json.load(open(options["--sources-file"]))
            state.setdefault("sent_sources", []).append(sources)
            if fault == "drop":
                sources = sources[1:]
            if fault == "corrupt":
                sources = [{{**sources[0], "excerpt": "changed"}}, *sources[1:]]
            # As the server does on a direct update (checked 2026-10-19): new record IDs, every field and the nested
            # metadata kept.
            spec["sources"] = [{{**source, "id": str(uuid.uuid4())}} for source in sources]
        save(); print(json.dumps({{"id": spec["id"], "seqNum": spec["seqNum"]}}))
""")
GH = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    state = json.load(open(os.environ["FAKE_TIDEN_STATE"]))
    args = sys.argv[1:]
    if args[:1] != ["api"] or not args[1].startswith("repos/GoSync-Inc/deedkit/pulls/"):
        sys.exit(64)
    pull = state.get("pulls", {{}}).get(args[1].rsplit("/", 1)[1])
    if pull is None:
        sys.stderr.write("HTTP 404\\n"); sys.exit(1)
    print(json.dumps(pull))
""")


def spec(number, *, kind="User Story", children=0, status="Active", content="", sources=(), branch=False):
    return {"id": f"{'b' if branch else 'm'}-{number:04d}-0000-0000-0000-000000000000", "seqNum": number,
            "type": kind, "childrenCount": children, "status": status, "title": f"Spec {number}",
            "content": content, "sources": [dict(source) for source in sources]}


def anchor(path, **extra):
    return {"id": f"src-{path}", "sourceType": "repo_file", "repoPath": path,
            "repository": "github.com/GoSync-Inc/deedkit", **extra}


class Server:
    def __init__(self, directory: Path, monkeypatch):
        self.path = directory / "state.json"
        self.cli, self.gh = directory / "tiden", directory / "gh"
        for target, text in ((self.cli, FAKE), (self.gh, GH)):
            target.write_text(text)
            target.chmod(0o755)
        monkeypatch.setenv("FAKE_TIDEN_STATE", str(self.path))
        monkeypatch.setenv("TIDEN_API_TOKEN", SECRET)
        self.path.write_text(json.dumps({"main": [], "branches": [{"name": "main", "status": "open"}], "log": []}))

    def set(self, **values):
        state = self.state()
        state.update(values)
        self.path.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.path.read_text())

    def calls(self):
        return [tuple(entry[:2]) for entry in self.state()["log"]]

    def writes(self):
        return [entry for entry in self.state()["log"] if tuple(entry[:2]) not in READS]

    def main(self, number):
        return next(spec for spec in self.state()["main"] if spec["seqNum"] == number)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(root, files, message):
    for name, text in files.items():
        if text is None:
            git(root, "rm", "-q", "--", name)
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(root, "add", "--", name)
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def repository(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(root, "config", key, value)
    return root
