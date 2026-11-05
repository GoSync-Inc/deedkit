"""Decide whether a Bash tool call would print a container's environment or a private env file (ANYM-774).

Reads the tool-call JSON from stdin; exit 0 allows, exit 2 blocks with a refusal on stderr. The threat is an agent's
accidental read while diagnosing, not a deliberate bypass (SECURITY.md). So this does not parse shell, which three
review rounds showed is the defect: it matches the raw text, conservatively normalised, and a false refusal is the
accepted cost. Quotes are dropped and separators split the text even inside quotes, so ssh, sudo and sh -c need no
handling and no misread syntax can hide a later line. The refusal never holds the command. Standard library only.
"""
from fnmatch import fnmatchcase
import json
import re
import sys

MAX_INPUT = 256 * 1024
REFUSAL = (
    "Blocked by .claude/hooks/block-secret-env-reads.sh: this command can print a container's environment or a private "
    "env file, and a session transcript goes to the model provider. Allowed ways: docker inspect with --format "
    "'<template>' limited to .State, .Name, .Image, .RestartCount or .Created; docker compose config --services, "
    "--images or --hash; for key names only, ask the orchestrator for a key-names read. The hook reads raw text and "
    "refuses some harmless commands: rewrite without ${...}, arithmetic, eval or env -S near these names, run one plain "
    "command at a time, and pass prose through a file (git commit -F, gh --body-file). See SECURITY.md."
)
INTERNAL = (
    "Blocked by .claude/hooks/block-secret-env-reads.sh: the hook could not read this tool call, and it fails closed. "
    "A session transcript goes to the model provider, so an unchecked command does not run. Allowed way: run one plain "
    "command under 256 KiB. See SECURITY.md."
)

SEPARATOR = re.compile(r"\$\(|[;&|\n()`]")
TEMPLATE_ACTION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
WRITE_REDIRECT = re.compile(r"[0-9&]*>+[&|]?\s*\S*")
INPUT_REDIRECT = re.compile(r"(?<!<)<(?![<(])\s*(\S+)")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*\+?=")
RESERVED = frozenset({"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"})
# A chunk led by one of these may run another command, so its later words count; any other leader's words are data.
RUNNERS = frozenset({"ssh", "sudo", "sh", "bash", "zsh", "dash", "ksh", "ash", "su", "runuser", "eval", "watch", "xargs",
                     "timeout", "gtimeout", "nice", "ionice", "nohup", "time", "command", "exec", "stdbuf", "setsid", "flock",
                     "chroot", "find", "env", "uv", "docker", "podman", "docker-compose", "podman-compose"})
PLAIN_READERS = frozenset({"cat", "less", "more", "head", "tail", "strings", "xxd", "base64", "nl", "tac", "od", "hexdump", "bat",
                           "cut", "sort", "uniq"})
PATTERN_READERS = frozenset({"grep", "egrep", "fgrep", "rg", "sed", "awk", "gawk"})
ACTIVE = RUNNERS | PLAIN_READERS | PATTERN_READERS | {"printenv", "set", "export", "systemctl"}
SENSITIVE_ENDINGS = (".env", ".dsn", ".token", ".key", ".pem", ".agekey")
SENSITIVE_NAMES = frozenset({"environ", "config.v2.json"})
# What a glob is tried against: `.env*` selects a private file although it does not end like one. `*` alone is not refused.
SENSITIVE_SAMPLES = (".env", "x.env", "postgres.env", "x.dsn", "x.token", "x.key", "x.pem", "x.agekey", "environ", "config.v2.json")
# A pattern reader given one of these directories reads the private files in it. /private/tmp is the macOS temp directory.
PRIVATE_DIRECTORY = re.compile(r"(?:^|/)(?:private|\.local)(?:/|$)|^/etc/deedkit(?:/|$)")
MACOS_SYSTEM = re.compile(r"^/private/(?:tmp|var|etc)(?:/|$)")
# A reader's quoted pattern is data: its separators must not cut the reader off from its file.
READER_PATTERN = re.compile(r"""(\b(?:grep|egrep|fgrep|rg|sed|awk|gawk)\b(?:\s+-\S+(?:\s+\d+)?)*\s+(?:--regexp=|--expression=)?)(['"])(.*?)\2""",
                            re.DOTALL)
# An attached pattern option leaves no pattern among the positional words, so the first of them is a path.
EXPLICIT_PATTERN = re.compile(r"--(?:regexp|expression|file)=.*|-[A-Za-z]*[ef].+")
PROC_ENVIRON = re.compile(r"/proc/[^/\s]+/environ")
# --no-env-resolution and --no-interpolate are absent on purpose: each still prints values (SECURITY.md).
COMPOSE_LIMITED = frozenset({"--services", "--images", "--quiet", "-q", "--volumes"})
TEMPLATE_SECRET = re.compile(r"\.(?:Container)?Config\b|\.Spec\b", re.IGNORECASE)
TEMPLATE_ROOT = re.compile(r"(?<![\w)\]$])\.(?!\w)|\$(?![\w.])")
# Syntax this hook does not read, refused on a line that also holds a name it guards.
UNREADABLE = re.compile(r"\$\{|\(\(|\$'|\beval\b|\benv\s+(?:-\w*S|--split-string)|\bbase64\s+(?:-\w*d|--decode)|\bxxd\s+-\w*r")
GUARDED_NAME = re.compile(r"\b(?:docker|podman|compose|printenv|systemctl)\b|(?<![\w-])env(?![\w-])|environ|config\.v2|\.(?:env|dsn|token|key|pem|agekey)\b",
                          re.IGNORECASE)


def one_word(match):
    prefix, quote, pattern = match.groups()
    if "$(" in pattern or "`" in pattern: return match.group(0)  # a substitution in it runs, so it stays visible
    return prefix + quote + re.sub(r"[;&|()\s]+", ",", pattern) + quote


def normalise(command):
    """Join continued lines, then drop quotes and backslashes: `doc''ker` and `\\"x\\"` read as what a shell would run."""
    # A run of quotes is how a shell nests a quote, at any depth: `'"'"'`, `'\"'\"'`.
    joined = re.sub(r"['\"]{2,}", '"', command.replace("\\\n", "").replace("\\", ""))
    text = READER_PATTERN.sub(one_word, joined)
    text = TEMPLATE_ACTION.sub(lambda match: "{{" + re.sub(r"[;&|()]", " ", match.group(1)) + "}}", text)
    return re.sub(r"[\"'\\]", "", text)


def name_of(word):
    return word.rsplit("/", 1)[-1].lower()


def sensitive(word):
    base = word.strip().rstrip("}").rsplit("/", 1)[-1].lower()
    if base in SENSITIVE_NAMES or base.endswith(SENSITIVE_ENDINGS): return True
    if base.startswith(".env.") and base not in (".env.example", ".env.sample"): return True  # .gitignore keeps these private
    return base != "*" and any(char in base for char in "*?[") and any(fnmatchcase(sample, base) for sample in SENSITIVE_SAMPLES)


def private_directory(word):
    return bool(PRIVATE_DIRECTORY.search(word)) and not MACOS_SYSTEM.match(word)


def input_blocked(chunk):
    """A private file fed to stdin. The service CLI told to read a credential from /dev/stdin (ACCESS.md) is the one
    exception: it passes the value without printing it."""
    for source in INPUT_REDIRECT.findall(chunk):
        if not sensitive(source): continue
        if "environ" in source or "messaging_service" not in chunk or "/dev/stdin" not in chunk: return True
    return False


def env_runs_a_command(words):
    """env prints the environment unless a word after its options and assignments names a command."""
    index = 0
    while index < len(words):
        word = words[index]
        if word in ("-u", "--unset", "-C", "--chdir"): index += 2
        elif word.startswith("-") or ASSIGNMENT.match(word): index += 1
        else: return True
    return False


def bare_environment(words):
    """env, printenv, set or export used to print, at any position. `env A=1 cmd` and `set -e` print nothing."""
    for index, word in enumerate(words):
        name = name_of(word)
        following = words[index + 1] if index + 1 < len(words) else None
        if name == "printenv": return True
        if name == "env" and not env_runs_a_command(words[index + 1:]): return True
        if name == "set" and following is None: return True
        if name == "export" and (following is None or following == "-p"): return True
    return False


def inspect_blocked(chunk, words):
    tail = words[words.index("inspect") + 1:]
    text = " ".join(tail)
    if re.search(r"(?:--format|-f)=|--format\s+json\b", text): return True
    if not re.search(r"(?:^|\s)(?:--format\s+|-f\s*)\S*\{\{", text) or TEMPLATE_SECRET.search(chunk) or "}}$" in text: return True
    return any(TEMPLATE_ROOT.search(action) for action in TEMPLATE_ACTION.findall(text))


def compose_config_blocked(words):
    tail = words[next(index for index, word in enumerate(words) if word in ("config", "convert")) + 1:]
    if any(word.startswith("--environment") for word in tail): return True
    return not any(word in COMPOSE_LIMITED or word.startswith("--hash") for word in tail)


def container_blocked(chunk, words):
    if not any(name_of(word) in ("docker", "podman", "docker-compose", "podman-compose") for word in words): return False
    names = [name_of(word) for word in words]
    if "inspect" in names and "compose" not in names and "docker-compose" not in names:
        # An image, network or volume holds no running container's environment (the card names containers).
        if names[names.index("inspect") - 1] in ("image", "network", "volume"): return False
        return inspect_blocked(chunk, names)
    if "config" in names or "convert" in names:
        if "compose" in names or "docker-compose" in names or "podman-compose" in names: return compose_config_blocked(names)
    if "exec" in names or "run" in names:
        return bool(PROC_ENVIRON.search(chunk)) or bool(re.search(r"--entrypoint[= ](?:\S*/)?(?:env|printenv)\b", chunk))
    return False


def reader_blocked(words):
    """A reader of a private path. A pattern reader's first positional word is its pattern or program, not a path."""
    for index, word in enumerate(words):
        name = name_of(word)
        rest = words[index + 1:]
        if name in PLAIN_READERS and any(sensitive(item) or sensitive(item.split("=", 1)[-1]) for item in rest): return True
        if name not in PATTERN_READERS: continue
        positional, after_dashes = [], False
        explicit = any(EXPLICIT_PATTERN.fullmatch(item) for item in rest)
        for position, item in enumerate(rest):
            if after_dashes or not item.startswith("-"): positional.append(item)
            elif item == "--": after_dashes = True
            elif item in ("-f", "--file") and sensitive(rest[position + 1] if position + 1 < len(rest) else ""): return True
            elif re.match(r"--(?:file|include|glob|iglob)=", item) and sensitive(item.split("=", 1)[1]): return True
        paths = positional if explicit else positional[1:]
        if any(sensitive(item) or private_directory(item) for item in paths): return True
    return False


def systemctl_blocked(chunk, words):
    if "systemctl" not in [name_of(word) for word in words]: return False
    if "show-environment" in words: return True
    if "show" not in words: return False
    if "environment" in chunk.lower(): return True
    return not any(word == "--property" or word.startswith("--property=") or re.match(r"-[a-zA-Z]*[pP]", word) for word in words)


def chunk_blocked(chunk):
    if input_blocked(chunk): return True
    words = WRITE_REDIRECT.sub(" ", chunk).replace("<", " ").split()
    while words and (words[0] in RESERVED or ASSIGNMENT.match(words[0])):
        words.pop(0)
    if not words or name_of(words[0]) not in ACTIVE: return False
    head = name_of(words[0])
    if head not in RUNNERS: words = words if head in PLAIN_READERS | PATTERN_READERS | {"systemctl"} else words[:2]
    return (bare_environment(words) or container_blocked(chunk, words) or reader_blocked(words)
            or systemctl_blocked(chunk, words))


def is_blocked(command):
    text = normalise(command)
    if any(UNREADABLE.search(line) and GUARDED_NAME.search(line) for line in text.split("\n")): return True
    return any(chunk_blocked(chunk) for chunk in SEPARATOR.split(text))


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
