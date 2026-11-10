#!/usr/bin/env python3
"""Build the pinned loopback-only driver. Requires JDK 17 and Android SDK 35.

Usage: python3 build_uiautomator.py /empty/build-directory /output/uiautomator.jar
No phone is accessed. Source/license remain in the supplied build directory.
"""

import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile

SOURCE = 'https://github.com/openatx/android-uiautomator-server-jar.git'
REVISION = '3da9d86b0fc241a98533b07ab6e08b534b1b3947'


def build(directory, output):
    subprocess.run(['git', 'clone', '--no-checkout', SOURCE, str(directory)], check=True)
    subprocess.run(['git', '-C', str(directory), 'checkout', '--detach', REVISION], check=True)
    path = directory / 'app/src/main/java/com/wetest/uia2/stub/AutomatorHttpServer.java'
    text = path.read_text()
    assert text.count('super(port);') == 1
    text = text.replace('super(port);', 'super("127.0.0.1", port);')
    logging = '        Log.d(String.format("URI: %s, Method: %s, params, %s, files: %s", uri, method, params, files));'
    assert text.count(logging) == 1
    path.write_text(text.replace(logging, '        // Device contents must never enter logcat.'))
    path = directory / 'app/src/main/java/com/github/uiautomator2/Console.java'
    text = path.read_text().replace('http server listening on *:', 'http server listening on 127.0.0.1:')
    # Upstream info logs include clipboard and toast contents.
    path.write_text(text.replace('Ln.i("[UiAutomator2Server] Starting Server");',
                                 'Ln.initLogLevel(Ln.Level.ERROR);'))
    subprocess.run(['./gradlew', '--no-daemon', ':app:assembleDebug'], cwd=directory, check=True)
    apk = directory / 'app/build/outputs/apk/debug/app-debug.apk'
    # app_process needs DEX and embedded resources. Stable zip metadata makes the
    # resulting artifact hash independent of packaging timestamps.
    with zipfile.ZipFile(apk) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as dest:
        for name in sorted(source.namelist()):
            if name.startswith('META-INF/') or name.endswith('/'):
                continue
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            dest.writestr(info, source.read(name), compress_type=zipfile.ZIP_DEFLATED)
    print(hashlib.sha256(output.read_bytes()).hexdigest(), output)


if __name__ == '__main__':
    build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
