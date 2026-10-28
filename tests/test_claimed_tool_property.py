"""claimed_tool (gateway dispatch, first 64 KiB, and the messaging app, first 16 KiB) reads a prefix of an MCP body to
decide how much more to read. It must never claim a tool other than the top-level params.name json.loads sees in the body."""
import ast
import json
from pathlib import Path
import re

from hypothesis import given, settings, strategies as st

from messaging_service.app import claimed_tool as messaging_claimed_tool

ROOT = Path(__file__).resolve().parents[1]


def _gateway_claimed_tool():
    # The gateway is its own project (not importable here): compile its exact function from source.
    source = (ROOT / "services/gateway/src/agent_gateway/app.py").read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "claimed_tool")
    namespace = {"json": json, "SPACE": re.compile(r"[ \t\n\r]*")}
    exec(compile(ast.Module([node], []), "agent_gateway/app.py", "exec"), namespace)
    return namespace["claimed_tool"]


CLAIMS = (messaging_claimed_tool, _gateway_claimed_tool())
TOOLS = ("messages_send", "upload_file_chunk", "messages_search", "")
space = st.sampled_from(["", " ", "\n", "\t ", "\r\n"])
scalar = st.one_of(st.none(), st.booleans(), st.integers(), st.sampled_from(TOOLS), st.text(max_size=5))


def key(name):
    # A key as written on the wire: plain, or with a \u escape json.loads decodes to the same name.
    return st.sampled_from([json.dumps(name), json.dumps(name).replace(name[:1], "\\u%04x" % ord(name[:1]), 1)]
                           if name else [json.dumps(name)])


@st.composite
def obj(draw, members):
    """A JSON object from (key, value text) pairs, kept in order and duplicates allowed, with random spacing."""
    parts = []
    for name, value in draw(members):
        parts.append(draw(space) + draw(key(name)) + draw(space) + ":" + draw(space) + value + draw(space))
    return "{" + ",".join(parts) + draw(space) + "}"


def members(names, values, first):
    # Usually a well-formed tools/call first, then any members (repeated keys included) after it.
    rest = st.lists(st.tuples(st.sampled_from(names), values), max_size=4)
    return st.one_of(rest, st.tuples(first, rest).map(lambda pair: pair[0] + pair[1]))


value = scalar.map(json.dumps)
tool = st.sampled_from(TOOLS).map(json.dumps)
params = obj(members(["name", "arguments", "_meta", "nam"], st.one_of(tool, value),
                     tool.map(lambda name: [("name", name)])))
method = st.sampled_from(["tools/call", "tools/list"]).map(json.dumps)
body = obj(members(["method", "params", "id", "jsonrpc"], st.one_of(method, params, value),
                   params.map(lambda p: [("method", '"tools/call"'), ("params", p)])))


MALFORMED = object()


def seen(raw):
    """The tool json.loads sees: the top-level tools/call's params.name, None, or MALFORMED when it parses nothing
    (then the parsed-call check after the read refuses any claim: messaging app.py, gateway large_call)."""
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError):
        return MALFORMED
    if isinstance(envelope, dict) and envelope.get("method") == "tools/call" and isinstance(envelope.get("params"), dict):
        name = envelope["params"].get("name")
        return name if isinstance(name, str) else None
    return None


# spec: ANYM-547
@settings(max_examples=1500, deadline=None)
@given(body, st.binary(max_size=2), st.integers(min_value=0, max_value=200))
def test_claimed_tool_never_claims_a_tool_json_loads_does_not_see(text, junk, at):
    raw = text.encode()
    # Also a stray byte (invalid UTF-8 or a control character) somewhere in the body.
    for candidate in (raw, raw[:at] + junk + raw[at:]):
        expected = seen(candidate)
        for copy, claimed_tool in zip(("messaging", "gateway"), CLAIMS):
            claim = claimed_tool(candidate)
            assert claim is None or expected is MALFORMED or claim == expected, (copy, candidate)


arguments = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=8)),
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=4), inner, max_size=3)),
    max_leaves=20)


# spec: ANYM-547
@settings(max_examples=800, deadline=None)
@given(st.sampled_from(TOOLS[:3]), arguments, st.integers(min_value=1, max_value=2000))
def test_a_cut_prefix_claims_the_named_tool_once_the_name_is_complete(name, args, cut):
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": {"value": args, "text": "x" * 300}}}).encode()
    # The byte after the name's closing quote: from there on, any cut (inside nested arguments, a number, a
    # literal or a key) must keep the claim, or a legitimate large call could not be read.
    named = raw.index(json.dumps(name).encode(), raw.index(b'"name"')) + len(json.dumps(name).encode())
    for claimed_tool in CLAIMS:
        assert claimed_tool(raw) == name
        assert claimed_tool(raw[:cut]) == name if cut >= named else claimed_tool(raw[:cut]) in (None, name)
