"""The seeded demo file: content-addressed, private, written durably and atomically, and refused when corrupt (ANYM-131).

The real Store.seed writes into a fresh originals directory of this test; the database rows are the seeded
fixture's. The fsync/rename seam only records the real calls and passes them through.
"""
from copy import copy
import hashlib
import os
import stat

import pytest

from messaging_service import store as store_module


@pytest.fixture()
def fresh(seeded, tmp_path):
    store, _, _ = seeded
    seeding = copy(store)
    seeding.files = tmp_path / 'originals'
    return seeding


def attachment(store):
    with store.pool.connection() as c:
        return c.execute("SELECT message,digest,filename,mime,bytes FROM attachments WHERE id='demo-file-1'").fetchone()


# spec: ANYM-131
def test_seed_writes_one_private_content_addressed_file_that_the_demo_attachment_names(fresh):
    fresh.seed()
    [stored] = list(fresh.files.iterdir())
    data = stored.read_bytes()
    assert stored.name == hashlib.sha256(data).hexdigest()  # named by its own content
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600 and stat.S_IMODE(fresh.files.stat().st_mode) == 0o700
    assert attachment(fresh) == {'message': 'demo-tg-2', 'digest': stored.name, 'filename': 'probe-note.txt',
                                 'mime': 'text/plain', 'bytes': len(data)}
    fresh.seed()  # idempotent: the same single file, unchanged
    assert list(fresh.files.iterdir()) == [stored] and stored.read_bytes() == data


# spec: ANYM-131
@pytest.mark.parametrize('damage', ['flipped', 'truncated', 'emptied'])
def test_seed_refuses_a_fixture_whose_bytes_no_longer_match_their_name(fresh, damage):
    fresh.seed()
    [stored] = list(fresh.files.iterdir())
    data = stored.read_bytes()
    stored.write_bytes({'flipped': bytes([data[0] ^ 1]) + data[1:], 'truncated': data[:-1], 'emptied': b''}[damage])
    with pytest.raises(ValueError, match='^Fixture file corrupt$'):
        fresh.seed()
    assert [path.name for path in fresh.files.iterdir()] == [stored.name]  # not replaced or repaired silently


# spec: ANYM-131
def test_seed_fsyncs_the_file_before_renaming_it_into_place_and_then_fsyncs_the_directory(fresh, monkeypatch):
    calls, real_fsync, real_replace = [], os.fsync, os.replace

    def fsync(fd):
        calls.append(('fsync', os.readlink(f'/proc/self/fd/{fd}')))
        return real_fsync(fd)

    def replace(source, target):
        calls.append(('replace', str(source), str(target)))
        return real_replace(source, target)
    monkeypatch.setattr(store_module.os, 'fsync', fsync)
    monkeypatch.setattr(store_module.os, 'replace', replace)
    fresh.seed()
    [stored] = list(fresh.files.iterdir())
    [(_, temporary, target)] = [call for call in calls if call[0] == 'replace']
    assert os.path.dirname(temporary) == str(fresh.files) and os.path.basename(temporary).startswith('pending-')
    assert target == str(stored)
    # The bytes are durable under the temporary name before it becomes the file; then the rename is made durable.
    assert calls == [('fsync', temporary), ('replace', temporary, target), ('fsync', str(fresh.files))]
