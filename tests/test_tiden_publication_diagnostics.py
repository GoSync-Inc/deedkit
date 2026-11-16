"""A refused main publication names its fixed failure stage and cause, and still refuses (PR #1006 attempt 1).

The refusal stays fail-closed with one anonymous read and no retry; its diagnostic carries only fixed words and an
HTTP status number, never response text, headers or tokens.
"""
import email.message
import http.client
import json
import urllib.error
import uuid

import pytest

from scripts import tiden_ci
from tests.test_ci_reuse import (PR_URL, PUSH_URL, REPOSITORY, SHA, TESTED_SHA, TREE, Response, chain_published,
                                 published_evidence, tiden_main)

REAL_COMMIT_TREE = tiden_ci.commit_tree
# Low-entropy canaries for text a response or error could carry; none may reach a diagnostic.
CANARY = "canary-response-text-" + "x" * 8
TOKEN = "canary-token-" + "y" * 8


def http_error(code):
    headers = email.message.Message()
    headers["WWW-Authenticate"] = TOKEN
    return urllib.error.HTTPError("https://api.github.com/" + CANARY, code, CANARY, headers, None)


def answers(monkeypatch, outcome):
    """GitHub's anonymous commit read answers with `outcome`: a body, an exception, or a reader that raises."""
    requests = []

    def urlopen(request, timeout):
        requests.append(dict(request.header_items()))
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, bytes):
            return Response(outcome)
        if callable(outcome):
            return outcome()
        return Response(json.dumps(outcome).encode())
    monkeypatch.setattr(tiden_ci.urllib.request, "urlopen", urlopen)
    return requests


class SlowBody(Response):
    def read(self, *args):
        raise TimeoutError(CANARY)


REMOTE_FAILURES = {
    "http_404": http_error(404), "http_403": http_error(403), "http_504": http_error(504),
    "timeout": urllib.error.URLError(TimeoutError(CANARY)),
    "transport": urllib.error.URLError(ConnectionRefusedError(CANARY)),
    "malformed_response": CANARY.encode(),
    "commit_mismatch": {"sha": "b" * 40, "tree": {"sha": TREE}, "message": CANARY},
    "malformed_tree": {"sha": TESTED_SHA, "tree": {"sha": CANARY}},
}


# spec: ANYM-322
@pytest.mark.parametrize("cause", [*REMOTE_FAILURES, "read_timeout", "incomplete_read", "not_an_object"])
def test_the_anonymous_commit_read_names_a_fixed_failure_class_and_still_returns_no_tree(monkeypatch, cause):
    outcome = {"read_timeout": lambda: SlowBody(b""), "incomplete_read": http.client.IncompleteRead(CANARY.encode()),
               "not_an_object": [CANARY]}.get(cause, REMOTE_FAILURES.get(cause))
    requests = answers(monkeypatch, outcome)
    tree = tiden_ci.commit_tree(REPOSITORY, TESTED_SHA)
    expected = {"read_timeout": "timeout", "incomplete_read": "transport", "not_an_object": "malformed_response"}
    assert tree == "" and tree.failure == expected.get(cause, cause)
    # One anonymous read, no retry and no credential.
    assert len(requests) == 1 and not any(name.lower() == "authorization" for name in requests[0])
    requests.clear()
    assert tiden_ci.commit_tree("fork/deedkit/../x", TESTED_SHA).failure == "invalid_request" and requests == []
    answers(monkeypatch, {"sha": TESTED_SHA, "tree": {"sha": TREE}})
    read = tiden_ci.commit_tree(REPOSITORY, TESTED_SHA)
    assert read == TREE and read.failure == ""


def refusal(monkeypatch, published, environment=None):
    code_calls = []
    with pytest.raises(ValueError, match="publication record") as raised:
        code_calls.append(tiden_main(monkeypatch, published, environment or {}, "publish"))
    assert not (published / "tiden-create.json").exists() and not (published / "ci-checks-published.json").exists()
    message = str(raised.value)
    assert CANARY not in message and TOKEN not in message and "Bearer" not in message
    return raised.value.stage, raised.value.cause, message


# spec: ANYM-322
def test_a_refused_chained_publication_tells_which_remote_local_or_record_check_failed(tmp_path, monkeypatch):
    seen = {}
    published = chain_published(tmp_path, monkeypatch)
    monkeypatch.setattr(tiden_ci, "commit_tree", REAL_COMMIT_TREE)
    for cause, outcome in REMOTE_FAILURES.items():
        requests = answers(monkeypatch, outcome)
        stage, got, message = refusal(monkeypatch, published)
        assert (stage, got) == ("remote_tree_unavailable", cause) and len(requests) == 1
        assert message.endswith(f"(stage: remote_tree_unavailable, cause: {cause})")
        seen[cause] = (stage, got)
    # GitHub records another tree for the tested commit: a genuine mismatch, not an unavailable read.
    answers(monkeypatch, {"sha": TESTED_SHA, "tree": {"sha": "0" * 40}})
    seen["remote_mismatch"] = refusal(monkeypatch, published)[:2]
    assert seen["remote_mismatch"] == ("remote_tree_mismatch", "")
    # The local checkout is not the push commit, or has another tree: refused before any GitHub read.
    requests = answers(monkeypatch, {"sha": TESTED_SHA, "tree": {"sha": TREE}})
    for name, local in (("local_unreadable", ""), ("local_mismatch", "0" * 40)):
        monkeypatch.setattr(tiden_ci, "checkout_tree", lambda sha, local=local: local)
        seen[name] = refusal(monkeypatch, published)[:2]
    assert seen["local_unreadable"] == ("local_checkout_unreadable", "")
    assert seen["local_mismatch"] == ("local_tree_mismatch", "") and requests == []
    monkeypatch.setattr(tiden_ci, "checkout_tree", lambda sha: TREE if sha == SHA else "")
    record = json.loads((published / "publication.json").read_text())
    for name, change, stage in (
            ("malformed_tree", {"chain": {**record["chain"], "tree": TREE.upper()}}, "chain_malformed"),
            ("chain_evidence", {"chain": {**record["chain"], "head_branch": "other/branch"}}, "chain_evidence"),
            ("producer", {"producer": {**record["producer"], "artifact_digest": "md5:x"}}, "producer_record"),
            ("record", {"run_attempt": "2"}, "publisher_identity")):
        (published / "publication.json").write_text(json.dumps({**record, **change}))
        seen[name] = refusal(monkeypatch, published)[:2]
        assert seen[name] == (stage, "")
    assert requests == []
    (published / "publication.json").write_text("{" + CANARY)
    seen["unreadable"] = refusal(monkeypatch, published)[:2]
    assert seen["unreadable"] == ("record_unreadable", "")
    # Every failure class is distinguishable from every other.
    assert len(set(seen.values())) == len(seen)
    # With the record restored and GitHub answering, the same evidence publishes its original rows.
    (published / "publication.json").write_text(json.dumps(record))
    requests = answers(monkeypatch, {"sha": TESTED_SHA, "tree": {"sha": TREE}})
    code, calls = tiden_main(monkeypatch, published, {}, "publish")
    assert code == 0 and len(requests) == 1 and any(call[1:3] == ["run", "create"] for call in calls)
    rows = json.loads((published / "ci-checks-published.json").read_text())
    assert rows and all(row["message"].endswith("tested in " + PR_URL) for row in rows)
    assert [row["id"] for row in rows] == [str(uuid.uuid5(uuid.NAMESPACE_URL, PUSH_URL + "/" + row["signature"].split("::", 1)[1]))
                                           for row in rows]


# spec: ANYM-358
def test_a_refused_queue_publication_names_its_stage_and_valid_queue_evidence_still_publishes(tmp_path, monkeypatch):
    evidence, published = published_evidence(tmp_path)
    requests = answers(monkeypatch, http_error(504))
    manifest = json.loads((published / "manifest.json").read_text())
    (published / "manifest.json").write_text(json.dumps({**manifest, "sha": "b" * 40}))
    assert refusal(monkeypatch, published)[:2] == ("queue_evidence", "")
    (published / "manifest.json").write_text(json.dumps(manifest))
    assert refusal(monkeypatch, published, {"GITHUB_REF": "refs/heads/feature"})[:2] == ("publisher_identity", "")
    code, _ = tiden_main(monkeypatch, published, {}, "publish")
    rows = json.loads((published / "ci-checks-published.json").read_text())
    queue_rows = json.loads((evidence / "ci-checks.json").read_text())
    assert code == 0 and requests == []
    assert [(row["signature"], row["execution"]) for row in rows] == [(row["signature"], row["execution"]) for row in queue_rows]
    assert [row["id"] for row in rows] == [str(uuid.uuid5(uuid.NAMESPACE_URL, PUSH_URL + "/" + row["signature"].split("::", 1)[1]))
                                           for row in rows]
