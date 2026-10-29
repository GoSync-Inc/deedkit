# Mac mini setup for DeedKit test runs (agent-run, owner D41/D42/D44, 2026-10-29)

`scripts/remote-test.sh` runs DeedKit test and build suites on the Mac mini as the service user
`_deedkitci`. Until section 1 is done, a run exits 92 ("not set up, CI covers"); nothing runs on the
laptop. An agent runs section 1 over ssh with `sudo -n` (egoregerev's sudo needs no password there) after
Astra APPROVEs it, because the user and its sudoers line are an access change (harness process.md section
2). Run it from a DeedKit checkout that holds `deploy/mac-mini/`, one block at a time. Stop at the first
failing block, run the undo, and report. No token or key is involved.

## 1. Service user `_deedkitci`, its shim and its sudoers line

The user is hidden, not an admin, with no login shell and no password login. It has its own primary
group (not `staff`: staff members can read egoregerev's home, mode 750 group staff) and the home
`/Users/_deedkitci` (mode 700). UID and GID 453 were free on 2026-10-29 (452 went to `_harnessci` that day).

The ledger `~/deedkit-ci-setup.created` (egoregerev's home) records each resource *before* the command
that creates it, so the undo removes exactly what this setup made, including a half-made resource, and
nothing that existed before.

Preflight (read-only). Every resource must be absent and the ledger must not exist. Go on only after
`preflight-ok`:

```bash
ssh egoregerev@100.88.40.111 'U=_deedkitci; ID=453; fail=0
[ ! -e ~/deedkit-ci-setup.created ] || { echo "ledger exists: undo or inspect first"; fail=1; }
dscl . -read /Users/$U >/dev/null 2>&1 && { echo "user $U exists"; fail=1; }
dscl . -read /Groups/$U >/dev/null 2>&1 && { echo "group $U exists"; fail=1; }
[ -z "$(dscl . -list /Users UniqueID | awk -v id=$ID "\$2==id")" ] || { echo "UID $ID taken"; fail=1; }
[ -z "$(dscl . -list /Groups PrimaryGroupID | awk -v id=$ID "\$2==id")" ] || { echo "GID $ID taken"; fail=1; }
for p in /Users/$U /usr/local/libexec/deedkit-remote-test /etc/sudoers.d/deedkit-remote-test; do
  [ ! -e $p ] || { echo "$p exists"; fail=1; }; done
[ $fail = 0 ] && touch ~/deedkit-ci-setup.created && echo preflight-ok'
```

Each step below first checks again that its resource is absent (`if ...; then exit 1; fi`, which stops
under bash and zsh alike), then records it, then creates it.

Step 1, the group:

```bash
ssh egoregerev@100.88.40.111 'set -e; U=_deedkitci; ID=453
if dscl . -read /Groups/$U >/dev/null 2>&1; then echo "group exists"; exit 1; fi
echo group >> ~/deedkit-ci-setup.created
sudo -n dscl . -create /Groups/$U
sudo -n dscl . -create /Groups/$U PrimaryGroupID $ID
sudo -n dscl . -create /Groups/$U RealName "DeedKit test runner"
sudo -n dscl . -create /Groups/$U Password "*"'
```

Step 2, the user:

```bash
ssh egoregerev@100.88.40.111 'set -e; U=_deedkitci; ID=453
if dscl . -read /Users/$U >/dev/null 2>&1; then echo "user exists"; exit 1; fi
echo user >> ~/deedkit-ci-setup.created
sudo -n dscl . -create /Users/$U
sudo -n dscl . -create /Users/$U RealName "DeedKit test runner"
sudo -n dscl . -create /Users/$U UniqueID $ID
sudo -n dscl . -create /Users/$U PrimaryGroupID $ID
sudo -n dscl . -create /Users/$U UserShell /usr/bin/false
sudo -n dscl . -create /Users/$U NFSHomeDirectory /Users/$U
sudo -n dscl . -create /Users/$U Password "*"
sudo -n dscl . -create /Users/$U IsHidden 1'
```

Step 3, the home:

```bash
ssh egoregerev@100.88.40.111 'set -e; U=_deedkitci
if [ -e /Users/$U ]; then echo "home exists"; exit 1; fi
echo home >> ~/deedkit-ci-setup.created
sudo -n mkdir -m 700 /Users/$U
sudo -n mkdir /Users/$U/deedkit-ci
sudo -n chown -R ${U}:${U} /Users/$U   # ${U}: the mini login shell is zsh, where $U:s is a modifier
sudo -n chmod 700 /Users/$U'
```

Step 4: the shim (`deploy/mac-mini/deedkit-remote-test`), which is the only command the sudoers line lets
egoregerev run as `_deedkitci`, and that line (`deploy/mac-mini/sudoers-deedkit-remote-test`):

The two files travel through a new private directory (`mktemp -d`, mode 700, recorded in the ledger as
`stage <path>` right after it exists), so no file of egoregerev's is overwritten; a failed copy stops the
block before anything is installed.

```bash
D="$(ssh egoregerev@100.88.40.111 'D=$(mktemp -d /tmp/deedkit-setup.XXXXXX) && echo "stage $D" >> ~/deedkit-ci-setup.created && echo $D')" &&
[ -n "$D" ] &&
scp deploy/mac-mini/deedkit-remote-test deploy/mac-mini/sudoers-deedkit-remote-test "egoregerev@100.88.40.111:$D/" &&
ssh egoregerev@100.88.40.111 "D=$D"'; set -e; cd "$D"
sudo -n visudo -cf sudoers-deedkit-remote-test
if [ -e /usr/local/libexec/deedkit-remote-test ]; then echo "shim exists"; exit 1; fi
if [ -e /etc/sudoers.d/deedkit-remote-test ]; then echo "sudoers file exists"; exit 1; fi
sudo -n mkdir -p /usr/local/libexec
echo shim >> ~/deedkit-ci-setup.created
sudo -n install -o root -g wheel -m 0755 deedkit-remote-test /usr/local/libexec/deedkit-remote-test
echo sudoers >> ~/deedkit-ci-setup.created
sudo -n install -o root -g wheel -m 0440 sudoers-deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo -n visudo -c
cd; rm -r "$D"'
```

Read-back (every line goes in the pull request that ran it):

```bash
ssh egoregerev@100.88.40.111 'U=_deedkitci; id $U; cat ~/deedkit-ci-setup.created
P=/Users/Shared/deedkit-ci/stage/.readback; (umask 077; printf x > $P); chmod go+r $P
dscl . -read /Users/$U UniqueID PrimaryGroupID UserShell NFSHomeDirectory IsHidden
dscl . -read /Users/$U Password; dscl . -read /Groups/$U PrimaryGroupID
dseditgroup -o checkmember -m $U admin; dseditgroup -o checkmember -m $U staff
ls -ld /Users/$U /Users/$U/deedkit-ci /usr/local/libexec/deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo -n visudo -cf /etc/sudoers.d/deedkit-remote-test; sudo -n -l | grep -n $U
echo "id -un; cd /Users/egoregerev 2>/dev/null && echo HOME-ENTERABLE || echo home-closed
ls /Users/egoregerev/telegram-session >/dev/null 2>&1 && echo SESSIONS-READABLE || echo sessions-closed
cat /Users/Shared/deedkit-ci/stage/.readback >/dev/null && echo stage-file-readable" | sudo -n -u $U /usr/local/libexec/deedkit-remote-test
rm -f $P'
```

Expected:

- `uid=453(_deedkitci) gid=453(_deedkitci)` and the ledger `group user home stage /tmp/deedkit-setup.<x> shim
  sudoers` (the stage directory itself is gone).
- `UniqueID: 453`, `PrimaryGroupID: 453`, `UserShell: /usr/bin/false`, `NFSHomeDirectory: /Users/_deedkitci`,
  `IsHidden: 1`, `Password: *` (or `********`), and the group's `PrimaryGroupID: 453`.
- NOT a member of `admin`, and NOT a member of `staff`.
- `drwx------ _deedkitci _deedkitci` for the home, `drwxr-xr-x _deedkitci _deedkitci` for `deedkit-ci`,
  `-rwxr-xr-x root wheel` for the shim, and `-r--r----- root wheel` for the sudoers file.
- `parsed OK`, and exactly one `(_deedkitci) NOPASSWD: /usr/local/libexec/deedkit-remote-test` line.
- Then `_deedkitci`, `home-closed`, `sessions-closed` and `stage-file-readable` (a file egoregerev made
  0600 and opened to group and others, as `scripts/remote-test.sh` does with every staged file; the first
  measured run then reads real staged files as `_deedkitci`).

Anything else: undo and report. The ledger stays as the record of what the setup created.

egoregerev's own sudo rule (`(ALL) NOPASSWD: ALL`) would allow more. The dedicated line is the interface
the script uses (`sudo -n -u _deedkitci /usr/local/libexec/deedkit-remote-test`), and it keeps working if
that broad rule is narrowed. The shim runs whatever script arrives on stdin as `_deedkitci`, by design:
the suites run arbitrary repository code anyway, and the user's isolation is what protects egoregerev's
files and sessions.

Undo. It removes, newest first, only what the ledger names, whether whole or half-made, and checks that each
is gone. The ledger is removed only when everything is gone and `visudo -c` passes; otherwise it is rewritten
to the resources still present, the undo prints them and exits 1, and a rerun tries again:

```bash
ssh egoregerev@100.88.40.111 'U=_deedkitci; L=~/deedkit-ci-setup.created; R=~/deedkit-ci-setup.left
[ -f $L ] || { echo "no ledger: nothing of this setup to undo"; exit 0; }
: > $R
if grep -qx sudoers $L; then sudo -n rm -f /etc/sudoers.d/deedkit-remote-test
  if [ -e /etc/sudoers.d/deedkit-remote-test ]; then echo sudoers >> $R; fi; fi
if grep -qx shim $L; then sudo -n rm -f /usr/local/libexec/deedkit-remote-test
  if [ -e /usr/local/libexec/deedkit-remote-test ]; then echo shim >> $R; fi; fi
for D in $(sed -n "s/^stage //p" $L); do
  case $D in /tmp/deedkit-setup.*) rm -rf "$D"; if [ -e "$D" ]; then echo "stage $D" >> $R; fi ;; esac; done
if grep -qx home $L; then sudo -n rm -rf /Users/$U
  if [ -e /Users/$U ]; then echo home >> $R; fi; fi
if grep -qx user $L; then
  if dscl . -read /Users/$U >/dev/null 2>&1; then sudo -n dscl . -delete /Users/$U; fi
  if dscl . -read /Users/$U >/dev/null 2>&1; then echo user >> $R; fi; fi
if grep -qx group $L; then
  if dscl . -read /Groups/$U >/dev/null 2>&1; then sudo -n dscl . -delete /Groups/$U; fi
  if dscl . -read /Groups/$U >/dev/null 2>&1; then echo group >> $R; fi; fi
if ! sudo -n visudo -c >/dev/null; then rm -f $R; echo "visudo -c fails: ledger kept"; exit 1; fi
if [ -s $R ]; then mv $R $L; echo "undo incomplete, still present:"; cat $L; exit 1; fi
rm -f $R $L; echo undo-ok'
```

## Agent-side state (no sudo)

- Homebrew as egoregerev: `python@3.13 go poppler tesseract tesseract-lang age node@24 postgresql@18`
  (ffmpeg was there already). No `brew services`: each run starts its own PostgreSQL and stops it.
- `/Users/Shared/deedkit-ci` (egoregerev, 755): the `stage/<worker>` copies and `caps.conf`, the run caps
  (`MAX_RUNS PARALLEL MIN_FREE_GB MIN_FREE_MEM_GB WAIT_SECONDS POLL_SECONDS BUSY_CHECK MAX_LOAD NICE
  STOP_SECONDS`,
  defaults in the script).
- `~_deedkitci/deedkit-ci`: the workers' run copies, `load.log` (`scripts/remote-test.sh --load`) and the
  slot locks.
