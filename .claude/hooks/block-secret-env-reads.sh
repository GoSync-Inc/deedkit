#!/bin/sh
# PreToolUse(Bash): refuse a command that prints a container's environment or a private env file (ANYM-774).
# Exit 0 allows, exit 2 blocks; every failure here blocks. The matcher reads the tool call from stdin and is bounded by
# its own deadline, so this does not rely on the runtime's timeout handling. Uses shell builtins only besides the two tools.
fail() {
  echo "Blocked by .claude/hooks/block-secret-env-reads.sh: $1, so the command was not checked and does not run (a session transcript goes to the model provider). See SECURITY.md." >&2
  exit 2
}
case $0 in */*) dir=${0%/*} ;; *) dir=. ;; esac
if command -v timeout >/dev/null 2>&1; then bound=timeout
elif command -v gtimeout >/dev/null 2>&1; then bound=gtimeout
else fail "neither timeout nor gtimeout (coreutils) is on PATH"
fi
command -v python3 >/dev/null 2>&1 || fail "python3 is not on PATH"
"$bound" 5 python3 -I "$dir/block_secret_env_reads.py"
status=$?
case $status in
  0) exit 0 ;;
  2) exit 2 ;;
  124) fail "the matcher passed its 5 second deadline" ;;
  *) fail "the matcher failed" ;;
esac
