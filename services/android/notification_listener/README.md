# USB order notification source

Source/build boundary only. No installation, notification-access grant, ADB session or real
notification has been used during development. See [the host contract](../../../docs/android-order-events.md).

The app has only a system-bound NotificationListenerService. It has no launcher UI, INTERNET,
Accessibility or secret-entry permission. Notification access is a separate Android settings
privilege and remains an explicit root rollout step. The service keeps at most 256 metadata
records in app-private storage, backups disabled. Revoking access closes the socket.

Build from this directory with JDK17, Android SDK36, Gradle8.11.1 and the pinned AGP8.10.1
([official compatibility](https://developer.android.com/build/releases/agp-8-10-0-release-notes)):

```sh
gradle --no-daemon assembleDebug
```

No wrapper/download or signing credential is added to the repository. A release APK needs the
installation owner's existing signing route. Debug APK is for isolated source validation only.
The listener and journal also compile with `javac --release 17 -cp <sdk>/platforms/android-36/android.jar`;
that check is not an installed APK/device test. An isolated javac/D8/aapt2 unsigned APK
source check also passed on SDK36/min26; no signing or installation was attempted. JVM journal coverage runs in
`services/android/tests/test_order_events_source.py` when JDK17+ is available.

Root's eventual rollout must allocate the phone window, build/sign the exact green source,
install the APK, enable notification access, establish a serial-specific existing-auth ADB
forward to `localabstract:deedkit-order-events`, and prove shell/root peer identity on that OEM.
None of those actions is performed by the source helper or authorized by this README.

The host sends one newline-terminated JSON watch request (max4096 bytes):
`{"version":1,"packages":["synthetic.food"],"source_epoch":null,"after":0}`.
An empty packages list disables collection. This configuration persists privately across
source restarts. One subscriber consumes a stream; disconnected hosts resume by epoch/sequence.
`hello` reports epoch, oldest/last sequence and `gap`; observations contain package,
`observation_ref` (SHA256 of the platform notification key), observed/posted times and sequence.
There is no assumed app order-ID field. A heartbeat carries last sequence; no model is invoked.
A journal or configuration failure closes delivery; host reports disconnect and retains timers.
Host tests must prove pending obligations survive revoked access, USB loss and journal gaps.

Undo in root's rollout: disconnect only this forward, revoke this app's notification access,
and stop collection. Preserve the host's obligations and evidence; do not alter another ADB
client, native session, provider session or app's permissions.
