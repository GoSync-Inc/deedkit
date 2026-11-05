#!/bin/sh
# PreToolUse(Bash): refuse a command that prints a container's environment or a private env file (ANYM-774).
# Exit 0 allows, exit 2 blocks; every failure here blocks. The matcher reads the tool call from stdin and is bounded by
# its own deadline, so this does not rely on the runtime's timeout handling. A broken hook stops all Bash, so each
# refusal says what it is, and SECURITY.md names the recovery. Uses shell builtins only besides the two tools.
broken() {
  echo "Blocked by .claude/hooks/block-secret-env-reads.sh: the hook itself is broken on this host ($1), so the command was not checked and does not run. This is not a secret refusal. Recovery: the owner's ! prefix, or the Edit tool on .claude/settings.json (SECURITY.md, Agent sessions)." >&2
  exit 2
}
case $0 in */*) dir=${0%/*} ;; *) dir=. ;; esac
bound=
# A non-login shell on a Mac has no Homebrew on PATH (Mac mini, 11-05), hence the two known directories.
for tool in timeout gtimeout /opt/homebrew/bin/timeout /opt/homebrew/bin/gtimeout /usr/local/bin/timeout /usr/local/bin/gtimeout; do
  if command -v "$tool" >/dev/null 2>&1; then bound=$tool; break; fi
done
[ -n "$bound" ] || broken "neither timeout nor gtimeout (coreutils) was found"
command -v python3 >/dev/null 2>&1 || broken "python3 is not on PATH"
"$bound" 5 python3 -I "$dir/block_secret_env_reads.py"
status=$?
case $status in
  0) exit 0 ;;
  2) exit 2 ;;
  124)
    echo "Blocked by .claude/hooks/block-secret-env-reads.sh: this is a load refusal, not a secret refusal. The check did not finish within its 5 second deadline, usually because the host is busy, so the command did not run. Retry the same command. If it repeats, see SECURITY.md, Agent sessions." >&2
    exit 2 ;;
  *) broken "the matcher failed with status $status" ;;
esac
