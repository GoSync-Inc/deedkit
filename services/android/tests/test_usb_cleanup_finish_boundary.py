"""Owned undo after the subscription ended must finish once, retaining original admission checks."""
import asyncio

import pytest

from deedkit_android.order_events_usb import UsbForward, UsbOrderConnector
from test_usb_recovery_synchronization import awaiting_retention


def cleanup_entry():
    entry, held = awaiting_retention()
    held[0] = False  # foreground released; retention was denied, so no read authority remains
    connector = object.__new__(UsbOrderConnector)
    entry.connector = connector
    connector.closing = False
    connector._finishing = False
    connector.cleanup_s = 1
    connector.reason = 'source_access_changed'
    connector._remember_gap = lambda: None
    connector._release = lambda: None
    connector._hold = lambda reason: None
    seen = []

    async def close_writer():
        pass

    async def emptywatch():
        entry.admit()  # actual owner/device/epoch/session admission, allowing owned cleanup only
        seen.append('emptywatch')

    def close_forward():
        seen.append('close_forward')
        return True

    connector._close_writer = close_writer
    connector._emptywatch = emptywatch
    connector.forward = UsbForward('serial', 1, close_forward, 'original-receipt')
    return entry, connector, seen


# spec: ANYM-997
def test_natural_finish_marks_owned_cleanup_before_original_run_authority_has_ended():
    _, connector, seen = cleanup_entry()
    asyncio.run(connector._finish())
    assert seen == ['emptywatch', 'close_forward']
    assert connector.cleanup == 'confirmed' and connector.forward is None


# spec: ANYM-997
def test_close_joining_already_started_cleanup_never_cancels_that_undo():
    async def exercise():
        connector = object.__new__(UsbOrderConnector)
        connector.closing = True
        connector._finishing = True
        connector._close_requested = False
        connector._hold = lambda reason: None
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []

        async def cleanup():
            entered.set()
            await release.wait()
            completed.append('original cleanup completed')

        connector.task = asyncio.create_task(cleanup())
        await entered.wait()
        closing = asyncio.create_task(connector.close())
        await asyncio.sleep(0)  # schedule the close request; not a timing deadline
        release.set()
        await closing
        assert completed == ['original cleanup completed'] and not connector.task.cancelled()
    asyncio.run(exercise())


# spec: ANYM-997
@pytest.mark.parametrize('change', ['owner', 'rights', 'epoch'])
def test_owned_cleanup_does_not_restore_foreign_or_revoked_original_authority(change):
    entry, connector, seen = cleanup_entry()
    if change == 'owner':
        entry.authorize().id = 'other'
    elif change == 'rights':
        entry.authorize().permissions = []
    else:
        entry.agents.read_config().agent.order_events = 'changed-source'
    asyncio.run(connector._finish())
    assert seen == ['close_forward'] and connector.cleanup == 'cleanup_unconfirmed'
