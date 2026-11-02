# Inference service on the Mac mini (agent-run, ANYM-684; owner 11-01 «2 b на мак мини ... инфру сделал чтоб могли все пользоваться»)

`deedkit-inference` (`services/inference`, ANYM-682) runs on the mini as the hidden user `_deedkitinf`, behind
Tailscale Serve, reachable from production only. This is the install runbook, in the form of
`remote-test-mini-setup.md`: every step has a check and an undo, and a ledger records each resource *before* the
command that creates it, so the undo removes exactly what this setup made. An agent runs it over ssh with `sudo -n`
(owner D44) after the card's Astra r1 (keys and access), **from a checkout of a green `main` SHA that contains
ANYM-682**, one block at a time. Stop at the first failing block, run the undo of that block, report.

Hard rules: Between comes first (owner D42). Never touch Between, colima, or a running remote test; run the busy
check below before each live block. No model is started during the install, and the service never makes an outbound
connection (the sandbox, section 5). No send, call, ACK, pairing or provider session is involved.

`$MINI` is read as in `remote-test-mini-setup.md` (first line of `~/.config/deedkit/mini-host`, `<admin>@<address>`);
`$ADMIN` is its account part. The ledger is `~/deedkit-inference-setup.created` in the admin account's home. The
repository names neither the mini nor its tailnet.

Busy check (read-only; go on only when it prints `idle`). It runs the probe's own observation, the one the
service trusts: every Docker context of the admin account, the test runs' slot processes, and **busy when anything
cannot be observed**. `idle` needs an explicit successful result (exit 0), never the absence of an error:

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; uptime; python3 -I - --once' < services/inference/src/deedkit_inference/busy_probe.py && echo idle || echo "busy or not observable: wait"
```

## 0. Owner-hands steps, asked once (card criterion 1)

Collected before any change and sent to the orchestrator as ONE message. Items are known only after section 1;
the candidates are:

1. The tailnet admin console login or an API access token with the `policy_file` scope (section 9). Without it the
   policy step waits; the rest of the install does not.
2. Turning on HTTPS certificates (and MagicDNS) for the tailnet, only if section 1 shows them off (`CertDomains` empty or
   `MagicDNSEnabled` false; both were on at 11-02).
3. Not part of the install: the reboot drill (section 13) waits for a window the owner picks later, because a reboot of
   the mini stops Between's VMs. The install never reboots the mini and never stops Between's VMs.

Passwords and MFA are never typed by an agent. sudo needs none on the mini.

## 1. Inventory (read-only, before any change)

Save the output; it goes on the card (Evidence). Stop if it differs from what a later block assumes. The mini has TWO
Tailscale instances: the app's network extension (what the default `tailscale` CLI talks to) and a standalone root
`tailscaled` with its own socket `/var/run/tailscaled.socket`. The research of 11-01 read only the default CLI and had
the roles reversed: on 11-02 the standalone daemon was the one logged in (Running, node key, the tailnet address) and the
app's extension was `NeedsLogin`. Read BOTH, by socket, and let the output say which one holds the identity.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'T=/Applications/Tailscale.app/Contents/MacOS/Tailscale; sw_vers -productVersion; hostname; uptime
app() { $T "$@"; }; std() { $T --socket=/var/run/tailscaled.socket "$@"; }
printf "\n== instance A: the app extension (default socket) ==\n"; app version | head -1; app status 2>&1 | head -4
printf "\n== instance B: the standalone tailscaled (its own socket) ==\n"
std status --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in (\"Version\",\"BackendState\",\"MagicDNSSuffix\",\"CertDomains\",\"HaveNodeKey\")}); s=d[\"Self\"]; print(s[\"DNSName\"], s[\"TailscaleIPs\"], s.get(\"Tags\")); print(d.get(\"CurrentTailnet\")); [print(\"peer\", p[\"HostName\"], p[\"TailscaleIPs\"], p.get(\"Online\"), p.get(\"Tags\")) for p in d.get(\"Peer\", {}).values()]"
std serve status 2>&1 | tail -n +2; std funnel status 2>&1 | tail -n +2
printf "\n== processes, jobs, system extension ==\n"; ps -axo pid,user,args | grep -i "[t]ailscale" | cut -c1-160; launchctl list | grep -i tailscale
systemextensionsctl list 2>&1 | grep -i tailscale; ifconfig | grep -E "^utun|inet 100\."; sudo -n ls /Library/Tailscale/certs
ls -l /Library/LaunchDaemons /Library/LaunchAgents ~/Library/LaunchAgents 2>/dev/null | grep -i tailscale
ls -l /var/db/tailscale /Library/Tailscale 2>/dev/null
printf "\n== users, ids, ports, disk ==\n"; dscl . -list /Users | grep -i "^_deedkit"; lsof -nP -iTCP:8090 -sTCP:LISTEN
df -h /Users | tail -1; vm_stat | head -4; command -v uv python3.13; ls /opt/homebrew/bin/python3.13 /opt/homebrew/bin/uv'
```

Whichever instance reports `Running` with a node key (and holds the tailnet address on a `utun`) holds the identity;
the other one is left alone. Nothing is retired: stopping the instance that holds the identity cuts the mini off the
tailnet (and the owner's SSH with it). Write the answer in the inventory.

Tailnet policy (ACL or grants, groups, tags, allow-all or not, and how an agent can change it): read through the
API when a token exists (section 9, step 1) or in the console:

```bash
curl -sS -H "Authorization: Bearer $TAILSCALE_API_KEY" -H "Accept: application/hujson" "https://api.tailscale.com/api/v2/tailnet/-/acl" -o policy.before.hujson -D policy.before.headers
```

Record: `ETag` (the hash), whether a rule `{"src":["*"],"dst":["*:*"]}` or an `autogroup:member` allow-all exists,
which rules reach the mini and production, the groups and tags, and whether the tailnet already has `tests`.

## 2. Preflight and ledger

Read-only until the last line. Every resource must be absent and the ledger must not exist. The id is chosen
here: the first number from 400 up that is free as UID and as GID, and it is written to the ledger first. Go on
only after `preflight-ok`:

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'U=_deedkitinf; fail=0
[ ! -e ~/deedkit-inference-setup.created ] || { echo "ledger exists: undo or inspect first"; fail=1; }
dscl . -read /Users/$U >/dev/null 2>&1 && { echo "user $U exists"; fail=1; }
dscl . -read /Groups/$U >/dev/null 2>&1 && { echo "group $U exists"; fail=1; }
for p in /Users/$U /Users/Shared/deedkit-inference /usr/local/libexec/deedkit-busy-probe /usr/local/libexec/deedkit-inference-keygen /usr/local/etc/deedkit-inference \
  /etc/newsyslog.d/deedkit-inference.conf /Library/LaunchDaemons/com.deedkit.inference.plist \
  /Library/LaunchDaemons/com.deedkit.inference-busy.plist; do [ ! -e $p ] || { echo "$p exists"; fail=1; }; done
launchctl print system/com.deedkit.inference >/dev/null 2>&1 && { echo "job com.deedkit.inference loaded"; fail=1; }
launchctl print system/com.deedkit.inference-busy >/dev/null 2>&1 && { echo "job com.deedkit.inference-busy loaded"; fail=1; }
ID=""; for n in $(seq 400 499); do
  if [ -z "$(dscl . -list /Users UniqueID | awk -v id=$n "\$2==id")" ] && [ -z "$(dscl . -list /Groups PrimaryGroupID | awk -v id=$n "\$2==id")" ]; then ID=$n; break; fi; done
[ -n "$ID" ] || { echo "no free id"; fail=1; }
[ $fail = 0 ] && echo "id $ID" > ~/deedkit-inference-setup.created && echo "preflight-ok (id $ID)"'
```

Undo of the whole setup is section 14; the ledger lines are `id N`, `group`, `user`, `home`, `shared`, `stage <dir>`,
`probe-bin`, `keygen-bin`, `profile`, `newsyslog`, `plist-busy`, `plist-service`, `job-busy`, `job-service`,
`serve`, `duty <pid>|<start time>`.

## 3. Group and user `_deedkitinf`

Hidden, not an admin, no login shell, no password login, own primary group (not `staff`).

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'set -e; U=_deedkitinf; ID=$(sed -n "s/^id //p" ~/deedkit-inference-setup.created)
case "$ID" in [0-9][0-9][0-9]) ;; *) echo "no id in ledger"; exit 1 ;; esac
if dscl . -read /Groups/$U >/dev/null 2>&1; then echo "group exists"; exit 1; fi
echo group >> ~/deedkit-inference-setup.created
sudo -n dscl . -create /Groups/$U; sudo -n dscl . -create /Groups/$U PrimaryGroupID $ID
sudo -n dscl . -create /Groups/$U RealName "DeedKit inference"; sudo -n dscl . -create /Groups/$U Password "*"
if dscl . -read /Users/$U >/dev/null 2>&1; then echo "user exists"; exit 1; fi
echo user >> ~/deedkit-inference-setup.created
sudo -n dscl . -create /Users/$U; sudo -n dscl . -create /Users/$U RealName "DeedKit inference"
sudo -n dscl . -create /Users/$U UniqueID $ID; sudo -n dscl . -create /Users/$U PrimaryGroupID $ID
sudo -n dscl . -create /Users/$U UserShell /usr/bin/false; sudo -n dscl . -create /Users/$U NFSHomeDirectory /Users/$U
sudo -n dscl . -create /Users/$U Password "*"; sudo -n dscl . -create /Users/$U IsHidden 1'
```

Read-back: `id _deedkitinf` shows `uid=N(_deedkitinf) gid=N(_deedkitinf)` and no other group; `dscl . -read
/Users/_deedkitinf UserShell IsHidden Password NFSHomeDirectory` shows `/usr/bin/false`, `1`, `*`;
`dseditgroup -o checkmember -m _deedkitinf admin` and `... staff` both say "no". Undo: section 14.

## 4. Layout

`/Users/_deedkitinf` (700) holds `releases/`, `current` (symlink, later), `work/` (700), `keys/` (700), `state/` (700,
the usage counters), `policy.json` (0600, written by keygen), `config.json`, `logs/`. The sandbox makes the whole home
read-only to the service except `work/`, `logs/`, `state/` and `.cache/`. `/Users/Shared/deedkit-inference` (the admin account,
group `_deedkitinf`, mode 750: nobody else can enter) holds `busy.json` (the probe writes it, group-readable) and the
read-only model store `models/` (owned by the admin account: the service user can read, never write).

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'set -e; U=_deedkitinf; A=$(id -un)
if [ -e /Users/$U ]; then echo "home exists"; exit 1; fi
echo home >> ~/deedkit-inference-setup.created
sudo -n mkdir -m 700 /Users/$U
for d in releases work keys logs state; do sudo -n mkdir -m 700 /Users/$U/$d; done
sudo -n chown -R ${U}:${U} /Users/$U; sudo -n chmod 700 /Users/$U
if [ -e /Users/Shared/deedkit-inference ]; then echo "shared dir exists"; exit 1; fi
echo shared >> ~/deedkit-inference-setup.created
sudo -n mkdir -m 750 /Users/Shared/deedkit-inference
sudo -n chown ${A}:${U} /Users/Shared/deedkit-inference
sudo -n mkdir -m 750 /Users/Shared/deedkit-inference/models; sudo -n chown ${A}:${U} /Users/Shared/deedkit-inference/models'
```

Read-back: `ls -ld /Users/_deedkitinf /Users/_deedkitinf/{releases,work,keys,logs,state} /Users/Shared/deedkit-inference
/Users/Shared/deedkit-inference/models` — `drwx------ _deedkitinf _deedkitinf` for the first six,
`drwxr-x--- <admin> _deedkitinf` for the shared ones; as the admin account `ls /Users/_deedkitinf` is denied; as
`_deedkitinf` (`sudo -n -u _deedkitinf ls ...`) it can read `models/` and not write there.

## 5. Files in place (probe, profile, rotation, jobs) and the sandbox proof

Render the files on the checkout (they hold no secret), copy them through a private stage directory (`mktemp -d`,
recorded as `stage <dir>`), install them root-owned. The busy probe and keygen are root-owned and live outside every
directory the service can write, because they run **outside the sandbox** (the probe as the admin account, keygen as the
service user with the key directory): code the sandboxed service could rewrite would carry its payload past the
sandbox. For the same reason the profile makes `releases/`, `keys/`, `current`, `previous`, `config.json` and
`policy.json` read-only to the service and the keys unreadable to it.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ADMIN="${MINI%@*}"; OUT="$(mktemp -d)"
python3 services/inference/launchd/render.py --admin "$ADMIN" --admin-home "/Users/$ADMIN" --python /opt/homebrew/bin/python3.13 --out "$OUT" &&
D="$(ssh "$MINI" 'D=$(mktemp -d /tmp/deedkit-setup.XXXXXX) && echo "stage $D" >> ~/deedkit-inference-setup.created && echo $D')" &&
[ -n "$D" ] &&
scp "$OUT"/* services/inference/src/deedkit_inference/busy_probe.py services/inference/src/deedkit_inference/keygen.py services/inference/launchd/sandbox_proof.py "$MINI:$D/" &&
ssh "$MINI" "D=$D"'; set -e; cd "$D"; chmod 755 "$D"; chmod 644 *   # no secret in it; the proof runs as the service user from here
for p in /usr/local/libexec/deedkit-busy-probe /usr/local/libexec/deedkit-inference-keygen /usr/local/etc/deedkit-inference \
  /etc/newsyslog.d/deedkit-inference.conf /Library/LaunchDaemons/com.deedkit.inference.plist /Library/LaunchDaemons/com.deedkit.inference-busy.plist; do
  if [ -e $p ]; then echo "$p exists"; exit 1; fi; done
sudo -n mkdir -p /usr/local/libexec
echo probe-bin >> ~/deedkit-inference-setup.created
sudo -n install -o root -g wheel -m 0755 busy_probe.py /usr/local/libexec/deedkit-busy-probe
echo keygen-bin >> ~/deedkit-inference-setup.created
sudo -n install -o root -g wheel -m 0755 keygen.py /usr/local/libexec/deedkit-inference-keygen
echo profile >> ~/deedkit-inference-setup.created
sudo -n install -d -o root -g wheel -m 0755 /usr/local/etc/deedkit-inference
sudo -n install -o root -g wheel -m 0644 deedkit-inference.sb /usr/local/etc/deedkit-inference/deedkit-inference.sb
echo newsyslog >> ~/deedkit-inference-setup.created
sudo -n install -o root -g wheel -m 0644 deedkit-inference.newsyslog /etc/newsyslog.d/deedkit-inference.conf
echo plist-busy >> ~/deedkit-inference-setup.created
sudo -n install -o root -g wheel -m 0644 com.deedkit.inference-busy.plist /Library/LaunchDaemons/
echo plist-service >> ~/deedkit-inference-setup.created
sudo -n install -o root -g wheel -m 0644 com.deedkit.inference.plist /Library/LaunchDaemons/
sudo -n install -o _deedkitinf -g _deedkitinf -m 0640 config.json /Users/_deedkitinf/config.json
plutil -lint /Library/LaunchDaemons/com.deedkit.inference.plist /Library/LaunchDaemons/com.deedkit.inference-busy.plist
sudo -n newsyslog -nv 2>&1 | grep -i deedkit || true'; rm -r "$OUT"
```

Read-back: `plutil -lint` says `OK` for both; `ls -l` shows root:wheel and the modes above; the plist prints with
`plutil -p` (UserName `_deedkitinf`, `Nice` 10, `KeepAlive` `{SuccessfulExit = false}`, `ExitTimeOut` 960).

### Sandbox proof (card criterion 2, `[open: measured]`)

Nothing of the service runs yet. A listener outside the sandbox gives the "other loopback port"; the proof script
runs inside the profile as the service user, listens on 8090 and answers one request. The stage directory holds the script.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'D=$(sed -n "s/^stage //p" ~/deedkit-inference-setup.created | tail -1); case $D in /tmp/deedkit-setup.*) ;; *) echo no stage; exit 1 ;; esac
if lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null; then echo "8090 in use"; exit 1; fi
python3 -I -m http.server 18091 --bind 127.0.0.1 >/dev/null 2>&1 & OTHER=$!
sleep 1
sudo -n -u _deedkitinf /usr/bin/sandbox-exec -f /usr/local/etc/deedkit-inference/deedkit-inference.sb \
  /usr/bin/python3 -I $D/sandbox_proof.py --other-port 18091 --wait 20 > $D/proof.out 2>&1 & PROOF=$!
sleep 3; echo "request from outside the sandbox:"; curl -sS --max-time 5 http://127.0.0.1:8090/; echo
wait $PROOF; cat $D/proof.out; kill $OTHER'
```

Expected: the curl prints `ok` and the proof prints one JSON line in which `accepted` is `true` and **every other
value is `denied`** (except `write_work`): `tcp_other_loopback`, `udp_other_loopback`, `tcp_public`, `udp_public`, `bind_other_port`,
`bind_all_interfaces`, `child_tcp_public` (a child process inherits the profile), `write_releases`, `write_keys`,
`write_home_root` (anything else in the service home, such as a `.config/uv/uv.toml`), and `write_work` **`ALLOWED`**
(the one place it must write; they need `releases/`, `keys/` and `work/` to exist: section 4). Anything `ALLOWED` or `error:...` in place of `denied`, or
`accepted: false`: the profile does not do its job, or breaks accepting. Then use the **fallback**, chosen by this test
and recorded on the card: a `pf` rule that drops every outbound packet of the service user, loaded as its own anchor
(never by editing the system rules): `block drop out quick all user _deedkitinf` after `pass out quick on lo0 proto tcp
from any port 8090 user _deedkitinf keep state`, in `/etc/pf.anchors/deedkit-inference`, referenced from a new `anchor`
line in `/etc/pf.conf` (copy of the original kept in the ledger as `pf-conf-backup`, undo: restore it and `pfctl -f
/etc/pf.conf`). A host firewall change gets its own Astra look before it is applied; do not start the service without
one of the two (the file rules of the profile stay either way). `/usr/bin/python3` is the system stub: if it does not run here, use the python of section 6 for the proof.

## 6. First release, config, keys (the service is not started yet)

`inference_deploy.py --stage-only` verifies the SHA (on `main`, `CI gate` and `checks / verify` green), unpacks
`services/inference` of it into `releases/<sha>/`, runs `uv sync --locked --group engines` there and points `current`.

```bash
SHA="$(git rev-parse HEAD)"; git merge-base --is-ancestor "$SHA" origin/main && python3 scripts/inference_deploy.py --stage-only "$SHA"
```

Config: `/Users/_deedkitinf/config.json` (placed in section 5) names `bind` 127.0.0.1, port 8090, the policy, the busy
file, `work_dir`, `drain_seconds` 900 and an **empty** `aliases` table. Since ANYM-683 an empty table is valid: the service
then serves the two aliases the package ships (`audio-default`, `vision-default`, pinned in `models.json`). Before
ANYM-683 (service at `d9f547f6`) an empty table was refused (`config.py` `min_length=1`), so the install stopped at
the staged release and the unloaded jobs; from `094a8581` on the config needs no change.

Keys: the service refuses to start without a valid policy, so the first principals are issued before the first start,
as the service user, from the release just unpacked (keygen is stdlib only). The enrichment key's numbers are above
the worker's own cap (ANYM-686); `deploy-probe` is the principal the deploy script reads `/v1/models` with.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'set -e
k() { sudo -n -H -u _deedkitinf /opt/homebrew/bin/python3.13 -I /usr/local/libexec/deedkit-inference-keygen --policy /Users/_deedkitinf/policy.json "$@"; }
k --principal deploy-probe --scopes audio --max-jobs-per-day 1000 --max-concurrent 1 --expires-days 365 --key-file /Users/_deedkitinf/probe.key
k --principal messaging-enrichment --scopes audio,image,document --max-jobs-per-day 2000 --max-concurrent 2 --expires-days 365 --key-file /Users/_deedkitinf/keys/messaging-enrichment.key
sudo -n ls -l /Users/_deedkitinf/policy.json /Users/_deedkitinf/probe.key /Users/_deedkitinf/keys'
```

The output names the files and the expiry, never the key. Read-back: `policy.json` and both key files are `-rw-------`
owned by `_deedkitinf`; `grep -c dki_ policy.json` is `0` (hashes only); `revision` is 2. Record the first key's
**expiry date** and its **rotation due date, 30 days before it**, in the card's History and in a calendar entry of
the operator. Undo: delete the three files (they are inside the user's home, removed with the home in section 14).
The `deedkit-inference keygen` subcommand named in the card is `/usr/local/libexec/deedkit-inference-keygen` here (stdlib, root-owned); ANYM-682's CLI may dispatch to it.

### 6b. Models, tools, egress probe and canary (ANYM-683; as run on 11-02)

Every block below starts after the busy check (`ssh "$MINI" '/opt/homebrew/bin/python3.13
/usr/local/libexec/deedkit-busy-probe --once'`, exit 0 only when idle) and is chained to it with `&&`: a `;` after a busy
verdict still runs the block (that slip happened once on 11-02, on a Serve write that did no harm). Order: tools,
pull, verify, egress probe, canary.

Tools: the engines call `ffmpeg` and `ffprobe` by absolute path (`/opt/homebrew/bin`; the service `PATH` is
`/usr/bin:/bin`). The sandbox profile needs no change for them or for the model store: `(allow default)` leaves reads
open and denies only the keys and `probe.key`.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'cd /; for t in ffprobe ffmpeg; do sudo -n -u _deedkitinf /opt/homebrew/bin/$t -version | head -1; done; df -g /Users | tail -1'
```

Pull (the only command that downloads; about 5.3 GB from `huggingface.co` and its CDN hosts, disk guard 15 GB free).
It runs as the **admin account**, which owns the store: the release venv lives in the service home, which the admin
cannot enter, so the admin runs the same release from its own stage copy (base dependencies only, no `engines` group).
The stage directory is a ledger `stage` entry, removed by section 14:

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
SHA="$(git rev-parse HEAD)"; git merge-base --is-ancestor "$SHA" origin/main &&
git archive -o "$TMPDIR/inference-$SHA.tar" "$SHA" services/inference &&
D="$(ssh "$MINI" 'D=$(mktemp -d /tmp/deedkit-setup.XXXXXX) && echo "stage $D" >> ~/deedkit-inference-setup.created && echo $D')" &&
case "$D" in /tmp/deedkit-setup.*) ;; *) echo "bad stage"; exit 1 ;; esac &&
scp -q "$TMPDIR/inference-$SHA.tar" "$MINI:$D/pkg.tar" &&
ssh "$MINI" "cd $D && tar -x -f pkg.tar && cd services/inference && /opt/homebrew/bin/uv sync --locked --no-dev --python /opt/homebrew/bin/python3.13 && (nohup nice -n 10 .venv/bin/deedkit-inference pull --model-dir /Users/Shared/deedkit-inference/models > $D/pull.log 2>&1 < /dev/null & echo \$! > $D/pull.pid)"
```

Wait until the pull process is gone (`kill -0 $(cat $D/pull.pid)`), then `tail $D/pull.log`: four `verified <alias>/<phase>`
lines (11-02: 5.3 GB on disk). An interrupted pull leaves nothing served. Verify, as the service user, which
also shows that it can read the store and not write it (11-02: `ok` for all four in 3.7 s):

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'cd /; sudo -n -u _deedkitinf /Users/_deedkitinf/current/.venv/bin/deedkit-inference verify; sudo -n -u _deedkitinf touch /Users/Shared/deedkit-inference/models/x 2>&1 | head -1'
```

Egress probe, **inside the sandbox profile** as the service user (it is the engine child's own view): all four
attempts must read `refused:PermissionError` and the exit status must be 0. The same command outside the profile is the
control: it must show `OPEN` for the public targets, or the probe proves nothing (11-02: inside 4 of 4 refused, outside 3 of 4 `OPEN`).
Any outbound connection inside the profile: stop and report BLOCKED.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'cd /; P=/Users/_deedkitinf/current/.venv/bin/python
sudo -n -u _deedkitinf /usr/bin/sandbox-exec -f /usr/local/etc/deedkit-inference/deedkit-inference.sb $P -I -m deedkit_inference.egress_probe; echo "inside exit $?"
sudo -n -u _deedkitinf $P -I -m deedkit_inference.egress_probe; echo "control exit $?"'
```

Canary (a client of the running service, so it runs **outside** the sandbox, which hides the key; synthetic data only).
Generate the set as the admin account (it needs Pillow, macOS `say`, ffmpeg and the system fonts) into the shared
directory (`generate.py` encodes with `libvorbis`; the mini's ffmpeg 8.1 has `libopus` and no `libvorbis`, so on a
release that still says `libvorbis` run it from the stage copy with that word replaced by `libopus`: Ogg/Opus is also
the format of Telegram voice notes):

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
D="$(ssh "$MINI" 'sed -n "s/^stage //p" ~/deedkit-inference-setup.created | tail -1')"; case "$D" in /tmp/deedkit-setup.*) ;; *) echo "no stage"; exit 1 ;; esac
ssh "$MINI" "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin; cd $D/services/inference && uv run --no-project --python /opt/homebrew/bin/python3.13 --with pillow python tests/canary/generate.py /Users/Shared/deedkit-inference/canary-set"
```

Then run it in a **quiet window**: `busy.json` false for a minute, load below 5 and free plus inactive plus speculative memory
(the figure admission uses: `vm_stat` free + inactive + speculative, as `scripts/remote-test.sh` counts it) at least 8.3 GB. A canary that meets `busy_deedkit_ci` or
`memory_low` fails at once with HTTP 503 (that is the design, not a defect of the run), so wait and rerun:

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'cd /; sudo -n -H -u _deedkitinf /Users/_deedkitinf/current/.venv/bin/deedkit-inference canary --key-file /Users/_deedkitinf/keys/messaging-enrichment.key --set /Users/Shared/deedkit-inference/canary-set'
```

Record from the output: character error per image, word error per note, real-time factor, the long recording's segment
bounds and the JSON shape; and, from `ps` and `vm_stat` while it runs, the service user's resident size and the free
plus inactive memory (before the speculative pages were counted, 11-02: the idle mini read 7.2 to 7.5 GB of free + inactive
with 2.0 GiB more speculative, under the 7.5 GB a cold vision start needs). On 11-02 the vision worker held about 3.8 GB resident and free plus inactive memory read 8.9 to 9.1 GB
at rest but fell to 7.6 to 7.7 GB when a request ended, under the 7.9 GB a vision phase needs (4.9 + `min_free_gb` 3.0).

## 7. Start the jobs (busy probe first)

Without a fresh `busy.json` the service closes admission (fail closed), so the probe starts first.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'set -e
echo job-busy >> ~/deedkit-inference-setup.created
sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.deedkit.inference-busy.plist
sleep 12; ls -l /Users/Shared/deedkit-inference; cat /Users/Shared/deedkit-inference/busy.json; echo
echo job-service >> ~/deedkit-inference-setup.created
sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.deedkit.inference.plist
sleep 5; sudo -n launchctl print system/com.deedkit.inference | grep -E "state|pid|program|last exit"
curl -sS --max-time 5 http://127.0.0.1:8090/health; echo'
```

Expected: `busy.json` is `-rw-r----- <admin> _deedkitinf`, `ts` within a few seconds of now, `between`/`deedkit_ci`
true only if Between or a test run is active (`between` also reads the CPU of the `_betweenci` colima VMs' Virtualization
processes: Between's CI runs inside them, where no Docker context of the admin account sees it; busy while their summed
%cpu averages 10 % over 60 s or spikes to 50 %, and for 120 s after the last such reading or after the probe's own start); the service is `running` under `/usr/bin/sandbox-exec`, `ps -axo
user,nice,args` shows `_deedkitinf` with NI 10; `/health` answers `{"status": "ok", "queue": ..., "admission":
"open"|"closed"}` (closed while Between is busy: that is the design). If the `logs/` files were created by launchd as
root, `chown _deedkitinf` them and record it. Then the deploy script's own read-back, which also proves
`/v1/models` with the probe key and that `release` equals the SHA: `python3 scripts/inference_deploy.py "$SHA"`
(it drains, which is instant on an idle service, and restarts). Undo: `sudo -n launchctl bootout
system/com.deedkit.inference` (then `-busy`) and remove the plists, section 14.

Rotation of logs: `newsyslog` rotates at 10 MB; launchd keeps its descriptor, so lines go to the rotated file until
the next deploy restarts the service (the logs hold metadata only; a day of 2000 jobs is well under 1 MB).

## 8. Deploy and rollback (afterwards)

`python3 scripts/inference_deploy.py <sha>` from a checkout that has `<sha>` (it refuses a SHA that is not on main
or not green), `--rollback` repoints `current` to the previous release with the same drain, `--force` kills only
after the 960 s limit. It stops the service with SIGTERM (admission closes, every queued and running job finishes,
exit 0; launchd does not restart a clean exit; after the service's own 900 s limit it exits 3 and launchd restarts the
old release, which is stopped again), then **unloads the job** (`launchctl bootout --wait`: SIGTERM first, so a restart that took
work meanwhile drains too, and no restart is possible before the switch; the script then waits until the job is gone
and no process is left, up to 1100 s), switches `current`, loads the job again
(`launchctl bootstrap`, then `kickstart` without `-k`), and reads
back `/health` and `/v1/models` (`release` must equal the SHA). It prints the admission state and does not require
it open. A deploy never runs while Between is the reason a job runs long: it waits, it does not kill. One deploy
at a time (a flock held on the mini). It touches `releases/`, `current` and `previous` only. `uv sync` runs unsandboxed as the service user, so it runs
with an empty environment and `UV_NO_CONFIG=1`: no uv configuration file of that user's home (which the sandbox also
makes read-only to the service) and no proxy setting can reach it.

## 9. Tailscale: one node, one Serve (criterion 4)

Precondition from section 1: which instance holds the identity (the `Running` one with a node key; **on 11-02 the
standalone root `tailscaled`, node `egors-mac-mini-1`**, MagicDNS on, `CertDomains` non-empty, no serve config, nothing on
443) and that HTTPS certificates are on for the tailnet (else the owner-hands toggle of section 0, asked once).

Criterion 4 of the card said to retire "the standalone logged-out `tailscaled`". The inventory of 11-02 showed the roles
the other way round (the research had read the default CLI, which talks to the app's extension), so **nothing is
retired**: both instances are left alone and Serve goes on the one that holds the identity, through its socket
(evidence-based change to the criterion, recorded in the card History). The CLI on the mini is older than the daemon (a
version warning on stderr); read the configuration back after setting it.

Serve (never Funnel):

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'set -e; ts() { /usr/local/bin/tailscale --socket=/var/run/tailscaled.socket "$@"; }
ts serve status 2>&1 | tail -n +2; ts funnel status 2>&1 | tail -n +2
echo serve >> ~/deedkit-inference-setup.created
ts serve --bg --https=443 http://127.0.0.1:8090
ts serve status 2>&1 | tail -n +2; ts funnel status 2>&1 | tail -n +2
N=$(ts status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)[\"Self\"][\"DNSName\"].rstrip(\".\"))"); echo "$N"
echo "read it back from a tailnet client, not from here"'
```

Expected: `serve status` lists `https://<mini>.<tailnet>.ts.net (tailnet only)` proxying `http://127.0.0.1:8090`, Funnel
shows nothing. The mini cannot reach its own tailnet address (its `utun` loops back: a request from the mini to the
`.ts.net` name times out, and its system resolver does not know the name), so read the Serve back from another tailnet
node, here the operator's laptop: `curl -sS -w "\nhttp %{http_code} verify %{ssl_verify_result}\n"
https://<mini>.<tailnet>.ts.net/health` returns the health JSON with `http 200 verify 0` (a valid certificate; 11-02).
Then from production's
host (read-only ssh, a plain GET, no key): `curl -sS --max-time 10 https://<mini>.<tailnet>.ts.net/health` returns the
same JSON, but only after section 10 lets production reach the mini (before it, a timeout is the expected answer
under a restricting policy). Undo: `ts serve reset` (same socket) and check `ts serve status` says "No serve config".

## 10. Who can reach it, without changing any identity (criterion 5)

No node is tagged: tagging replaces a node's user identity and can remove access granted by user or group (owner
SSH, the backup server, CI). The policy gets two **host aliases** (the mini's and production's tailnet addresses) and
**one rule**: production's alias to the mini's alias on 443. The change is an addition.

1. Read and save the current policy and its `ETag` (section 1). Build the before-matrix from it: for each of owner
   devices → mini:22, production ↔ backup server, laptop → mini, CI → its targets, the verdict the *effective* policy
   gives (the API's `acl/preview` or the console's Access Controls preview).
2. If the policy is **allow-all**: the new rule restricts nothing and narrowing it is out of scope. Record "tailnet
   policy does not restrict the mini" on the card; the per-service key, the loopback bind and HTTPS stay the access
   control and no flow is weakened. Stop here; the rule may still be added for the record.
3. Otherwise add, keeping everything else byte for byte:

   ```
   "hosts": { "deedkit-prod": "<production tailnet IP>", "deedkit-mini": "<mini tailnet IP>" },
   "acls":  [ ... existing rules ..., { "action": "accept", "src": ["deedkit-prod"], "dst": ["deedkit-mini:443"] } ],
   "tests": [ ... existing tests ...,
     { "src": "deedkit-prod", "accept": ["deedkit-mini:443"] },
     { "src": "deedkit-prod", "deny":   ["deedkit-mini:22"] },
     { "src": "<another device>", "deny": ["deedkit-mini:443"] },
     <one accept test per flow of the before-matrix> ]
   ```

   A `deny` test for (b) and (c) is only valid when the before-matrix says those flows were already denied; when
   another existing rule accepts one, assert "not accepted by this rule" by the matrix difference instead: after
   minus before is exactly {production → mini:443}.
4. Validate first (`POST /api/v2/tailnet/-/acl/validate` runs the `tests`), then apply with `If-Match: <saved
   ETag>` so a concurrent edit is refused. Re-read the policy; save `policy.after.hujson` and its hash; record the
   after-matrix next to the before-matrix. Every before flow must still be accepted by the effective policy.

Undo: apply the saved `policy.before.hujson` with `If-Match` of the new ETag; the validate run shows the before-matrix
again. If an agent has no API token, the owner-hands step of section 0 provides one or does the paste.

## 11. Deliver the first key to production; the probe from production (criteria 6 and 8)

Production gets the key as a file in a directory the worker mounts (ANYM-688), so an atomic replace is seen by the
running container. The key travels once through the two ssh sessions inside the tailnet and is never printed, logged,
written to a ticket, a PR or Tiden; do not run these lines with `-v`. `$PROD` is production's ssh destination.

`$PRIVATE` is the absolute path of the deployment's `private/` directory (the one holding the other private files,
production.md; check it with `ls -ld` first), and `$PRIVATE/inference` is the directory the worker mounts. The delivery
stages the key beside the live one, proves the copy and that the NEW file works, keeps the old file, and only then
renames, so a failed or truncated transfer can never replace the working credential:

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
KEY=/Users/_deedkitinf/keys/messaging-enrichment.key    # keys/messaging-enrichment.new.key for a rotation
# 1. copy into a staging file; the two sessions are separate steps, and the result is checked, not assumed
ssh "$MINI" "sudo -n test -s $KEY" || { echo "no key file on the mini"; exit 1; }
ssh "$MINI" "sudo -n cat $KEY" | ssh "$PROD" "umask 077; mkdir -p $PRIVATE/inference && cat > $PRIVATE/inference/.inference.key.new"
# 2. same bytes? (the sha256 of the key is what policy.json holds anyway)
A="$(ssh "$MINI" "sudo -n shasum -a 256 $KEY" | cut -d' ' -f1)"; B="$(ssh "$PROD" "sha256sum $PRIVATE/inference/.inference.key.new" | cut -d' ' -f1)"
[ -n "$A" ] && [ "$A" = "$B" ] || { echo "copy differs or failed: nothing replaced"; exit 1; }
# 3. does the NEW file work against the service from production?
ssh "$PROD" "python3 $PRIVATE/../scripts/inference_probe.py --url https://<mini>.<tailnet>.ts.net --key-file $PRIVATE/inference/.inference.key.new"
```

Go on only if step 3 prints `reachable=yes` and a `key_days_left=` value (the deployed checkout's `scripts/` path in
step 3 is the one production.md names). Then, on production (this writes a production file: AGENTS.md section 2 gates
apply, and the key is covered by this card's Astra r1):

```bash
ssh "$PROD" "set -e; cd $PRIVATE/inference; chown 10001:10001 . .inference.key.new; chmod 700 .; chmod 600 .inference.key.new
[ ! -e inference.key ] || cp -p inference.key inference.key.prev
mv .inference.key.new inference.key; ls -ln . ; ls -ldn ."
```

`ls -ln` must show `inference.key` as `-rw-------` owned by uid 10001 and the directory `drwx------` owned by uid
10001 too (the container's uid 10001 must be able to enter the mounted directory; the docker daemon, root, mounts it
whatever the parent's mode). The proof is the read-back **as the worker**, after ANYM-688 mounts the directory:
`docker compose exec -T <worker> sh -c 'test -r <mounted path>/inference.key && echo readable'`, then the probe line
below from inside the worker. Until the worker exists that read-back is open and the card says so. Do not run any of
these lines with `-v`. After the worker's own read-back (below, once ANYM-688 mounts the directory), delete the mini's
copy: `sudo -n -u _deedkitinf rm /Users/_deedkitinf/keys/messaging-enrichment.key`; the hash stays in `policy.json`.
`inference.key.prev` stays until the next rotation succeeds (restore with `mv`).

Read-back from production: `python3 scripts/inference_probe.py --url https://<mini>.<tailnet>.ts.net --key-file
private/inference/inference.key` prints one line, for example `inference mini=<mini>.<tailnet>.ts.net reachable=yes
status=ok queue=0/0/16 admission=closed key_days_left=364 key_expires=2027-11-02T00:00:00Z`, and exits 0 whatever the
answer (no failure of the probe can page or block a deploy). The same from inside the running enrichment worker (the **mounted worker path**, not the host file) once
ANYM-688 exists: `docker compose exec -T <worker> python -I - --url ... --key-file <mounted path> < scripts/inference_probe.py`.

## 12. Rotation that cannot strand the client

At most two keys per principal overlap. Do this at the due date (30 days before expiry), or when a key may have leaked:

1. On the mini (`k` as in section 6): `k --rotate --principal messaging-enrichment --expires-days 365 --key-file
   /Users/_deedkitinf/keys/messaging-enrichment.new.key`. Both keys now work; the new expiry must be later than the old.
2. Deliver the new file to production as in section 11 (stage, compare, probe the new file, keep `.prev`, then `mv`). If the
   copy fails, nothing else happens: the old key still works and nothing is paused.
3. Read back **with the new key** from production, and again from inside the running worker with the credential it has
   mounted: both lines must show `reachable=yes` and `key_expires=<the new key's expiry>`.
4. `k --confirm-readback "<that probe line>" --principal messaging-enrichment`. It accepts only a line whose
   `key_expires` is the new key's expiry (so it was the new key that answered). `--retire-old` is refused until then.
5. `k --retire-old --principal messaging-enrichment` removes only the old hash and bumps the revision. (The gate is procedural at its root: the operator supplies the
   line, and `--confirm-readback` checks only that it names the new key's expiry; the line must come from the worker.)
   Delete the mini's copy of the new key file. Record the new expiry and due date on the card.

If an expiry or a rotation ever leaves the enrichment scope paused as `model_auth_failed` (an indefinite pause,
`resource_admission.py:120-133`), after the read-back of step 3 succeeds clear exactly that pause, one row per account and kind (a data change on
production: the gates of AGENTS.md section 2 apply; run it through the host's documented database access, once for each
paused `account`/`kind`/`profile_hash` the SELECT lists; other accounts sharing the profile are not touched):

```sql
-- 1. look at the row first and save the output (the undo below restores exactly this)
SELECT account, kind, profile_hash, pause_reason, paused_until FROM processing_control
 WHERE account = :'account' AND kind = :'kind' AND profile_hash = :'profile_hash' AND pause_reason = 'model_auth_failed';
-- 2. clear that one row; the statement must report UPDATE 1, else ROLLBACK
BEGIN;
UPDATE processing_control SET pause_reason = NULL, paused_until = NULL, updated_at = clock_timestamp()
 WHERE account = :'account' AND kind = :'kind' AND profile_hash = :'profile_hash' AND pause_reason = 'model_auth_failed';
COMMIT;
-- undo (the pause had no end time, being indefinite):
UPDATE processing_control SET pause_reason = 'model_auth_failed', paused_until = NULL, updated_at = clock_timestamp()
 WHERE account = :'account' AND kind = :'kind' AND profile_hash = :'profile_hash' AND pause_reason IS NULL;
```

## 13. Resource evidence and the duty record (criteria 9 and 10, `[open: measured]`)

With no model loaded, record on the card (with the date): the service's resident size
(`ps -o rss= -p $(sudo -n launchctl print system/com.deedkit.inference | sed -n 's/^[[:space:]]*pid = //p')`, target
under 200 MB), one health read-back (`curl -w '%{time_total}'`), one `uv sync` of a new release (`time` of the sync
step) and one drain-restart (`time python3 scripts/inference_deploy.py <sha>` of the running SHA).

Overload check, once ANYM-683 has pinned an alias (a job needs one): stop the probe job (`launchctl bootout
system/com.deedkit.inference-busy`), write `{"ts": <now>, "between": true, "deedkit_ci": false}` into `busy.json`
(same owner, group and mode) and send the same small request twice from the mini with the probe key: with the file
forced the answer is `503 admission_closed` (`X-Admission-Reason: busy_between`); with `between: false` and a fresh
`ts` it is served. Then `launchctl bootstrap` the probe again and check `busy.json` refreshes. A forced file
goes stale in 30 s, which also proves the stale rule.

Reboot drill: a reboot stops Between's VMs, so this install never does it. It is a remaining live item on the card, run
in a window the owner picks later. Meanwhile record the `bootout` + `bootstrap` equivalent (what a boot does with the two
plists). In the window, with no manual command after the reboot: `launchctl print system/com.deedkit.inference` and
`-busy` are running, `busy.json` is fresh, `/health` answers. Record the date.

24-hour duty record: as the admin account, copy `services/inference/launchd/duty_log.py` and run
`nohup python3 duty_log.py log --out ~/deedkit-inference-duty.log --hours 24 --max-load 8 --need-gb 7.9 &`
(`echo "duty $!|$(ps -o lstart= -p $!)" >> ~/deedkit-inference-setup.created`: the pid and its start time); after 24 h `python3 duty_log.py summarize
~/deedkit-inference-duty.log`. Put on the card: the fraction of the day admission would have been open, the longest
closed stretch, and which rule closed it most. Target for enrichment: open at least 60% of a weekday, no closed
stretch over 3 hours; if missed, adjust `max_load_1m` (per slot) or the definition of the `between` rule from the
data, in a config change before the feature is enabled (ANYM-686 refuses to start otherwise), never by weakening
Between's priority. `--need-gb` is the largest phase's `resident_gb` plus `min_free_gb`.

## 14. Undo of the whole setup

Removes, newest first, only what the ledger names, whole or half-made, and checks that each is gone. The ledger is
removed only when everything is gone; otherwise it is rewritten to what remains and the undo exits 1 (rerun it).
The undo never signals a process: an entry is dropped only on positive evidence (the pid is gone, or now has another start
time); a logger that is still running, or that cannot be inspected, stays in the ledger and the undo exits 1.
Not covered (section-specific undo above): the production key files, the tailnet policy (apply the saved policy), the Serve config
(`serve reset` on the standalone daemon's socket), and the production key file.

```bash
MINI="$(head -1 ~/.config/deedkit/mini-host)"; case "$MINI" in ""|-*|*[[:space:]]*) echo "bad mini address"; exit 1 ;; esac
ssh "$MINI" 'U=_deedkitinf; L=~/deedkit-inference-setup.created; R=~/deedkit-inference-setup.left
[ -f $L ] || { echo "no ledger: nothing of this setup to undo"; exit 0; }
: > $R
sed -n "s/^duty //p" $L | while IFS="|" read -r P S; do
  ps -p $P >/dev/null 2>&1; RC=$?
  if [ $RC = 1 ]; then continue; fi                                  # confirmed gone: the logger ended by itself
  if [ $RC != 0 ]; then echo "duty $P|$S" >> $R; continue; fi         # could not look: keep the entry
  NOW=$(ps -o lstart= -p $P 2>/dev/null); RC=$?
  if [ $RC != 0 ] || [ -z "$NOW" ]; then echo "duty $P|$S" >> $R; continue; fi   # could not tell: keep the entry
  if [ "$NOW" = "$S" ]; then echo "duty $P|$S" >> $R; echo "the duty logger (pid $P) is still running; it stops by itself after its --hours, or: kill $P"
  else echo "pid $P belongs to another process now: the logger is gone"; fi; done
if grep -qx job-service $L; then sudo -n launchctl bootout system/com.deedkit.inference 2>/dev/null
  launchctl print system/com.deedkit.inference >/dev/null 2>&1 && echo job-service >> $R; fi
if grep -qx job-busy $L; then sudo -n launchctl bootout system/com.deedkit.inference-busy 2>/dev/null
  launchctl print system/com.deedkit.inference-busy >/dev/null 2>&1 && echo job-busy >> $R; fi
printf "%s\n" "plist-service /Library/LaunchDaemons/com.deedkit.inference.plist" "plist-busy /Library/LaunchDaemons/com.deedkit.inference-busy.plist" \
  "newsyslog /etc/newsyslog.d/deedkit-inference.conf" "probe-bin /usr/local/libexec/deedkit-busy-probe" \
  "keygen-bin /usr/local/libexec/deedkit-inference-keygen" | while read -r NAME FILE; do
  if grep -qx $NAME $L; then sudo -n rm -f $FILE; [ -e $FILE ] && echo $NAME >> $R; fi; done
if grep -qx profile $L; then sudo -n rm -f /usr/local/etc/deedkit-inference/deedkit-inference.sb; sudo -n rmdir /usr/local/etc/deedkit-inference 2>/dev/null
  [ -e /usr/local/etc/deedkit-inference ] && echo profile >> $R; fi
for D in $(sed -n "s/^stage //p" $L); do case $D in /tmp/deedkit-setup.*) rm -rf "$D"; [ -e "$D" ] && echo "stage $D" >> $R ;; esac; done
if grep -qx shared $L; then sudo -n rm -rf /Users/Shared/deedkit-inference; [ -e /Users/Shared/deedkit-inference ] && echo shared >> $R; fi
if grep -qx home $L; then sudo -n rm -rf /Users/$U; [ -e /Users/$U ] && echo home >> $R; fi
if grep -qx user $L; then dscl . -read /Users/$U >/dev/null 2>&1 && sudo -n dscl . -delete /Users/$U
  dscl . -read /Users/$U >/dev/null 2>&1 && echo user >> $R; fi
if grep -qx group $L; then dscl . -read /Groups/$U >/dev/null 2>&1 && sudo -n dscl . -delete /Groups/$U
  dscl . -read /Groups/$U >/dev/null 2>&1 && echo group >> $R; fi
grep "^id " $L >> $R.id; if [ -s $R ]; then { cat $R.id $R; } > $L; rm -f $R $R.id; echo "undo incomplete, still present:"; cat $L; exit 1; fi
rm -f $R $R.id $L; echo undo-ok'
```

The undo of a half-made install (a failed block) is this same block: it acts on whatever the ledger recorded before
the failed command ran. A mini that rebooted in the middle needs no special case: launchd jobs are only loaded after
section 7.

## Agent-side facts

- The release layout is `releases/<sha>/` (the `services/inference` tree of that commit, its `.venv`, a `RELEASE`
  file and a `.ready` marker written after `uv sync`), `current` and `previous` symlinks, `releases/.uv-cache`.
  The service gets its release SHA from `current/RELEASE` through the launch wrapper (`DEEDKIT_INFERENCE_RELEASE`).
- The service logs to `logs/service.err.log` (stderr: one metadata line per request) and `service.out.log`.
- The deploy lock is `releases/.deploy.lock`, held by a process on the mini for the length of the ssh session.
- Owner D41 («no launchd») was a condition on the test runs (`DECISIONS.md` D41). A long-running service is the owner's
  11-01 request («2 b на мак мини ... инфру сделал чтоб могли все пользоваться»); this runbook reads it that way, and
  the review of ANYM-684 states the reading.
