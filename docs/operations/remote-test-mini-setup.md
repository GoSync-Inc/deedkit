# Mac mini setup for DeedKit test runs (agent-run, owner D41/D42/D44, 2026-10-29)

`scripts/remote-test.sh` runs DeedKit test and build suites on the Mac mini as the service user
`_deedkitci`. Until section 1 is done, every run takes the laptop fallback (the script says
`the mini's service user _deedkitci is not set up`). An agent runs section 1 over ssh with `sudo -n`
(egoregerev's sudo needs no password there) after Astra r1 APPROVEs it, because the user and its sudoers
line are an access change (harness process.md section 2). Run it from a DeedKit checkout that holds
`deploy/mac-mini/`, one block at a time. Stop at the first failing command, run the undo, and report.
No token or key is involved.

## 1. Service user `_deedkitci`, its shim and its sudoers line

The user is hidden, not an admin, with no login shell and no password login. It has its own primary
group (not `staff`: staff members can read egoregerev's home, mode 750 group staff) and the home
`/Users/_deedkitci` (mode 700). UID and GID 452 were free on 2026-10-29.

Preflight (read-only). Every resource the next steps create must be absent, so nothing that exists is
modified, and the undo removes only what these steps made. Go on only after `preflight-ok`:

```bash
ssh egoregerev@100.88.40.111 'U=_deedkitci; ID=452; fail=0
dscl . -read /Users/$U >/dev/null 2>&1 && { echo "user $U exists"; fail=1; }
dscl . -read /Groups/$U >/dev/null 2>&1 && { echo "group $U exists"; fail=1; }
[ -z "$(dscl . -list /Users UniqueID | awk -v id=$ID "\$2==id")" ] || { echo "UID $ID taken"; fail=1; }
[ -z "$(dscl . -list /Groups PrimaryGroupID | awk -v id=$ID "\$2==id")" ] || { echo "GID $ID taken"; fail=1; }
for p in /Users/$U /usr/local/libexec/deedkit-remote-test /etc/sudoers.d/deedkit-remote-test; do
  [ ! -e $p ] || { echo "$p exists"; fail=1; }; done
[ $fail = 0 ] && echo preflight-ok'
```

Step 1, the group:

```bash
ssh egoregerev@100.88.40.111 'set -e; U=_deedkitci; ID=452
sudo -n dscl . -create /Groups/$U
sudo -n dscl . -create /Groups/$U PrimaryGroupID $ID
sudo -n dscl . -create /Groups/$U RealName "DeedKit test runner"
sudo -n dscl . -create /Groups/$U Password "*"'
```

Step 2, the user:

```bash
ssh egoregerev@100.88.40.111 'set -e; U=_deedkitci; ID=452
sudo -n dscl . -create /Users/$U
sudo -n dscl . -create /Users/$U RealName "DeedKit test runner"
sudo -n dscl . -create /Users/$U UniqueID $ID
sudo -n dscl . -create /Users/$U PrimaryGroupID $ID
sudo -n dscl . -create /Users/$U UserShell /usr/bin/false
sudo -n dscl . -create /Users/$U NFSHomeDirectory /Users/$U
sudo -n dscl . -create /Users/$U Password "*"
sudo -n dscl . -create /Users/$U IsHidden 1'
```

Step 3, the home (new: the preflight showed `/Users/_deedkitci` absent):

```bash
ssh egoregerev@100.88.40.111 'set -e; U=_deedkitci
sudo -n mkdir -m 700 /Users/$U
sudo -n mkdir /Users/$U/deedkit-ci
sudo -n chown -R ${U}:${U} /Users/$U   # ${U}: the mini login shell is zsh, where $U:s is a modifier
sudo -n chmod 700 /Users/$U'
```

Step 4: the shim (`deploy/mac-mini/deedkit-remote-test`), which is the only command the sudoers line lets
egoregerev run as `_deedkitci`, and that line (`deploy/mac-mini/sudoers-deedkit-remote-test`). The
block stops before installing if either target exists, so `install` never replaces a file:

```bash
scp deploy/mac-mini/deedkit-remote-test deploy/mac-mini/sudoers-deedkit-remote-test egoregerev@100.88.40.111:
ssh egoregerev@100.88.40.111 'set -e
sudo -n visudo -cf sudoers-deedkit-remote-test
[ ! -e /usr/local/libexec/deedkit-remote-test ] && [ ! -e /etc/sudoers.d/deedkit-remote-test ]
sudo -n mkdir -p /usr/local/libexec
sudo -n install -o root -g wheel -m 0755 deedkit-remote-test /usr/local/libexec/deedkit-remote-test
sudo -n install -o root -g wheel -m 0440 sudoers-deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo -n visudo -c
rm deedkit-remote-test sudoers-deedkit-remote-test'
```

Read-back (every line goes in the pull request that ran it):

```bash
ssh egoregerev@100.88.40.111 'U=_deedkitci; id $U
dscl . -read /Users/$U UniqueID PrimaryGroupID UserShell NFSHomeDirectory IsHidden
dscl . -read /Users/$U Password; dscl . -read /Groups/$U PrimaryGroupID
dseditgroup -o checkmember -m $U admin; dseditgroup -o checkmember -m $U staff
ls -ld /Users/$U /Users/$U/deedkit-ci /usr/local/libexec/deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo -n visudo -cf /etc/sudoers.d/deedkit-remote-test; sudo -n -l | grep -n $U
echo "id -un; cd /Users/egoregerev 2>/dev/null && echo HOME-ENTERABLE || echo home-closed
ls /Users/egoregerev/telegram-session >/dev/null 2>&1 && echo SESSIONS-READABLE || echo sessions-closed
ls /Users/Shared/deedkit-ci/stage >/dev/null && echo stage-readable" | sudo -n -u $U /usr/local/libexec/deedkit-remote-test'
```

Expected:

- `uid=452(_deedkitci) gid=452(_deedkitci)`, and `UniqueID: 452`, `PrimaryGroupID: 452`,
  `UserShell: /usr/bin/false`, `NFSHomeDirectory: /Users/_deedkitci`, `IsHidden: 1`.
- `Password: *` (or `********`), and the group's `PrimaryGroupID: 452`.
- NOT a member of `admin`, and NOT a member of `staff`.
- `drwx------ _deedkitci _deedkitci` for the home, `drwxr-xr-x _deedkitci _deedkitci` for `deedkit-ci`,
  `-rwxr-xr-x root wheel` for the shim, and `-r--r----- root wheel` for the sudoers file.
- `parsed OK`, and exactly one `(_deedkitci) NOPASSWD: /usr/local/libexec/deedkit-remote-test` line.
- Then `_deedkitci`, `home-closed`, `sessions-closed` and `stage-readable`.

Anything else: undo and report.

egoregerev's own sudo rule (`(ALL) NOPASSWD: ALL`) would allow more. The dedicated line is the interface
the script uses (`sudo -n -u _deedkitci /usr/local/libexec/deedkit-remote-test`), and it keeps working if
that broad rule is narrowed. The shim runs whatever script arrives on stdin as `_deedkitci`, by design:
the suites run arbitrary repository code anyway, and the user's isolation is what protects
egoregerev's files and sessions.

Undo. Each line removes one resource only if it is the one these steps created, independent of the others,
so it also cleans up a partial setup:

```bash
ssh egoregerev@100.88.40.111 'U=_deedkitci
[ -f /etc/sudoers.d/deedkit-remote-test ] && sudo -n rm /etc/sudoers.d/deedkit-remote-test
[ -f /usr/local/libexec/deedkit-remote-test ] && sudo -n rm /usr/local/libexec/deedkit-remote-test
[ -d /Users/$U ] && [ "$(stat -f %u /Users/$U)" = 452 ] && sudo -n rm -r /Users/$U
[ "$(dscl . -read /Users/$U UniqueID 2>/dev/null | awk "{print \$2}")" = 452 ] && sudo -n dscl . -delete /Users/$U
[ "$(dscl . -read /Groups/$U PrimaryGroupID 2>/dev/null | awk "{print \$2}")" = 452 ] && sudo -n dscl . -delete /Groups/$U
sudo -n visudo -c; true'
```

## Agent-side state (no sudo)

- Homebrew as egoregerev: `python@3.13 go poppler tesseract tesseract-lang age node@24 postgresql@18`
  (ffmpeg was there already). No `brew services`: each run starts its own PostgreSQL and stops it.
- `/Users/Shared/deedkit-ci` (egoregerev, 755): the `stage/<worker>` copies and `caps.conf`, the run caps
  (`MAX_RUNS PARALLEL MIN_FREE_GB WAIT_SECONDS BUSY_CHECK MAX_LOAD NICE`, defaults in the script).
- `~_deedkitci/deedkit-ci`: the workers' run copies, `load.log` (`scripts/remote-test.sh --load`) and the
  slot locks.
