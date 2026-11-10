# Android device service

Runs on the Mac or Linux host connected to an Android phone over USB. The DeedKit
gateway calls this service over a private network. The gateway remains on its
existing server and exposes the `android` module through `/mcp/modules/android`.

```
Grok → DeedKit MCP gateway → private HTTP → USB host → ADB → Android apps
```

The client agent chooses each action. A second language model and Maestro are
optional additions for a later workflow layer.

## Tools

| Tool | Purpose |
| --- | --- |
| `android_capabilities` | Granted devices, connection states and command names |
| `android_session` | Acquire, renew or release exclusive control of a phone |
| `android_observe` | PNG screenshot plus Android UI hierarchy |
| `android_execute` | Run device-targeted ADB arguments and retain a result receipt |
| `android_get_operation` | Read or wait for the receipt using the original request UUID |
| `android_upload` | Upload an APK or another file in bounded chunks |
| `android_download` | Read command output or a pulled file in bounded chunks |
| `android_delete_file` | Release stored files after use |

`android_execute` accepts `shell`, `exec-out`, `reboot`, `logcat`, `get-state`,
`get-devpath`, `wait-for-device`, `root`, `unroot`, `remount`, `disable-verity`,
`enable-verity`, `reconnect`, `jdwp`, `push`, `pull`, `install`, `install-multiple`
and `uninstall`. Shell commands can run Android input, intents, package and settings
operations. Android's own permission checks still apply. `wait-for-device` and
`reconnect` take no further arguments. ADB server administration and arbitrary
commands on the USB host are outside this interface.

`push` takes a completed upload UUID and a device destination. `pull` takes one
device file path and returns a download UUID. It transfers regular file bytes.
`install` takes options followed by one upload UUID. `install-multiple` takes
options followed by one or more upload UUIDs. Each completed upload is staged
under a private `.apk` name for that command. Supported switches are `-r`, `-t`,
`-d`, `-g`, `-s`, `-f`, `-p`, `--instant`, `--full`, `--dont-kill`,
`--enable-rollback`, `--force-queryable`, `--no-streaming`, `--streaming`,
`--no-incremental`, `--staged` and `--force-non-staged`. The options `-i`,
`--abi`, `--user`, `--install-location` and `--install-reason` take one value.
Values contain letters, digits, underscores, dots or hyphens and cannot end
in `.apk` or `.apex`. Shell and `exec-out` retain ADB's command-line quoting
semantics.

The MCP observation result contains a native image content block. Its structured
result includes image size and SHA-256, plus the UI nodes. The REST result carries
the PNG as base64. Password nodes omit their text and content description.
Secure Android screens may withhold screenshots or hierarchy information.

## Install on the USB host

Install Android SDK Platform Tools from Google's official distribution and
authorize this host on the unlocked phone. Confirm its serial using `adb devices -l`.
Install the service from this checkout with Python 3.13 and uv.

```sh
cd services/android
uv sync --locked
```

Write a private configuration file with mode `0600`. The serial, binary path and
state directory below are examples. Hash a separate random backend token using
SHA-256 and put only its hash in this file.

```json
{
  "adb": "/opt/android/platform-tools/adb",
  "state_dir": "/private/deedkit-android/state",
  "devices": {"home-phone": "YOUR_DEVICE_SERIAL"},
  "principals": [{
    "id": "home-agent",
    "key_sha256": "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS",
    "devices": ["home-phone"],
    "permissions": ["android:read", "android:control"]
  }]
}
```

Start one process, using the host's actual Tailscale IPv4 address for a gateway
on another machine. Loopback is the default and is suitable for an SSH tunnel.

```sh
uv run deedkit-android --config /private/deedkit-android/config.json --host 100.64.0.10
```

Use launchd on macOS or a service manager on Linux for persistent operation.
Supply absolute paths and one worker. A file lock prevents two service processes
from sharing the state directory. Expose the port only to the gateway through
the private network. Keep ADB connected over USB.

## Connect the gateway

Deploy a gateway build containing the Android catalog before adding `android` to
its policy. Add the private service URL under `deployment.backends.android`, include
`android` in `deployment.modules`, and add the eight exact tool names to the chosen
profile. Grant that principal `android:read` and `android:control`. Its Android
credential uses a private `key_file` holding the backend token and `key_sha256`
matching that token. The MCP client continues using its separate gateway token.

Follow the gateway's existing policy replacement procedure and increment its
revision. Backend URL changes require a gateway restart. For rollback, remove
the Android policy entries before returning to an older gateway build.

Use one backend principal and token for each independently authorized gateway
principal. Receipts, reservations and files are scoped to that backend identity.
Grant both scopes to an agent that operates the phone. A read-only principal can
inspect capabilities and its previously created receipts and files.

## Execution and recovery

1. Read capabilities and acquire a session for the chosen device.
2. Observe the screen, then execute one intended action with a fresh request UUID.
3. Keep that UUID and read its receipt until it leaves `running`.
4. Observe again before deciding on another UI action.
5. Renew the reservation before its expiry and release it when finished.

A duplicate request UUID with the same device, arguments and command timeout
returns its existing receipt. Different arguments produce `request_id_conflict`.
Completed receipts remain readable after reservation expiry. Only one command or
observation runs on a device at a time, including after its reservation expires.

The service records admission before starting a command. Timeout, service shutdown
or restart may leave `unknown` because the phone can already have acted. Inspect
the phone and recover using the original UUID. The service never replays such a
command. An interrupted HTTP response can leave a command running in the service.
The gateway includes the original UUID in its unknown-outcome response.

An acquire whose reply is lost keeps its reservation until the bounded expiry.
Acquire a new session after that time. Uploads accept identical chunk retries,
including a partially written final chunk after a crash. File deletion is idempotent
and waits until device commands have finished.

Commands have a 1–600 second timeout and can be polled with a 0–25 second wait.
File chunks hold up to 32 KiB, files up to 256 MiB and stored files up to 512 MiB
in total. Delete completed transfers to free the quota. Command stdout and stderr
are bounded at 4 MiB each. Large or binary stdout becomes a downloadable file.
Observation PNGs have a separate 16 MiB ceiling, to preserve full-resolution phone screenshots.
Receipts persist in SQLite. Preserve the state directory across upgrades to retain
duplicate-request protection.

## Verification

The service tests use a synthetic ADB executable and no phone or user accounts.

```sh
uv run --locked --group dev ruff check src tests
uv run --locked --group dev pytest -q
```

Gateway tests verify tool discovery, permission intersections, native image
delivery, isolated backend failure and recovery after a lost command response.
