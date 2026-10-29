# Mac mini setup for DeedKit test runs (agent-run, owner D41/D42/D44, 2026-10-29)

`scripts/remote-test.sh` runs DeedKit test and build suites on the Mac mini as the service user
`_deedkitci`. Until section 1 is done, every run takes the laptop fallback (the script says
`the mini's service user _deedkitci is not set up`). An agent runs section 1 over ssh with `sudo -n`
(egoregerev's sudo needs no password there) after Astra r1 APPROVEs it, because the user and its sudoers
line are an access change (harness process.md section 2). Run it from a DeedKit checkout that holds
`deploy/mac-mini/`, one block at a time; stop at the first failing command. No token or key is involved.

## 1. Service user `_deedkitci`, its shim and its sudoers line

Hidden, not an admin, no login shell, no password login, own primary group (not `staff`: its members can
read egoregerev's home, mode 750 group staff), home `/Users/_deedkitci` (mode 700). UID and GID 452
were free on 2026-10-29; the first lines check again.

```bash
ssh egoregerev@100.88.40.111 'set -e
U=_deedkitci; ID=452
[ -z "$(dscl . -list /Users UniqueID | awk -v id=$ID "\$2==id")" ] || { echo "UID $ID taken"; exit 1; }
[ -z "$(dscl . -list /Groups PrimaryGroupID | awk -v id=$ID "\$2==id")" ] || { echo "GID $ID taken"; exit 1; }
sudo -n dscl . -create /Groups/$U
sudo -n dscl . -create /Groups/$U PrimaryGroupID $ID
sudo -n dscl . -create /Groups/$U RealName "DeedKit test runner"
sudo -n dscl . -create /Groups/$U Password "*"
sudo -n dscl . -create /Users/$U
sudo -n dscl . -create /Users/$U RealName "DeedKit test runner"
sudo -n dscl . -create /Users/$U UniqueID $ID
sudo -n dscl . -create /Users/$U PrimaryGroupID $ID
sudo -n dscl . -create /Users/$U UserShell /usr/bin/false
sudo -n dscl . -create /Users/$U NFSHomeDirectory /Users/$U
sudo -n dscl . -create /Users/$U Password "*"
sudo -n dscl . -create /Users/$U IsHidden 1
sudo -n mkdir -p /Users/$U/deedkit-ci
sudo -n chown -R ${U}:${U} /Users/$U   # ${U}: the mini login shell is zsh, where $U:s is a modifier
sudo -n chmod 700 /Users/$U
id $U'
```

Then the shim (`deploy/mac-mini/deedkit-remote-test`, the only command the sudoers line lets egoregerev
run as `_deedkitci`) and that line (`deploy/mac-mini/sudoers-deedkit-remote-test`):

```bash
scp deploy/mac-mini/deedkit-remote-test deploy/mac-mini/sudoers-deedkit-remote-test egoregerev@100.88.40.111:
ssh egoregerev@100.88.40.111 'set -e
sudo -n visudo -cf sudoers-deedkit-remote-test
sudo -n mkdir -p /usr/local/libexec
sudo -n install -o root -g wheel -m 0755 deedkit-remote-test /usr/local/libexec/deedkit-remote-test
sudo -n install -o root -g wheel -m 0440 sudoers-deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo -n visudo -c
rm deedkit-remote-test sudoers-deedkit-remote-test'
```

Read-back (every line goes in the pull request that ran it):

```bash
ssh egoregerev@100.88.40.111 'id _deedkitci; dscl . -read /Groups/_deedkitci PrimaryGroupID
dseditgroup -o checkmember -m _deedkitci admin; dseditgroup -o checkmember -m _deedkitci staff
ls -ld /Users/_deedkitci /usr/local/libexec/deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo -n visudo -cf /etc/sudoers.d/deedkit-remote-test; sudo -n -l | grep _deedkitci
echo "id -un; ls /Users/egoregerev >/dev/null 2>&1 && echo HOME-READABLE || echo home-closed" | sudo -n -u _deedkitci /usr/local/libexec/deedkit-remote-test'
```

Expected: uid=452 gid=452, `PrimaryGroupID: 452`, NOT a member of `admin` nor `staff`, the home
`drwx------ _deedkitci _deedkitci`, the shim `-rwxr-xr-x root wheel`, the sudoers file `-r--r----- root
wheel` and `parsed OK`, exactly one `(_deedkitci) NOPASSWD: /usr/local/libexec/deedkit-remote-test` line,
then `_deedkitci` and `home-closed`. Anything else: undo and report.

egoregerev's own sudo rule (`(ALL) NOPASSWD: ALL`) would allow more; the dedicated line is the interface
the script uses (`sudo -n -u _deedkitci /usr/local/libexec/deedkit-remote-test`) and keeps working if that
broad rule is narrowed.

Undo:

```bash
ssh egoregerev@100.88.40.111 'sudo -n rm /etc/sudoers.d/deedkit-remote-test /usr/local/libexec/deedkit-remote-test
sudo -n dscl . -delete /Users/_deedkitci && sudo -n dscl . -delete /Groups/_deedkitci && sudo -n rm -r /Users/_deedkitci'
```

## Agent-side state (no sudo)

- Homebrew as egoregerev: `python@3.13 go poppler tesseract tesseract-lang age node@24 postgresql@18`
  (ffmpeg was there). No `brew services`: each run starts its own PostgreSQL and stops it.
- `/Users/Shared/deedkit-ci` (egoregerev, 755): `stage/<worker>` copies and `caps.conf`, the run caps
  (`MAX_RUNS PARALLEL MIN_FREE_GB WAIT_SECONDS BUSY_CHECK MAX_LOAD NICE`, defaults in the script).
- `~_deedkitci/deedkit-ci`: the workers' run copies, `load.log` (`scripts/remote-test.sh --load`) and the
  slot locks.
