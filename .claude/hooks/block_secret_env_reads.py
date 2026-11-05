"""Decide whether a Bash tool call would print a container's environment or a private env file (ANYM-774).

Reads the tool-call JSON from stdin; exit 0 allows, exit 2 blocks with a refusal on stderr. Any doubt blocks. It guards
against an accident by a diagnosing agent and is no sandbox: SECURITY.md names what it does not cover. It parses the
command the way a shell splits it, so a quoted message is data and a command inside ssh, sudo, sh -c or $(...) is not.
The refusal never holds the command, which may itself hold the secret. Standard library only: it runs under python3 -I.
"""
import json
import re
import shlex
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
SSH_VALUE = "BbcDEeFIiJLlmOoPpQRSWw"
SYSTEMCTL_VALUE = "tsnoHM"
DOCKER_VALUE = frozenset({"--context", "-c", "--host", "-H", "--config", "--log-level", "-l", "--tlscacert", "--tlscert", "--tlskey"})
COMPOSE_VALUE = frozenset({"-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory", "--workdir",
                           "--ansi", "--parallel", "--progress"})
EXEC_FLAG_LETTERS = "ditTP"
EXEC_LONG_FLAGS = frozenset({"--rm", "--privileged", "--no-deps", "--detach", "--interactive", "--tty", "--init", "--read-only",
                             "--service-ports", "--build", "--quiet-pull", "--remove-orphans", "--use-aliases", "--no-TTY",
                             "--publish-all", "--dry-run"})
COMPOSE_LIMITED = ("--services", "--images", "--quiet", "-q", "--volumes")
TRUE_VALUES = frozenset({"1", "t", "T", "TRUE", "true", "True"})
PROC_ENVIRON = re.compile(r"/proc/[^/\s]+/environ")
SENSITIVE_ENDINGS = (".env", ".dsn", ".token", ".key", ".pem", ".agekey")
SENSITIVE_NAMES = frozenset({"environ", "config.v2.json"})
QUIET_STDIN_PROGRAMS = frozenset({"python", "python3", "uv"})
PLAIN_READERS = frozenset({"cat", "less", "more", "head", "tail", "strings", "xxd", "base64"})
# Pattern-first readers: their first positional word is a pattern or a program, not a path. (short options that take a
# value, long options that take the next word). List only what surely consumes a word on every platform: an option
# wrongly listed hides the file, one wrongly left out only reads a pattern as a path, which at worst refuses.
GREP_OPTIONS = ("ABDdefm", frozenset({"--regexp", "--file", "--include", "--exclude", "--exclude-dir", "--max-count",
                                      "--after-context", "--before-context"}))
AWK_OPTIONS = ("Fvfe", frozenset({"--file", "--field-separator", "--assign"}))
PATTERN_READERS = {"grep": GREP_OPTIONS, "egrep": GREP_OPTIONS, "fgrep": GREP_OPTIONS,
                   "rg": ("ABCefgjmMrtTE", frozenset({"--regexp", "--file", "--glob", "--iglob", "--max-count", "--context",
                                                      "--after-context", "--before-context", "--type", "--type-not", "--color"})),
                   "sed": ("ef", frozenset({"--expression", "--file"})), "awk": AWK_OPTIONS, "gawk": AWK_OPTIONS}
LONG_EXPLICIT = frozenset({"--regexp", "--file", "--expression"})
LONG_PATH = frozenset({"--file", "--include", "--glob", "--iglob"})
TEMPLATE_ACTION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
TEMPLATE_STRING = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`')
TEMPLATE_SECRET = re.compile(r"\.(?:Container)?Config\b|\.Spec\b|(?<![\w)\]$])\.(?!\w)|\$(?![\w.])", re.IGNORECASE)


def trailing_backslashes(line):
    return len(line) - len(line.rstrip("\\"))


class Word(str):
    """A shell word after quote removal; dynamic: it holds an expansion whose value this hook cannot see; quoted: some of
    it was quoted, so it is never a reserved word."""
    dynamic = False
    quoted = False


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

    def arithmetic_end(self, start):
        """The index after the `))` of arithmetic opening with `((` at start, or None when it is commands. As bash reads it:
        the inner parenthesis closes directly before the outer one. A doubtful region with a later `<<` refuses, because
        reading a shift as a here-document would swallow the commands after it."""
        text, depth = self.text, 0
        for index in range(start + 1, len(text)):
            char = text[index]
            if char in "'\"": break
            if char == "(": depth += 1
            elif char == ")":
                depth -= 1
                if depth: continue
                if text[index + 1:index + 2] == ")": return index + 2
                break
        if "<<" in text[start:]: raise ValueError("doubtful arithmetic")
        return None

    def dollar_paren(self, index):
        """Reads $(...) or $((...)) at index; `<<` in arithmetic is a shift, never a here-document."""
        end = self.arithmetic_end(index + 1) if self.text.startswith("$((", index) else None
        if end is None: return self.substitution(index + 2)
        Parser(self.text[index + 3:end - 2], self.depth + 1).collect_expansions(self.segments)
        return end

    def parameter(self, index):
        """Reads ${...} at index: an operator such as `<` or `#` inside it is not shell syntax."""
        text, depth, start = self.text, 0, index
        while index < len(text):
            if text.startswith("$(", index):
                index = self.dollar_paren(index)
                continue
            if text[index] == "`":
                index = self.backtick(index + 1)
                continue
            if text[index] == "\\":
                index += 2
                continue
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
            if depth == 0: break
        return index, text[start:index]

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
            elif text.startswith("${", index):
                index, part = self.parameter(index)
                parts.append(part)
                dynamic = True
            elif text.startswith("$(", index):
                end = self.dollar_paren(index)
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
        pending, heredocs, parens, cases, pattern = None, [], 0, 0, False

        def flush():
            nonlocal buffer, in_word, dynamic, quoted, pending, cases, pattern
            if not in_word: return
            word = Word("".join(buffer))
            word.dynamic, word.quoted = dynamic, quoted
            if pending is None:
                if not segment.words and not quoted and word == "esac":
                    cases, pattern = max(cases - 1, 0), False
                segment.words.append(word)
                command = without_prefix(segment.words)
                if word == "in" and not quoted and len(command) == 3 and command[0] == "case" and not command[0].quoted:
                    cases, pattern = cases + 1, True
                    segment.words = []
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
                        while not literal and index <= length and trailing_backslashes(line) % 2:
                            end = text.find("\n", index)
                            end = length if end < 0 else end
                            line = line[:-1] + text[index:end]
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
                    in_word = quoted = True
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
            elif text.startswith("${", index):
                index, part = self.parameter(index)
                buffer.append(part)
                in_word = dynamic = True
            elif text.startswith("$(", index):
                end = self.dollar_paren(index)
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
            elif char == "|" and pattern:
                flush()
                index += 1
            elif char in ";|&":
                finish()
                operator = text[index:index + 3] if text.startswith(";;&", index) else text[index:index + 2]
                index += len(operator) if operator in (";;", ";;&", "&&", "||", "|&", ";&") else 1
                if operator in (";;", ";;&", ";&"): pattern = cases > 0
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
            elif char == "(" and pattern:
                index += 1  # the optional opening of a case pattern
            elif text.startswith("((", index) and not in_word and (closing := self.arithmetic_end(index)) is not None:
                Parser(text[index + 2:closing - 2], self.depth + 1).collect_expansions(self.segments)
                index = closing
            elif char == "(":
                finish()
                parens += 1
                index += 1
            elif char == ")":
                if pattern:
                    finish(keep=False)  # a case pattern: its words are data
                    pattern = False
                elif parens:
                    finish()
                    parens -= 1
                elif stop == ")":
                    finish()
                    return index + 1
                else:
                    raise ValueError("unmatched parenthesis")  # a shell refuses it too; a misread must not hide a command
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
    try:
        return any(segment_blocked(segment, depth) for segment in Parser(command, depth).run())
    except (ValueError, RecursionError):
        return True  # it cannot be read as a shell would read it, so it is refused


def segment_blocked(segment, depth):
    if any(sensitive(word) and not quiet_stdin(segment.words, word) for word in segment.inputs): return True
    if command_blocked(segment.words, depth): return True
    if segment.bodies and any(name_of(word) in SHELLS or name_of(word) == "ssh" for word in segment.words):
        return any(is_blocked(body, depth + 1) for body in segment.bodies)
    return False


def quiet_stdin(words, source):
    """The service CLI told to take a credential from /dev/stdin (`python -m messaging_service ... --key-file /dev/stdin
    < a.token`, ACCESS.md) is the safe way to pass one: it does not print it. Nothing else is excepted."""
    words = without_prefix(words)
    if source.rsplit("/", 1)[-1].lower() in SENSITIVE_NAMES or not words: return False
    return name_of(words[0]) in QUIET_STDIN_PROGRAMS and "messaging_service" in words and "/dev/stdin" in words


def without_prefix(words):
    words = list(words)
    while words and (words[0] in RESERVED or ASSIGNMENT.match(words[0])):
        words.pop(0)
    return words


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
    found, stack, seen = set(), [0], set()
    while stack:
        index = stack.pop()
        if index in seen or index >= len(words): continue
        seen.add(index)
        word = words[index]
        if word == "--":
            found.add(index + 1 + positional)
        elif not is_flag(word):
            found.add(index + positional)
        elif "=" in word or word in long_flags or (not word.startswith("--") and all(char in letters for char in word[1:])):
            stack.append(index + 1)
        else:
            stack.extend((index + 1, index + 2))
    return found


def any_start(words, positional, depth, letters="", long_flags=frozenset()):
    return any(command_blocked(words[start:], depth + 1) for start in starts(words, positional, letters, long_flags))


def command_blocked(words, depth):
    if depth > MAX_DEPTH: raise ValueError("nesting too deep")
    words = without_prefix(words)
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


def ssh_options(args, index):
    """The index after the options at index, reading clusters as getopt does."""
    while index < len(args) and is_flag(args[index]):
        word = args[index]
        index += 1
        if word == "--": break
        for position, char in enumerate(word[1:], 1):
            if char in SSH_VALUE:
                if position == len(word) - 1: index += 1
                break
    return index


def ssh_blocked(args, depth):
    index = ssh_options(args, ssh_options(args, 0) + 1)
    return is_blocked(" ".join(args[index:]), depth + 1)


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
        if flag_state(rest, "--environment") is not False: return True
        if flag_state(rest, "--no-env-resolution"): return False
        if any(arg.startswith("--hash=") and len(arg) > 7 or arg == "--hash" for arg in rest): return False
        return not any(flag_state(rest, flag) for flag in COMPOSE_LIMITED)
    if name in ("exec", "run"): return exec_blocked(rest, depth)
    return False


def flag_state(args, flag):
    """A boolean option's last setting: False when absent or off, True when on, None when an expansion hides it."""
    state = False
    for arg in args:
        if arg == flag: state = True
        elif arg.startswith(flag + "="): state = None if arg.dynamic else arg.split("=", 1)[1] in TRUE_VALUES
    return state


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
        if word.startswith("--format=") or re.match(r"-s*f=", word): return True
        attached = re.fullmatch(r"-s*f(.+)", word, re.DOTALL)
        if attached:
            template = Word(attached.group(1))
            template.dynamic = word.dynamic
            templates.append(template)
        elif word == "--format" or re.fullmatch(r"-s*f", word):
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
        if word == "--property":
            properties.append(args[index + 1] if index + 1 < len(args) else "")
            index += 1
        elif word.startswith("--property="): properties.append(word.split("=", 1)[1])
        elif word.startswith("-") and not word.startswith("--"):
            for position, char in enumerate(word[1:], 1):
                if char in "pP" or char in SYSTEMCTL_VALUE:
                    value = word[position + 1:]
                    if not value and index + 1 < len(args):
                        value = args[index + 1]
                        index += 1
                    if char in "pP": properties.append(value)
                    break
        index += 1
    if not any(part.strip() for value in properties for part in value.split(",")): return True
    return any(part.strip().lower() == "environment" for value in properties for part in value.split(","))


def env_split_word(token):
    word = Word(token)
    word.dynamic = "$" in token  # env -S expands ${VAR} itself
    return word


def env_blocked(args, depth):
    index = 0
    while index < len(args):
        word = args[index]
        split = re.fullmatch(r"(?:-[iv0]*S|--split-string=?)(.*)", word, re.DOTALL)
        if split:
            # env splits only this payload, with no shell; the words after it stay literal arguments.
            tail = args[index + 1:] if split.group(1) else args[index + 2:]
            payload = split.group(1) or (args[index + 1] if index + 1 < len(args) else "")
            return command_blocked([*map(env_split_word, shlex.split(payload)), *tail], depth + 1)
        if word in ("-u", "--unset", "-C", "--chdir"): index += 2
        elif is_flag(word) or ASSIGNMENT.match(word): index += 1
        else: return command_blocked(args[index:], depth + 1)
    return True


def path_value_sensitive(value):
    return not value.startswith("!") and sensitive(value)


def pattern_reader_blocked(name, args):
    (letters, long_value), explicit, positional, index = PATTERN_READERS[name], False, [], 0
    listing, found = False, False
    while index < len(args):
        word = args[index]
        index += 1
        if word == "--":
            positional.extend(args[index:])
            break
        if word.startswith("--"):
            key, equals, value = word.partition("=")
            listing = listing or (name == "rg" and word == "--files")  # lists names; reads no content
            if not equals and key in long_value and index < len(args):
                value = args[index]
                index += 1
            elif not equals:
                continue
            explicit = explicit or key in LONG_EXPLICIT
            found = found or (key in LONG_PATH and path_value_sensitive(value))
        elif is_flag(word):
            for position, char in enumerate(word[1:], 1):
                if char not in letters: continue
                value = word[position + 1:]
                if not value and index < len(args):
                    value = args[index]
                    index += 1
                explicit = explicit or char in "ef"
                found = found or (char in "fg" and path_value_sensitive(value))
                break
        else:
            positional.append(word)
    if listing: return False
    return found or any(sensitive(word) for word in (positional if explicit else positional[1:]))


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
