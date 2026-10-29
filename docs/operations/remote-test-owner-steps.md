# Owner steps: DeedKit test runs on the Mac mini (owner D41/D42, 2026-10-29)

`scripts/remote-test.sh` runs DeedKit test and build suites on the Mac mini as the service user
`_deedkitci`. Until section 1 is done, every run takes the laptop fallback (the script says
`the mini's service user _deedkitci is not set up`). The steps need the mini's sudo password, so only the
owner runs them, from the laptop, in a DeedKit checkout of `main`. No token or key is involved.

## 1. Service user `_deedkitci`, its shim and its sudoers line

Hidden, not an admin, no login shell, no password login, own primary group (not `staff`, whose members can
read egoregerev's home), home `/Users/_deedkitci` (mode 700). UID and GID 452 were free on 2026-10-29;
the first lines check again.

```bash
ssh -t egoregerev@100.88.40.111 'set -e
U=_deedkitci; ID=452
[ -z "$(dscl . -list /Users UniqueID | awk -v id=$ID "\$2==id")" ] || { echo "UID $ID taken"; exit 1; }
[ -z "$(dscl . -list /Groups PrimaryGroupID | awk -v id=$ID "\$2==id")" ] || { echo "GID $ID taken"; exit 1; }
sudo dscl . -create /Groups/$U
sudo dscl . -create /Groups/$U PrimaryGroupID $ID
sudo dscl . -create /Groups/$U RealName "DeedKit test runner"
sudo dscl . -create /Groups/$U Password "*"
sudo dscl . -create /Users/$U
sudo dscl . -create /Users/$U RealName "DeedKit test runner"
sudo dscl . -create /Users/$U UniqueID $ID
sudo dscl . -create /Users/$U PrimaryGroupID $ID
sudo dscl . -create /Users/$U UserShell /usr/bin/false
sudo dscl . -create /Users/$U NFSHomeDirectory /Users/$U
sudo dscl . -create /Users/$U Password "*"
sudo dscl . -create /Users/$U IsHidden 1
sudo mkdir -p /Users/$U/deedkit-ci
sudo chown -R ${U}:${U} /Users/$U   # ${U}: the mini login shell is zsh, where $U:s is a modifier
sudo chmod 700 /Users/$U
id $U'
```

Then the shim (`deploy/mac-mini/deedkit-remote-test`, the only command egoregerev may run as
`_deedkitci`) and its sudoers line (`deploy/mac-mini/sudoers-deedkit-remote-test`):

```bash
scp deploy/mac-mini/deedkit-remote-test deploy/mac-mini/sudoers-deedkit-remote-test egoregerev@100.88.40.111:
ssh -t egoregerev@100.88.40.111 'set -e
sudo visudo -cf sudoers-deedkit-remote-test
sudo mkdir -p /usr/local/libexec
sudo install -o root -g wheel -m 0755 deedkit-remote-test /usr/local/libexec/deedkit-remote-test
sudo install -o root -g wheel -m 0440 sudoers-deedkit-remote-test /etc/sudoers.d/deedkit-remote-test
sudo visudo -c
rm deedkit-remote-test sudoers-deedkit-remote-test
sudo -k'
```

Check (no password prompt may appear; the agent can run this one):

```bash
ssh egoregerev@100.88.40.111 'dseditgroup -o checkmember -m _deedkitci admin; dseditgroup -o checkmember -m _deedkitci staff
echo "id -un; ls /Users/egoregerev >/dev/null 2>&1 && echo HOME-READABLE || echo home-closed" | sudo -n -u _deedkitci /usr/local/libexec/deedkit-remote-test'
```

Expected: `no _deedkitci is NOT a member of admin`, the same for `staff`, then `_deedkitci` and
`home-closed`. Anything else: stop and tell the orchestrator.

Undo:

```bash
ssh -t egoregerev@100.88.40.111 'sudo rm /etc/sudoers.d/deedkit-remote-test /usr/local/libexec/deedkit-remote-test
sudo dscl . -delete /Users/_deedkitci && sudo dscl . -delete /Groups/_deedkitci && sudo rm -r /Users/_deedkitci'
```

## Agent-side state (no sudo; already in place or recreated by any agent)

- Homebrew as egoregerev: `python@3.13 go poppler tesseract tesseract-lang age node@24 postgresql@18`
  (ffmpeg was there). No `brew services`: each run starts its own PostgreSQL and stops it.
- `/Users/Shared/deedkit-ci` (egoregerev, 755): `stage/<worker>` copies and `caps.conf`, the run caps
  (`MAX_RUNS PARALLEL MIN_FREE_GB WAIT_SECONDS BUSY_CHECK MAX_LOAD NICE`, defaults in the script).
- `~_deedkitci/deedkit-ci`: the workers' run copies, `load.log` (`scripts/remote-test.sh --load`) and the
  slot locks.
