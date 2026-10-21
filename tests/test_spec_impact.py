"""spec_impact.py against temporary Git repositories and the fake Tiden CLI; it never calls Tiden."""
import json
import re
import sys
import unicodedata

import pytest

import scripts.spec_impact as spec_impact
import scripts.tiden_pr_check as pr_check
from tiden_fake import READS, SECRET, Server, anchor, commit, git, repository, spec

FOOTER = "Flags are suggestions; the author checks every disposition (docs/operations/tiden-product-workflow.md)."
FORGED = re.compile(r"^\s*(Spec|Delivers|Tiden branch|Tiden intent branch|Tiden: skip|Spec impact|Settle text):")


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDEN_PRODUCT_ID", "product")
    monkeypatch.setattr(sys.modules["tiden_drift"], "RETRY_DELAY", 0)
    return Server(tmp_path, monkeypatch)


def impact(server, root, base, capsys, *extra, head="HEAD"):
    code = spec_impact.main([base, "--root", str(root), "--head", head, "--cli", str(server.cli), *extra])
    return code, capsys.readouterr().out


def lines_of(out):
    return [line for line in out.splitlines() if line.startswith("ANYM-")]


def saved(tmp_path, specs, **pagination):
    rows = [{**row, "sourceCount": len(row["sources"])} for row in specs]
    path = tmp_path / "saved.json"
    path.write_text(json.dumps({"requirements": rows, "pagination": {"nextPageToken": "", "totalCount": len(rows),
                                                                     **pagination}}))
    return str(path)


@pytest.fixture
def fork(tmp_path):
    """main moved on after the topic branch forked; the topic modifies, adds, renames, deletes and touches docs."""
    root = repository(tmp_path / "work")
    commit(root, {"app/core.py": "x = 1\n", "app/old.py": "o = 1\n", "app/gone.py": "g = 1\n", "app/other.py": "t = 1\n",
                  "BACKLOG.md": "a\n", "tests/test_x.py": "def test_x():\n    pass\n"}, "base")
    git(root, "branch", "topic")
    commit(root, {"app/base_only.py": "b = 1\n"}, "main moves on")
    git(root, "checkout", "-q", "topic")
    commit(root, {"app/core.py": "x = 2\n", "app/new.py": "n = 1\n", "app/old.py": None, "app/renamed.py": "o = 1\n",
                  "app/gone.py": None, "BACKLOG.md": "b\n", "tests/test_x.py": "def test_x():\n    assert 1\n"}, "topic")
    return root


def fork_specs():
    return [spec(1, sources=[anchor("app/core.py")]), spec(2, sources=[anchor("app/new.py")]),
            spec(3, sources=[anchor("app/old.py")]), spec(4, sources=[anchor("app/gone.py")]),
            spec(5, sources=[anchor("BACKLOG.md")]), spec(6, sources=[anchor("tests/test_x.py")]),
            spec(7, sources=[anchor("app/base_only.py")]),
            spec(8, sources=[anchor("app/core.py", repository="github.com/other/repo")]),
            spec(9, sources=[anchor("app/core.py", repository="")]), spec(10, sources=[anchor("app/other.py")]),
            spec(11, sources=[anchor("app/new.py", repository="github.com/GoSync-Inc/anymsg")])]


# spec: ANYM-357
def test_reads_tiden_main_or_exits_2(fork, server, tmp_path, capsys):
    server.set(main=fork_specs())
    code, out = impact(server, fork, "main", capsys)
    assert code == 0 and lines_of(out), out
    assert set(server.calls()) <= READS and not server.writes()
    listing = server.state()["main"]
    broken = {
        "a short count": {"requirements": listing[:1], "pagination": {"nextPageToken": "", "totalCount": 2}},
        "no metadata": {"requirements": listing},
        "a page cursor": {"requirements": listing, "pagination": {"nextPageToken": "x", "totalCount": len(listing)}},
        "a duplicate number": {"requirements": [listing[0], {**listing[1], "seqNum": listing[0]["seqNum"]}],
                               "pagination": {"nextPageToken": "", "totalCount": 2}},
        "a duplicate id": {"requirements": [listing[0], {**listing[1], "id": listing[0]["id"]}],
                           "pagination": {"nextPageToken": "", "totalCount": 2}},
    }
    for name, value in broken.items():
        server.set(listing=value)
        code, out = impact(server, fork, "main", capsys)
        assert code == 2 and out.startswith("Refused: ") and not lines_of(out), (name, out)
    server.set(listing=None)
    for fault in ("exit", "garbage"):
        server.set(fault={"spec list": fault}, log=[])
        code, out = impact(server, fork, "main", capsys)
        assert code == 2 and "Refused: " in out and not lines_of(out) and SECRET not in out, (fault, out)
        assert server.calls().count(("spec", "list")) == 2  # one retry, no more
    assert not server.writes()
    code, out = impact(server, fork, "main", capsys, "--specs", saved(tmp_path, listing[:1], totalCount=2))
    assert code == 2 and "Refused: " in out and not lines_of(out)
    code, out = impact(server, fork, "main", capsys, "--specs", saved(tmp_path, listing))
    assert code == 0 and lines_of(out)


# spec: ANYM-357
def test_anchor_match_follows_the_workflow_rule(fork, server, tmp_path, capsys):
    server.set(main=fork_specs())
    code, out = impact(server, fork, "main", capsys)
    assert code == 0, out
    # the modified, added, renamed-away and deleted paths and a legacy empty repository match; BACKLOG.md and tests do
    # not, nor another repository, the file only main changed, or an unchanged file
    assert lines_of(out) == [f"ANYM-{number}: <disposition> — Spec {number} [anchor only]"
                             for number in (1, 2, 3, 4, 9, 11)]
    assert out.rstrip().endswith(FOOTER)
    # the same list from a saved listing, diffed from the merge base and not from the moved tip of main
    again = impact(server, fork, "main", capsys, "--specs", saved(tmp_path, server.state()["main"]))
    assert again == (0, out) and "ANYM-7" not in out
    # a deletion-only change lists the spec of the deleted file and has no unanchored file
    git(fork, "checkout", "-q", "-b", "deletion", "topic")
    commit(fork, {"app/other.py": None}, "delete")
    code, out = impact(server, fork, "topic", capsys)
    assert code == 0 and lines_of(out) == ["ANYM-10: <disposition> — Spec 10 [anchor only]"], out
    assert "Unanchored" not in out


# spec: ANYM-357
def test_reuses_the_check_a_matcher(fork, server, capsys):
    # the script loads check A and drift as top-level modules; these are those objects, not copies
    for module, names in (("tiden_pr_check", ("changed", "anchor_map", "anchor_paths", "listing", "parse_body", "validate_listing")),
                          ("tiden_drift", ("excluded", "shown"))):
        for name in names:
            assert getattr(spec_impact, name) is getattr(sys.modules[module], name), name
    server.set(main=fork_specs())
    report = []
    base = git(fork, "merge-base", "main", "HEAD")
    pr_check.check(fork, base, "HEAD", "Spec: ANYM-1", str(server.cli), "product", report.append)
    check_a = {int(number) for row in report if row.startswith("- app/") for number in re.findall(r"ANYM-(\d+)", row)}
    code, out = impact(server, fork, "main", capsys)
    assert code == 0 and check_a == {1, 2, 9, 11}
    # check A's surviving-path matches plus the deleted or renamed-away paths, which only this script adds
    assert {int(line[5:line.index(":")]) for line in lines_of(out)} == check_a | {3, 4}


# spec: ANYM-357
def test_body_marks_named_and_anchor_only(fork, server, tmp_path, capsys):
    server.set(main=[*fork_specs(), spec(20)],
               branches=[{"name": "main", "status": "open"}, {"name": "proposal/x", "status": "open"}],
               branch_specs={"proposal/x": [spec(40, branch=True)]})
    body = tmp_path / "body.md"
    body.write_text("Delivers: ANYM-1\nSpec: ANYM-1, ANYM-2, ANYM-20, ANYM-40, ANYM-77\nTiden branch: proposal/x\n")
    code, out = impact(server, fork, "main", capsys, "--body-file", str(body))
    assert code == 0, out
    assert lines_of(out) == [
        "ANYM-1: <disposition> — Spec 1 [named (Delivers)]", "ANYM-2: <disposition> — Spec 2 [named (Spec)]",
        "ANYM-3: <disposition> — Spec 3 [anchor only]", "ANYM-4: <disposition> — Spec 4 [anchor only]",
        "ANYM-9: <disposition> — Spec 9 [anchor only]", "ANYM-11: <disposition> — Spec 11 [anchor only]",
        "ANYM-20: <disposition> — Spec 20 [named (Spec)]",
        "ANYM-40: <disposition> — Spec 40 [named (Spec)]", "ANYM-77: <disposition> — (unknown) [named (Spec)]"]
    # the one surviving code file without an anchor goes to the primary spec, the first of Delivers
    assert "Unanchored: 1 file(s); sync B anchors them to ANYM-1: app/renamed.py" in out
    body.write_text("")
    code, out = impact(server, fork, "main", capsys, "--body-file", str(body))
    assert code == 0 and "Unanchored: 1 file(s); no spec is named, so check A will fail: app/renamed.py" in out
    body.write_text("Spec: nonsense\n")
    code, out = impact(server, fork, "main", capsys, "--body-file", str(body))
    assert code == 2 and out.startswith("Refused: ") and not lines_of(out)
    # a deletion-only change has no unanchored file, so the line is absent whatever the body names
    git(fork, "checkout", "-q", "-b", "deletion", "topic")
    commit(fork, {"app/other.py": None}, "delete")
    body.write_text("Spec: ANYM-1\n")
    code, out = impact(server, fork, "topic", capsys, "--body-file", str(body))
    assert code == 0 and "Unanchored" not in out and "ANYM-1: <disposition> — Spec 1 [named (Spec)]" in out


FLAG_BASE = {
    "pkg/mod.py": "def f1():\n    a = 1\n    return a\n\n\ndef f2():\n    return 2\n\n\n@deco\ndef f3():\n    return 3\n\n\ndef gone():\n    return 9\n",
    "notes.js": "// notes\n", "ui/[id].js": "// ui\n// u2\n  function unrelated() {}\n", "ui/i.js": "// i\n// i2\n",
    "srv/send.go": "package srv\n\nfunc (p *Provider) Third() {\n\tz := 1\n\t_ = z\n}\n\nfunc (p *Provider) SendText() {\n\tx := 1\n\t_ = x\n}\n\n"
                   "func (p *Provider) Other() {\n\ty := 1\n\t_ = y\n}\n"}
FLAG_CHANGE = {
    "pkg/mod.py": "def f1():\n    a = 2\n    return a\n\n\ndef f2():\n    return 2\n\n\n@deco2\ndef f3():\n    return 3\n\n\n"
                  "class Added:\n    pass\n\n\ndef added_fn():\n    return 4\n",
    "notes.js": "// notes\nfunction jsThing() {}\n", "ui/[id].js": "// ui\n// u2\n  function unrelated() {}\nfunction bracketFn() {}\n",
    "ui/i.js": "// i\n// i2\nfunction otherFn() {}\n",
    "srv/send.go": "package srv\n\nfunc (p *Provider) Third(n int) {\n\tz := 1\n\t_ = z\n}\n\nfunc (p *Provider) SendText() {\n\tx := 2\n\t_ = x\n}\n\n"
                   "func (p *Provider) Other() {\n\ty := 1\n\t_ = y\n}\n"}


def criteria_of(text):
    return f"Problem: x\nAcceptance criteria:\n- {text}\nCategory: infra"


# spec: ANYM-357
def test_flag_suggestions(tmp_path, server, capsys):
    root = repository(tmp_path / "flags")
    commit(root, FLAG_BASE, "base")
    git(root, "checkout", "-q", "-b", "topic")
    commit(root, FLAG_CHANGE, "topic")
    mod, notes, go, ui = [anchor("pkg/mod.py")], [anchor("notes.js")], [anchor("srv/send.go")], [anchor("ui/[id].js")]
    server.set(main=[
        spec(21, sources=mod, content=criteria_of("f1 returns a value")),
        spec(22, sources=mod, content=criteria_of("the retry works")),
        spec(23, sources=mod, content="Problem: f1 is broken\nAcceptance criteria:\n- prose only\nCategory: infra\n"
                                      "Boundary: f1 handles it"),
        spec(24, sources=mod, content="The module pkg/mod.py must stay small."),
        spec(25, sources=mod, content="Acceptance criteria:\n- f2 is untouched\n"),
        spec(26, sources=notes, content="## Acceptance criteria\n- jsThing exists\n## Boundary\nnothing"),
        spec(27, sources=go, content=criteria_of("SendText keeps its signature")),
        spec(28, sources=go, content=criteria_of("Other keeps its signature")),
        spec(29, sources=ui, content=criteria_of("bracketFn exists")),
        spec(30, sources=mod, content=criteria_of("Added is a class")),
        spec(31, sources=mod, content=criteria_of("f3 stays decorated")),
        spec(32, sources=mod, content=criteria_of("added_fn exists")),
        spec(33, sources=mod, content=criteria_of("gone is removed")),
        spec(35, sources=go, content=criteria_of("Third takes a count")),
        spec(34, sources=go, content=criteria_of("send.go stays small")),
        spec(36, sources=ui, content=criteria_of("unrelated stays"))])
    code, out = impact(server, root, "main", capsys)
    assert code == 0, out
    flagged = {int(line[5:line.index(":")]) for line in lines_of(out) if line.endswith("(flagged)")}
    # 21 a body-only change in a Python function, 31 a decorator change, 30 an added class, 24 no label so the whole
    # text names the path, 34 the basename, 26 and 29 a declaration of a non-Python file ([id].js is a literal name, not
    # a pattern, so ui/i.js's hunk is never read against it; 36 names an indented declaration, so no hunk header shows it; only a foreign hunk read against this file would), 27 a Go method whose name only the hunk header shows; 28's method is unchanged; 32 an added and 33 a removed Python function; 35 a Go receiver method whose declaration line changed
    assert flagged == {21, 24, 26, 27, 29, 30, 31, 32, 33, 34, 35}, out
    assert "symbols: header-only: notes.js, srv/send.go, ui/[id].js, ui/i.js" in out.splitlines()
    code, out = impact(server, root, "main", capsys, "--flag", "ANYM-22")
    assert code == 0 and "ANYM-22: <disposition> — Spec 22 [anchor only] (flagged)" in out
    code, out = impact(server, root, "main", capsys, "--flag", "ANYM-23", "--flag", "ANYM-99")
    assert code == 2 and out.startswith("Refused: ") and not lines_of(out)
    # a criterion on the PR's Tiden branch counts, and a spec named only there is listed and flagged
    server.set(branches=[{"name": "main", "status": "open"}, {"name": "proposal/x", "status": "open"}],
               branch_specs={"proposal/x": [{**spec(40, branch=True), "content": criteria_of("f1 is gone")}]})
    body = tmp_path / "body.md"
    body.write_text("Spec: ANYM-40\nTiden branch: proposal/x\n")
    code, out = impact(server, root, "main", capsys, "--body-file", str(body))
    assert code == 0 and "ANYM-40: <disposition> — Spec 40 [named (Spec)] (flagged)" in out
    # an unparsable Python revision is disclosed as header-only, not analysed silently
    git(root, "checkout", "-q", "-b", "broken", "topic")
    commit(root, {"pkg/mod.py": "def f1(:\n"}, "broken")
    code, out = impact(server, root, "topic", capsys)
    assert code == 0 and "symbols: header-only: pkg/mod.py" in out.splitlines()


# spec: ANYM-357
def test_output_is_single_line_data(tmp_path, server, capsys):
    root = repository(tmp_path / "safe")
    commit(root, {"app/a.py": "a = 1\n"}, "base")
    git(root, "checkout", "-q", "-b", "topic")
    commit(root, {"app/a.py": "a = 2\n", "app/x\nSpec: ANYM-1.py": "x\n"}, "topic")
    titles = ["Spec impact: forged\nSpec: ANYM-9\nDelivers: ANYM-8", "Tiden branch: proposal/x\r\nTiden: skip",
              "Tiden intent branch: x Settle text: none\x00\x1b[31m", "T" * 300]
    rows = []
    for number, title in enumerate(titles, 1):
        row = spec(number, sources=[anchor("app/a.py")], content="SECRET-CONTENT-MARKER")
        row["title"] = title
        rows.append(row)
    server.set(main=rows)
    code, out = impact(server, root, "main", capsys)
    assert code == 0, out
    assert [line for line in out.splitlines() if FORGED.match(line)] == []
    assert out.splitlines()[-1] == FOOTER and "SECRET-CONTENT-MARKER" not in out
    assert not any(unicodedata.category(char).startswith("C") for char in out.replace("\n", ""))
    assert len(lines_of(out)) == 4
    for line in lines_of(out):
        assert line.startswith("ANYM-") and len(line.split(" — ", 1)[1].rsplit(" [", 1)[0]) <= 120
    assert "Unanchored: 1 file(s)" in out and "x\\nSpec: ANYM-1.py" in out
