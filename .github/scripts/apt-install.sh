#!/usr/bin/env bash
# Install Ubuntu packages on a GitHub-hosted runner through the first apt mirror that answers.
#
#   apt-install.sh <package>...
#
# Outage 2026-11-11 (from about 13:57Z): every Canonical host (azure.archive.ubuntu.com, archive.ubuntu.com,
# security.ubuntu.com) accepted no connection from the runners for hours, while packages.microsoft.com and the
# university mirrors answered. The old step retried on archive.ubuntu.com, which was down too, so every
# python/verify/services job failed after 5 minutes of 20-second timeouts.
#
# Each mirror is probed first (one small request, 5 s to connect), so a dead one costs seconds, not an apt
# timeout. The first candidate is the runner image's own list, left untouched; the next ones replace it with a
# single mirror: archive.ubuntu.com, then mirrors that do not depend on Canonical's network. An apt run that
# stalls or fails moves on to the next answering mirror (a retry on the same mirror would stall again). Package
# lists are signed by the Ubuntu archive key, which apt checks, so a third-party mirror can serve only genuine files.
# The security pocket is probed too: a mirror without it is skipped. x64 images only (/ubuntu paths).
#
# Each apt run keeps the old bounds: 90 s for `update`, 150 s for `install`, 20 s network timeouts.
#
# Environment (tests set them; a runner needs none): APT_ETC (default /etc/apt), APT_OS_RELEASE (default
# /etc/os-release), APT_MAX_ATTEMPTS (default 3).
set -uo pipefail

[ "$#" -gt 0 ] || { echo "usage: apt-install.sh <package>..." >&2; exit 2; }
etc="${APT_ETC:-/etc/apt}"
max_attempts="${APT_MAX_ATTEMPTS:-3}"
codename="$(sed -n 's/^VERSION_CODENAME=//p' "${APT_OS_RELEASE:-/etc/os-release}" | tr -d '"')"
[ -n "$codename" ] || { echo "::error::apt-install: no VERSION_CODENAME in the os-release file" >&2; exit 2; }
apt_opts=(-o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=3)

# `default` keeps the image's configuration; every other candidate is a mirror base URL.
candidates=(
  default
  http://archive.ubuntu.com/ubuntu
  http://mirrors.edge.kernel.org/ubuntu
  http://mirror.math.princeton.edu/pub/ubuntu
  http://mirrors.mit.edu/ubuntu
  http://mirror.arizona.edu/ubuntu
  http://ubuntu.osuosl.org/ubuntu
  http://mirrors.cat.pdx.edu/ubuntu
)

# The mirror apt asks first: the head of the image's mirror list, else the first URL of the Ubuntu source files.
default_mirror() {
  local url=""
  if [ -f "$etc/apt-mirrors.txt" ]; then
    url="$(awk 'NF {print $1; exit}' "$etc/apt-mirrors.txt")"
  else
    url="$(cat "$etc/sources.list.d/ubuntu.sources" "$etc/sources.list" 2>/dev/null \
      | grep -E '^(URIs:[[:space:]]*|deb(-src)?[[:space:]]+(\[[^]]*\][[:space:]]+)?)https?://' \
      | grep -Eo 'https?://[^[:space:]]+' | head -n 1)"
  fi
  [ -n "$url" ] || url="http://azure.archive.ubuntu.com/ubuntu"
  printf '%s\n' "${url%/}"
}

reachable() {
  curl -fsS -o /dev/null --connect-timeout 5 --max-time 10 "$1/dists/$codename-security/InRelease" 2>/dev/null
}

# Point apt at one mirror: the image's mirror list when it has one, else the Ubuntu source files' own URLs.
use_mirror() {
  local url="$1" file
  if [ -f "$etc/apt-mirrors.txt" ]; then
    printf '%s/\n' "$url" | sudo tee "$etc/apt-mirrors.txt" > /dev/null
  fi
  for file in "$etc/sources.list.d/ubuntu.sources" "$etc/sources.list"; do
    [ -f "$file" ] || continue
    sed -E -e "s#^(URIs:[[:space:]]*)https?://[^[:space:]]+#\\1$url/#" \
           -e "s#^((deb|deb-src)[[:space:]]+(\\[[^]]*\\][[:space:]]+)?)https?://[^[:space:]]+#\\1$url/#" "$file" \
      | sudo tee "$file.new" > /dev/null && sudo mv "$file.new" "$file"
  done
}

attempt=0
for candidate in "${candidates[@]}"; do
  if [ "$candidate" = default ]; then mirror="$(default_mirror)"; else mirror="$candidate"; fi
  if ! reachable "$mirror"; then
    echo "::warning::apt-install: $mirror does not answer, skipped"
    continue
  fi
  [ "$candidate" = default ] || use_mirror "$mirror"
  attempt=$((attempt + 1))
  echo "apt-install: attempt $attempt on $mirror"
  if sudo timeout --kill-after=10s 90 apt-get "${apt_opts[@]}" update \
    && sudo timeout --kill-after=10s 150 apt-get "${apt_opts[@]}" install -y "$@"; then
    exit 0
  fi
  echo "::warning::apt-install: attempt $attempt on $mirror failed"
  [ "$attempt" -lt "$max_attempts" ] || break
done
echo "::error::apt-install: no Ubuntu mirror installed $*" >&2
exit 1
