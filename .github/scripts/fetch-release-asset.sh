#!/usr/bin/env bash
# Fetch one pinned GitHub release asset and accept it only when its SHA-256 equals the pin.
#
# Usage: fetch-release-asset.sh <https://github.com/OWNER/REPO/releases/download/TAG/ASSET> <sha256> <dest>
#
# Sources, in order; each one must match the pin, else the next is tried and nothing is kept:
#   1. <dest> itself when it already exists (restored by actions/cache): no download at all;
#   2. the release download URL, bounded retries with exponential backoff;
#   3. the same asset through the GitHub REST API (api.github.com assets endpoint, served by
#      release-assets.githubusercontent.com): the same bytes, so the same checksum, through
#      another front end than github.com/.../releases/download (outage 2026-11-08: HTTP 504).
# GH_TOKEN, when set, goes only to api.github.com: curl drops it on the cross-host redirect.
set -euo pipefail

url=$1 sum=$2 dest=$3
pattern='^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/download/([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)$'
if [[ ! $url =~ $pattern ]]; then echo "not a GitHub release download URL: $url" >&2; exit 2; fi
repo="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}" tag=${BASH_REMATCH[3]} asset=${BASH_REMATCH[4]}
if [[ ! $sum =~ ^[0-9a-f]{64}$ ]]; then echo "not a SHA-256: $sum" >&2; exit 2; fi

part="$dest.part"
matches() { echo "$sum  $1" | sha256sum --check --strict --status; }
# Every attempt is bounded: 5 tries per source (curl backs off 1, 2, 4, 8 s and retries 5xx and timeouts).
get() { curl --fail --silent --show-error --location --connect-timeout 20 --max-time 120 --retry 4 "$@"; }
keep() {
  if matches "$part"; then mv -- "$part" "$dest"; echo "$asset: $1"; exit 0; fi
  echo "$asset from $1 does not match the pinned SHA-256; discarded" >&2
  rm -- "$part"
}

if [ -f "$dest" ]; then
  if matches "$dest"; then echo "$asset: cache"; exit 0; fi
  echo "cached $asset does not match the pinned SHA-256; discarded" >&2
  rm -- "$dest"
fi

if get -o "$part" "$url"; then keep "release download"; else echo "release download of $asset failed" >&2; fi
rm -f -- "$part"

auth=()
if [ -n "${GH_TOKEN:-}" ]; then auth=(-H "Authorization: Bearer $GH_TOKEN"); fi
api="https://api.github.com/repos/$repo"
if id=$(get "${auth[@]}" -H 'Accept: application/vnd.github+json' "$api/releases/tags/$tag" \
    | jq -r --arg name "$asset" '[.assets[] | select(.name == $name) | .id] | if length == 1 then .[0] else empty end') \
    && [[ $id =~ ^[0-9]+$ ]]; then
  if get "${auth[@]}" -H 'Accept: application/octet-stream' -o "$part" "$api/releases/assets/$id"; then
    keep "GitHub API asset"
  else
    echo "GitHub API download of $asset failed" >&2
  fi
else
  echo "GitHub API lookup of $asset in $repo $tag failed" >&2
fi
rm -f -- "$part"
echo "no source returned $asset with the pinned SHA-256" >&2
exit 1
