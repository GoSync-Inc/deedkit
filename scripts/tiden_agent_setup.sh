#!/bin/sh
# Prepare the pinned Tiden CLI for a coding agent session without a local install,
# such as a cloud session. Reads the API token from TIDEN_API_TOKEN; never takes it
# as an argument and never writes it into the checkout. No hooks are installed.
set -eu

base_url=${TIDEN_BASE_URL:-https://app.tiden.ai}
# Product identifiers are not credentials; the CI reporting job uses the same ones.
workspace=${TIDEN_WORKSPACE_ID:-1dc18e09-4a09-4d74-8300-cd4302622357}
product=${TIDEN_PRODUCT_ID:-75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1}
bin_dir=${TIDEN_BIN_DIR:-$HOME/.local/bin}

if [ -z "${TIDEN_API_TOKEN:-}" ]; then
  echo 'TIDEN_API_TOKEN is not set; Tiden is unavailable in this session.' >&2
  exit 2
fi
cd "$(git rev-parse --show-toplevel)"
# The shared pin (harness tiden.md, "Tiden CLI version"), the same one CI reads.
pin=docs/harness/tiden.md
version=$(sed -n 's/^TIDEN_CLI_VERSION=\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p' "$pin" | head -n 1)
checksum=$(sed -n 's/^TIDEN_CLI_SHA256_linux_amd64=\([0-9a-f]\{64\}\)$/\1/p' "$pin" | head -n 1)
[ -n "$version" ] && [ -n "$checksum" ] || { echo "no valid Tiden pin in $pin" >&2; exit 1; }

if ! "$bin_dir/tiden" version 2>/dev/null | grep -q "$version"; then
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  curl --fail --silent --show-error --location -o "$work/tiden.tar.gz" \
    "https://github.com/qase-tms/homebrew-tap/releases/download/v$version/tiden_linux_amd64.tar.gz"
  echo "$checksum  $work/tiden.tar.gz" | sha256sum --check --quiet
  tar -xzf "$work/tiden.tar.gz" -C "$work" tiden
  mkdir -p "$bin_dir"
  install -m 0755 "$work/tiden" "$bin_dir/tiden"
fi
tiden() { TIDEN_NO_AUTO_UPDATE=1 "$bin_dir/tiden" "$@"; }

tiden config set autoUpdate off >/dev/null
tiden config set baseUrl "$base_url" >/dev/null
printf '%s' "$TIDEN_API_TOKEN" | tiden config set apiToken - >/dev/null
tiden workspace use "$workspace" >/dev/null
tiden product bind --product-id "$product" --workspace-id "$workspace" >/dev/null
tiden config verify --format text
echo "Tiden CLI $version ready: $bin_dir/tiden"
