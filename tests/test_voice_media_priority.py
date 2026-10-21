"""A voice note that is not downloaded yet says how to get it; a fresh download is transcribed first."""
import hashlib

import pytest

from messaging_service.store import APIError
from test_archive import archive
from test_enrichment import worker
from test_media_requests import pending


# spec: ANYM-455
def test_reading_an_undownloaded_attachment_names_the_download_call(archive):
    ar,s,key,_=archive; fid=pending(ar,s)
    result=s.get_file_text(key,fid)
    assert result['empty_reason']=='attachment_not_downloaded'
    assert result['attachment_id']==fid and result['text']==''
    assert result['download_status']=='queued' and result['reason'] in ('outside_prefetch_window','queued')
    assert result['next_action']=='request_file_download'
    status=s.get_attachment(key,fid)
    assert (result['reason'],result['next_action'])==(status['reason'],status['next_action'])
    with s.pool.connection() as c:
        assert c.execute('SELECT requested_until FROM media_jobs WHERE id=%s',(fid,)).fetchone()['requested_until'] is None
    s.set_access('archive-reader',chats=[])
    with pytest.raises(APIError,match='not_found'):s.get_file_text(key,fid)
    s.set_access('archive-reader',chats=None)
    with pytest.raises(APIError,match='not_found'):s.get_file_text(key,'unknown-attachment')


# spec: ANYM-455
def test_recently_downloaded_old_note_is_transcribed_as_fresh(seeded,tmp_path):
    s,_,_=seeded
    with s.pool.connection() as c:
        for attachment,message,days,resident in [('old-download','demo-tg-1',30,'0 days'),('newer-note','demo-tg-2',5,'3 days')]:
            data=attachment.encode();sha=hashlib.sha256(data).hexdigest();(s.files/sha).write_bytes(data)
            c.execute("UPDATE messages SET sent_at=now()-%s::interval WHERE id=%s",(f'{days} days',message))
            c.execute("INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,'voice.ogg','audio/ogg',%s)",
                (attachment,message,sha,len(data)))
            c.execute('''INSERT INTO attachment_residency(digest,bytes,updated_at) VALUES(%s,%s,now()-%s::interval)
                ON CONFLICT(digest) DO UPDATE SET updated_at=excluded.updated_at''',(sha,len(data),resident))
    row,_=worker(s,tmp_path).claim()
    assert row['attachment']=='old-download' and row['fresh'] is True
