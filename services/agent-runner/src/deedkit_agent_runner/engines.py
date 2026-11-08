"""The engines' JSON streams, read as data: normalized log entries, progress, the summary and the outcome.

Claude: `claude -p --output-format stream-json --verbose` (system / assistant / user / result events).
Codex: `codex exec --json` (thread.* / turn.* / item.* events, items agent_message, command_execution,
file_change, ...), with the final message in `-o /out/result.txt`.
"""

import json
import re

SUMMARY_LIMIT = 4000
LAST_TEXT_LIMIT = 300
TEXT_LIMIT = 2000
RESULT_LINE = re.compile(r"^\s*RESULT:\s*(done|partial|blocked)\b", re.M | re.I)
COMMIT = re.compile(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+commit\b")
LOGIN_HINTS = re.compile(r"(invalid api key|please run /login|oauth token|not logged in|login required|"
                         r"authentication_error|unauthorized|\b401\b|refresh token|codex login)", re.I)
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def outcome(summary):
    matches = RESULT_LINE.findall(summary or "")
    return matches[-1].lower() if matches else "unknown"


def clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Stream:
    """Reads one engine's stdout line by line. `feed` returns the normalized entries for the log."""

    def __init__(self, engine):
        self.engine = engine
        self.commits, self.files = 0, set()
        self.last_text, self.final, self.error = None, None, None

    def progress(self):
        return {"last_text": self.last_text, "commits": self.commits, "files_changed": len(self.files)}

    def feed(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict):
            try:
                return (self._claude if self.engine == "claude" else self._codex)(event)
            except (AttributeError, TypeError, ValueError, KeyError):
                pass  # an event of an unexpected shape is logged as text, never fatal to monitoring
        text = line.strip()
        return [("tool", clip(text, TEXT_LIMIT))] if text else []

    def _text(self, text):
        if text and text.strip():
            self.last_text = clip(text.strip(), LAST_TEXT_LIMIT)
        return ("assistant", clip(text, TEXT_LIMIT))

    def _command(self, command):
        if COMMIT.search(command or ""):
            self.commits += 1

    def _claude(self, event):
        kind = event.get("type")
        entries = []
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    entries.append(self._text(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    name, data = block.get("name") or "tool", block.get("input") or {}
                    if name == "Bash":
                        self._command(data.get("command"))
                    if name in EDIT_TOOLS and isinstance(data.get("file_path"), str):
                        self.files.add(data["file_path"])
                    entries.append(("tool", clip(f"{name} {json.dumps(data, ensure_ascii=False)}", TEXT_LIMIT)))
        elif kind == "result":
            text = event.get("result") if isinstance(event.get("result"), str) else ""
            self.final = text
            if event.get("is_error"):
                self.error = text or str(event.get("subtype") or "error")
            entries.append(("result", clip(text or str(event.get("subtype") or ""), TEXT_LIMIT)))
        return entries

    def _codex(self, event):
        kind, item = event.get("type"), event.get("item") or {}
        if kind in ("error", "turn.failed"):
            message = event.get("message") or (event.get("error") or {}).get("message") or "error"
            self.error = str(message)
            return [("result", clip(str(message), TEXT_LIMIT))]
        if kind == "turn.completed":
            return [("result", "turn completed")]
        if kind != "item.completed" or not isinstance(item, dict):
            return []
        itype = item.get("type")
        if itype == "agent_message":
            self.final = item.get("text") or ""
            return [self._text(self.final)]
        if itype == "command_execution":
            self._command(item.get("command"))
            return [("tool", clip(f"$ {item.get('command')} (exit {item.get('exit_code')})", TEXT_LIMIT))]
        if itype == "file_change":
            for change in item.get("changes") or []:
                if isinstance(change, dict) and isinstance(change.get("path"), str):
                    self.files.add(change["path"])
            return [("tool", clip(f"file_change {json.dumps(item.get('changes'), ensure_ascii=False)}", TEXT_LIMIT))]
        if itype in ("mcp_tool_call", "web_search", "todo_list"):
            return [("tool", clip(f"{itype} {json.dumps(item, ensure_ascii=False)}", TEXT_LIMIT))]
        return []

    def login_required(self, exit_code):
        """An engine that ended at once on an authentication error: the start does not count toward the cap."""
        return bool(exit_code and self.error and LOGIN_HINTS.search(self.error) and not self.commits)


def summary(stream, result_file):
    if stream.engine == "codex" and result_file is not None:
        text = result_file.decode(errors="replace")
    else:
        text = stream.final or ""
    return clip(text, SUMMARY_LIMIT) if text else None


def help_ok(engine, text, flags):
    return all(re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) for flag in flags[engine])


def login_state(code, text):
    if code == 0:
        return "ok"
    if LOGIN_HINTS.search(text or ""):
        return "login_required"
    return "unknown"
