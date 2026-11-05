"""Decide whether a Bash tool call would print a container's environment or a private env file (ANYM-774).

Reads the tool-call JSON from stdin; exit 0 allows, exit 2 blocks with a refusal on stderr. Any doubt blocks. It guards
against an accident by a diagnosing agent and is no sandbox: SECURITY.md names what it does not cover. It parses the
command the way a shell splits it, so a quoted message is data and a command inside ssh, sudo, sh -c or $(...) is not.
The refusal never holds the command, which may itself hold the secret. Standard library only: it runs under python3 -I.
"""
import json
import re
import sys

MAX_INPUT = 256 * 1024
MAX_DEPTH = 32
REFUSAL = (
    "Blocked by .claude/hooks/block-secret-env-reads.sh: this command can print a container's environment or a private "
    "env file, and a session transcript goes to the model provider. Safe forms: docker inspect with a space-separated "
    "--format '<template>' limited to .State, .Name, .Image, .RestartCount or .Created; docker compose config "
    "--no-env-resolution; for key names only, ask the orchestrator for a key-names read. See SECURITY.md."
)
INTERNAL = (
    "Blocked by .claude/hooks/block-secret-env-reads.sh: the hook could not read this tool call, and it fails closed. "
    "A session transcript goes to the model provider, so an unchecked command does not run. See SECURITY.md."
)

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "su", "runuser"})
RESERVED = frozenset({"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "!", "{", "}", "esac"})
DATA_LED = frozenset({"for", "select", "case", "function"})
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*\+?=")
# Name: (positional words before the wrapped command, one-letter options that take no value).
WRAPPERS = {"sudo": (0, "nEHSisbkAPB"), "timeout": (1, "v"), "gtimeout": (1, "v"), "nice": (0, ""), "ionice": (0, "t"),
            "nohup": (0, ""), "command": (0, "p"), "builtin": (0, ""), "exec": (0, "cl"), "stdbuf": (0, ""),
            "setsid": (0, "cfw"), "xargs": (0, "0rtpxo"), "watch": (0, "tdebcpx"), "time": (0, "pv"), "caffeinate": (0, "dimsu"),
            "unbuffer": (0, "p"), "flock": (1, "sxnuoFe"), "chroot": (1, "")}
SSH_VALUE = "bcDEeFIiJLlmOopQRSWw"
SSH_FLAG = "46AaCfGgKkMNnqsTtVvXxYy"
DOCKER_VALUE = frozenset({"--context", "-c", "--host", "-H", "--config", "--log-level", "-l", "--tlscacert", "--tlscert", "--tlskey"})
COMPOSE_VALUE = frozenset({"-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory", "--workdir",
                           "--ansi", "--parallel", "--progress"})
EXEC_FLAG_LETTERS = "ditTP"
EXEC_LONG_FLAGS = frozenset({"--rm", "--privileged", "--no-deps", "--detach", "--interactive", "--tty", "--init", "--read-only",
                             "--service-ports", "--build", "--quiet-pull", "--remove-orphans", "--use-aliases", "--no-TTY",
                             "--publish-all", "--dry-run"})
COMPOSE_LIMITED = ("--services", "--images", "--hash", "--quiet", "-q", "--volumes")
PROC_ENVIRON = re.compile(r"/proc/[^/\s]+/environ")
SENSITIVE_ENDINGS = (".env", ".dsn", ".token", ".key", ".pem", ".agekey")
SENSITIVE_NAMES = frozenset({"environ", "config.v2.json"})
PLAIN_READERS = frozenset({"cat", "less", "more", "head", "tail", "strings", "xxd", "base64"})
# Pattern-first readers: their first positional word is a pattern or a program, not a path. Letters take a value.
PATTERN_READERS = {"grep": "ABCDdefm", "egrep": "ABCDdefm", "fgrep": "ABCDdefm", "rg": "ABCefgjmMrtTE", "sed": "efl",
                   "awk": "Fvfe", "gawk": "Fvfe"}
LONG_VALUE = frozenset({"--regexp", "--file", "--expression", "--include", "--exclude", "--exclude-dir", "--glob", "--iglob",
                        "--max-count", "--context", "--after-context", "--before-context", "--type", "--type-not", "--color",
                        "--field-separator", "--assign"})
LONG_EXPLICIT = frozenset({"--regexp", "--file", "--expression"})
LONG_PATH = frozenset({"--file", "--include", "--glob", "--iglob"})
TEMPLATE_ACTION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
TEMPLATE_STRING = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`')
TEMPLATE_SECRET = re.compile(r"\.(?:Container)?Config\b|\.Spec\b|(?<![\w)\]$])\.(?!\w)|\$(?![\w.])", re.IGNORECASE)


class Word(str):
    """A shell word after quote removal; dynamic: it holds an expansion whose value this hook cannot see."""
    dynamic = False


class Segment:
    """One simple command: its words, its input redirections, and the here-documents and here-strings it receives."""
    def __init__(self):
        self.words, self.inputs, self.bodies = [], [], []


class Parser:
    def __init__(self, text, depth):
        if depth > MAX_DEPTH: raise ValueError("nesting too deep")
        self.text, self.depth, self.segments = text, depth, []

    def run(self):
        self.sequence(0, None)
        return self.segments

    def nested(self, text):
        self.segments.extend(Parser(text, self.depth + 1).run())

    def substitution(self, start):
        """Parses $(...) from the index after its opening; returns the index after its closing parenthesis."""
        self.depth += 1
        if self.depth > MAX_DEPTH: raise ValueError("nesting too deep")
        end = self.sequence(start, ")")
        self.depth -= 1
        return end

    def backtick(self, start):
        """Parses `...` from the index after its opening; returns the index after its closing backtick."""
        text, index = self.text, start
        while index < len(text) and text[index] != "`":
            index += 2 if text[index] == "\\" else 1
        self.nested(re.sub(r"\\([`$\\])", r"\1", text[start:index]))
        return index + 1

    def expansions(self, index, heredoc=False):
        """Reads double-quoted text, or a whole unquoted here-document: returns (index after it, text, dynamic)."""
        text, parts, dynamic = self.text, [], False
        while index < len(text):
            char = text[index]
            if char == '"' and not heredoc: return index + 1, "".join(parts), dynamic
            if char == "\\" and index + 1 < len(text):
                following = text[index + 1]
                if following != "\n": parts.append(following if following in '$`"\\' else char + following)
                index += 2
            elif text.startswith("$(", index):
                end = self.substitution(index + 2)
                parts.append(text[index:end])
                index, dynamic = end, True
            elif char == "`":
                end = self.backtick(index + 1)
                parts.append(text[index:end])
                index, dynamic = end, True
            else:
                dynamic = dynamic or char == "$"
                parts.append(char)
                index += 1
        return index, "".join(parts), dynamic

    def sequence(self, index, stop):
        text, length = self.text, len(self.text)
        segment, buffer, in_word, dynamic, quoted = Segment(), [], False, False, False
        pending, heredocs, parens, cases = None, [], 0, 0

        def flush():
            nonlocal buffer, in_word, dynamic, quoted, pending, cases
            if not in_word: return
            word = Word("".join(buffer))
            word.dynamic = dynamic
            if pending is None:
                if not segment.words and not quoted:
                    if word == "case": cases += 1
                    elif word == "esac": cases = max(cases - 1, 0)
                segment.words.append(word)
            elif pending == "in": segment.inputs.append(word)
            elif pending == "herestring": segment.bodies.append(word)
            elif pending in ("heredoc", "heredoc-tabs"): heredocs.append((str(word), pending == "heredoc-tabs", quoted, segment))
            buffer, in_word, dynamic, quoted, pending = [], False, False, False, None

        def finish(keep=True):
            nonlocal segment
            flush()
            if keep: self.segments.append(segment)
            segment = Segment()

        while index < length:
            char = text[index]
            if char in " \t":
                flush()
                index += 1
            elif char == "\n":
                finish()
                index += 1
                for delimiter, tabs, literal, receiver in heredocs:
                    lines = []
                    while index < length:
                        end = text.find("\n", index)
                        end = length if end < 0 else end
                        line = text[index:end]
                        index = end + 1
                        if (line.lstrip("\t") if tabs else line) == delimiter: break
                        lines.append(line)
                    body = "\n".join(lines)
                    if not literal: Parser(body, self.depth + 1).collect_expansions(self.segments)
                    receiver.bodies.append(body)
                heredocs = []
            elif char == "\\":
                if text[index + 1:index + 2] != "\n":
                    buffer.append(text[index + 1:index + 2])
                    in_word = True
                index += 2
            elif char == "'":
                end = text.find("'", index + 1)
                end = length if end < 0 else end
                buffer.append(text[index + 1:end])
                in_word = quoted = True
                index = end + 1
            elif char == '"':
                index, part, expanded = self.expansions(index + 1)
                buffer.append(part)
                in_word = quoted = True
                dynamic = dynamic or expanded
            elif text.startswith("$(", index):
                end = self.substitution(index + 2)
                buffer.append(text[index:end])
                in_word = dynamic = True
                index = end
            elif text.startswith("$'", index):
                end = index + 2
                while end < length and text[end] != "'":
                    end += 2 if text[end] == "\\" else 1
                buffer.append(text[index + 2:end])
                in_word = quoted = True
                index = end + 1
            elif char == "$":
                buffer.append(char)
                in_word = dynamic = True
                index += 1
            elif char == "`":
                end = self.backtick(index + 1)
                buffer.append(text[index:end])
                in_word = dynamic = True
                index = end
            elif char == "#" and not in_word:
                end = text.find("\n", index)
                index = length if end < 0 else end
            elif char == "&" and text.startswith("&>", index):
                flush()
                pending = "out"
                index += 3 if text.startswith("&>>", index) else 2
            elif char in ";|&":
                finish()
                index += 2 if text[index:index + 2] in (";;", "&&", "||", "|&", ";&") else 1
            elif char in "<>":
                if in_word and not quoted and "".join(buffer).isdigit():
                    buffer, in_word = [], False
                flush()
                for token, kind in (("<<<", "herestring"), ("<<-", "heredoc-tabs"), ("<<", "heredoc"), ("<&", "out"), ("<>", "in"),
                                    ("<(", None), ("<", "in"), (">>", "out"), (">&", "out"), (">|", "out"), (">(", None), (">", "out")):
                    if text.startswith(token, index):
                        pending = kind
                        index += len(token) - (1 if token.endswith("(") else 0)
                        break
            elif char == "(":
                finish()
                parens += 1
                index += 1
            elif char == ")":
                if parens:
                    finish()
                    parens -= 1
                elif cases:
                    finish(keep=False)  # a case pattern: its words are data
                elif stop == ")":
                    finish()
                    return index + 1
                else:
                    finish(keep=False)
                index += 1
            else:
                buffer.append(char)
                in_word = True
                index += 1
        finish()
        return length

    def collect_expansions(self, segments):
        self.expansions(0, heredoc=True)
        segments.extend(self.segments)


def is_blocked(command, depth=0):
    return any(segment_blocked(segment, depth) for segment in Parser(command, depth).run())


def segment_blocked(segment, depth):
    if any(sensitive(word) and not quiet_stdin(segment.words, word) for word in segment.inputs): return True
    if command_blocked(segment.words, depth): return True
    if segment.bodies and any(name_of(word) in SHELLS or name_of(word) == "ssh" for word in segment.words):
        return any(is_blocked(body, depth + 1) for body in segment.bodies)
    return False


def quiet_stdin(words, source):
    """A program told to take a credential from /dev/stdin (`--key-file /dev/stdin < a.token`, ACCESS.md) is the safe way
    to pass one; it does not print it. Readers, wrappers and shells could, and a process environment is never a credential."""
    if source.rsplit("/", 1)[-1].lower() in SENSITIVE_NAMES or not words or "/dev/stdin" not in words: return False
    name = name_of(words[0])
    return not (name in PLAIN_READERS or name in PATTERN_READERS or name in WRAPPERS or name in SHELLS or name in RESERVED
                or name in ("ssh", "eval", "env", "tr", "docker", "podman", "docker-compose", "podman-compose"))


def name_of(word):
    return word.rsplit("/", 1)[-1].lower()


def sensitive(word):
    base = word.strip().rstrip("}").rsplit("/", 1)[-1].lower()
    return base in SENSITIVE_NAMES or base.endswith(SENSITIVE_ENDINGS)


def is_flag(word):
    return len(word) > 1 and word.startswith("-") and not word.dynamic


def starts(words, positional, letters="", long_flags=frozenset()):
    """Every index where the wrapped command may begin. An option not known to be a flag may or may not take a value,
    so both readings count: a wrong guess must not hide a command."""
    found, index = set(), 0
    while index < len(words):
        word = words[index]
        if word == "--":
            found.add(index + 1 + positional)
            break
        if not is_flag(word):
            found.add(index + positional)
            break
        known = "=" in word or word in long_flags or (not word.startswith("--") and all(char in letters for char in word[1:]))
        if known:
            index += 1
        else:
            found.add(index + 1 + positional)
            index += 2
    return found


def any_start(words, positional, depth, letters="", long_flags=frozenset()):
    return any(command_blocked(words[start:], depth + 1) for start in starts(words, positional, letters, long_flags))


def command_blocked(words, depth):
    if depth > MAX_DEPTH: raise ValueError("nesting too deep")
    words = list(words)
    while words and (words[0] in RESERVED or ASSIGNMENT.match(words[0])):
        words.pop(0)
    if not words: return False
    if words[0] in DATA_LED: return False
    if re.search(r"\s", words[0]): return is_blocked(" ".join(words), depth + 1)
    name, args = name_of(words[0]), words[1:]
    if name in SHELLS: return shell_blocked(args, depth)
    if name == "ssh": return ssh_blocked(args, depth)
    if name == "eval": return is_blocked(" ".join(args), depth + 1)
    if name in ("docker", "podman"): return docker_blocked(args, depth)
    if name in ("docker-compose", "podman-compose"): return compose_blocked(args, depth)
    if name == "systemctl": return systemctl_blocked(args)
    if name in PLAIN_READERS: return any(sensitive(arg) or ("=" in arg and sensitive(arg.split("=", 1)[1])) for arg in args)
    if name in PATTERN_READERS: return pattern_reader_blocked(name, args)
    if name == "printenv": return True
    if name == "env": return env_blocked(args, depth)
    if name == "set": return not args
    if name == "export": return all(arg == "-p" for arg in args)
    if name == "find":
        return any(arg in ("-exec", "-execdir", "-ok", "-okdir") and command_blocked(args[index + 1:], depth + 1)
                   for index, arg in enumerate(args))
    if name == "command" and any(arg in ("-v", "-V") for arg in args): return False
    if name in WRAPPERS:
        positional, letters = WRAPPERS[name]
        return any_start(args, positional, depth, letters)
    return False


def shell_blocked(args, depth):
    for index, arg in enumerate(args):
        if arg.startswith("--command="): return is_blocked(arg.split("=", 1)[1], depth + 1)
        if is_flag(arg) and not arg.startswith("--") and "c" in arg[1:] and index + 1 < len(args):
            return is_blocked(args[index + 1], depth + 1)
    return False


def ssh_blocked(args, depth):
    index = 0
    while index < len(args) and is_flag(args[index]):
        word = args[index]
        if word == "--":
            index += 1
            break
        takes_value = word[-1] in SSH_VALUE and all(char in SSH_FLAG for char in word[1:-1])
        index += 2 if takes_value else 1
    return is_blocked(" ".join(args[index + 1:]), depth + 1)


def subcommand(args, value_flags):
    """The index of the first word that names a subcommand. An expansion may hold options, so it never counts."""
    index = 0
    while index < len(args):
        word = args[index]
        if word.dynamic:
            index += 1
        elif is_flag(word):
            index += 2 if word in value_flags else 1
        else:
            return index
    return None


def docker_blocked(args, depth):
    index = subcommand(args, DOCKER_VALUE)
    if index is None: return False
    name, rest = args[index], args[index + 1:]
    if name == "compose": return compose_blocked(rest, depth)
    if name in ("container", "service") and rest:
        name, rest = rest[0], rest[1:]
    if name == "inspect": return inspect_blocked(rest)
    if name in ("exec", "run"): return exec_blocked(rest, depth)
    return False


def compose_blocked(args, depth):
    index = subcommand(args, COMPOSE_VALUE)
    if index is None: return False
    name, rest = args[index], args[index + 1:]
    if name in ("config", "convert"):
        if "--environment" in rest: return True
        if "--no-env-resolution" in rest: return False
        return not any(arg == flag or arg.startswith(flag + "=") for arg in rest for flag in COMPOSE_LIMITED)
    if name in ("exec", "run"): return exec_blocked(rest, depth)
    return False


def exec_blocked(args, depth):
    if any(PROC_ENVIRON.search(arg) for arg in args): return True
    entrypoint = None
    for index, arg in enumerate(args):
        if arg == "--entrypoint" and index + 1 < len(args): entrypoint = args[index + 1]
        elif arg.startswith("--entrypoint="): entrypoint = Word(arg.split("=", 1)[1])
    for start in starts(args, 1, EXEC_FLAG_LETTERS, EXEC_LONG_FLAGS):
        inner = args[start:]
        if inner[:1] == ["--"]: inner = inner[1:]
        if entrypoint is not None: inner = [entrypoint, *inner]
        if command_blocked(inner, depth + 1): return True
    return False


def inspect_blocked(args):
    templates, index = [], 0
    while index < len(args):
        word = args[index]
        if word.startswith(("--format=", "-f=")) or (word.startswith("-f") and word != "-f" and not word.startswith("--")):
            return True
        if word == "--format" or re.fullmatch(r"-[a-z]*f", word):
            if index + 1 >= len(args): return True
            templates.append(args[index + 1])
            index += 1
        index += 1
    return not templates or any(template_blocked(template) for template in templates)


def template_blocked(template):
    actions = TEMPLATE_ACTION.findall(template)
    if template.dynamic or not actions: return True
    return any(TEMPLATE_SECRET.search(TEMPLATE_STRING.sub('""', action)) for action in actions)


def systemctl_blocked(args):
    if "show-environment" in args: return True
    if "show" not in args: return False
    properties, index = [], 0
    while index < len(args):
        word = args[index]
        if word in ("-p", "-P", "--property"):
            properties.append(args[index + 1] if index + 1 < len(args) else "")
            index += 1
        elif word.startswith("--property="): properties.append(word.split("=", 1)[1])
        elif re.match(r"-[pP].", word): properties.append(word[2:])
        index += 1
    if not properties: return True
    return any(part.strip().lower() == "environment" for value in properties for part in value.split(","))


def env_blocked(args, depth):
    index = 0
    while index < len(args):
        word = args[index]
        if word in ("-S", "--split-string") and index + 1 < len(args): return is_blocked(args[index + 1], depth + 1)
        if word in ("-u", "--unset", "-C", "--chdir"): index += 2
        elif is_flag(word) or ASSIGNMENT.match(word): index += 1
        else: return command_blocked(args[index:], depth + 1)
    return True


def path_value_sensitive(value):
    return not value.startswith("!") and sensitive(value)


def pattern_reader_blocked(name, args):
    letters, explicit, positional, index = PATTERN_READERS[name], False, [], 0
    while index < len(args):
        word = args[index]
        index += 1
        if word == "--":
            positional.extend(args[index:])
            break
        if word.startswith("--"):
            key, equals, value = word.partition("=")
            if not equals and key in LONG_VALUE and index < len(args):
                value = args[index]
                index += 1
            elif not equals:
                continue
            explicit = explicit or key in LONG_EXPLICIT
            if key in LONG_PATH and path_value_sensitive(value): return True
        elif is_flag(word):
            for position, char in enumerate(word[1:], 1):
                if char not in letters: continue
                value = word[position + 1:]
                if not value and index < len(args):
                    value = args[index]
                    index += 1
                explicit = explicit or char in "ef"
                if char in "fg" and path_value_sensitive(value): return True
                break
        else:
            positional.append(word)
    return any(sensitive(word) for word in (positional if explicit else positional[1:]))


def decide(raw):
    """Returns (exit code, stderr text). The text is one of two constants, so it can never hold the command."""
    try:
        if len(raw) > MAX_INPUT: return 2, INTERNAL
        command = json.loads(raw.decode("utf-8"))["tool_input"]["command"]
        if not isinstance(command, str) or not command: return 2, INTERNAL
        return (2, REFUSAL) if is_blocked(command) else (0, "")
    except Exception:
        return 2, INTERNAL


def main():
    code, message = decide(sys.stdin.buffer.read(MAX_INPUT + 1))
    if message: print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
