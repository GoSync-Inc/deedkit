"""Source protocol fixtures and JVM journal tests; never connect to ADB."""
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from deedkit_android.order_events import OrderEvents, OrderRefused
from deedkit_android.order_events_source import UsbOrderSource


# spec: ANYM-997
def test_usb_cursor_restart_gap_app_scope_and_dedup(tmp_path):
    db = sqlite3.connect(tmp_path / 'orders.db')
    events = OrderEvents(db, lambda *args: True, clock=lambda: 1000)
    events.bind('owner', owner='owner', device_id='phone', order_id='food', package='app.food',
                source_id='usb-source', source_order_id='known-key', auth_epoch=1,
                original_request_id='request', native_thread_id='thread', original_user_words='Original words',
                next_check_at=1100)
    source = UsbOrderSource(events, principal='listener', owner='owner', device_id='phone',
                            source_id='usb-source', auth_epoch=1)
    frame = dict(type='observation', source_epoch='epoch', sequence=1, package='app.food',
                 observed_at=1000, observation_ref='opaque-ref')
    with pytest.raises(OrderRefused):
        source.consume(frame)
    source.consume(dict(type='hello', version=1, source_epoch='epoch', oldest_sequence=1, last_sequence=2, gap=True))
    assert source.status()['hold_reason'] == 'observation_gap'
    assert source.consume(frame) == 'recorded'
    restarted = UsbOrderSource(events, principal='listener', owner='owner', device_id='phone',
                               source_id='usb-source', auth_epoch=1)
    assert restarted.watch(['app.food'])['after'] == 1
    restarted.consume(dict(type='hello', version=1, source_epoch='epoch', oldest_sequence=1, last_sequence=2, gap=False))
    assert restarted.consume(frame) == 'duplicate'
    assert restarted.consume(frame | {'sequence': 2, 'package': 'unrelated.app'}) == 'order_not_bound'
    assert len(events.observations('owner', 'owner', 'food')) == 1
    assert events.observations('owner', 'owner', 'food')[0]['correlation'] == 'unresolved_app'
    db.close()


# spec: ANYM-997
def test_companion_journal_commit_restart_bounds_and_usb_peer(tmp_path):
    javac, java = shutil.which('javac'), shutil.which('java')
    if not javac or not java:
        pytest.skip('JDK needed for isolated companion journal check')
    source = Path(__file__).parents[1] / 'notification_listener/src/main/java/app/deedkit/orderevents/EventJournal.java'
    harness = tmp_path / 'JournalCheck.java'
    harness.write_text('''
        import app.deedkit.orderevents.EventJournal;
        import java.io.File;
        public class JournalCheck {
          public static void main(String[] args) throws Exception {
            File path = new File(args[0]);
            EventJournal first = new EventJournal(path, 2);
            first.append("{\\"event\\":1}"); first.append("{\\"event\\":2}"); first.append("{\\"event\\":3}");
            EventJournal restarted = new EventJournal(path, 2);
            if (!first.epoch.equals(restarted.epoch) || restarted.last()!=3 || restarted.oldest()!=2
                || restarted.after(0).size()!=2 || restarted.after(2).get(0).sequence()!=3)
              throw new AssertionError("journal_restart");
            if (!EventJournal.usbPeerAllowed(2000) || !EventJournal.usbPeerAllowed(0)
                || EventJournal.usbPeerAllowed(10001)) throw new AssertionError("peer_fence");
            boolean refused=false;
            try { restarted.append("unbounded\\nevent"); } catch(java.io.IOException e) {refused=true;}
            if (!refused || restarted.last()!=3) throw new AssertionError("invalid_frame");
          }
        }
    ''')
    subprocess.run([javac, '--release', '17', '-d', str(tmp_path), str(source), str(harness)], check=True,
                   capture_output=True, timeout=30)
    subprocess.run([java, '-cp', str(tmp_path), 'JournalCheck', str(tmp_path / 'journal')], check=True,
                   capture_output=True, timeout=30)
