from datetime import datetime,timedelta,timezone

from messaging_service.operational import liveness
from messaging_service.status_details import describe, freshness


def test_freshness_uses_scans_not_the_age_of_the_last_message():
    now=datetime.now(timezone.utc)
    assert freshness(now-timedelta(seconds=10),now)['state']=='recent_scan'
    assert freshness(now-timedelta(minutes=6),now)['state']=='scan_stale'
    assert freshness(None,now)['state']=='scan_unknown'
    assert describe('whatsapp_catchup_gap')['next_action']=='inspect_chat_gaps'
    assert describe('version_conflict')['next_action']=='inspect_chat'


def test_liveness_separates_a_quiet_open_session_from_a_broken_one():
    now=datetime.now(timezone.utc)
    def phases(connection='connected',running=True,capture='receiving',refreshed=10,pending=0,event=None):
        last={'committed_by':(now-timedelta(seconds=event)).isoformat(),'origin':'receipt'} if event is not None else None
        return {'connection':{'state':connection,'collector_running':running},
            'live_capture':{'state':capture,'observed_at':(now-timedelta(seconds=refreshed)).isoformat(),
                'pending_events':pending,'last_commit':last}}
    quiet=liveness(phases(event=3*3600),now)
    assert quiet['state']=='idle_connected' and quiet['quiet_but_healthy'] and quiet['connected']
    assert quiet['last_live_event_origin']=='receipt' and quiet['confirmation_age_seconds']==10
    assert liveness(phases(event=30),now)['state']=='receiving'
    assert liveness(phases(pending=3),now)['state']=='capture_backlogged'
    assert liveness(phases(refreshed=600),now)['state']=='unconfirmed'
    assert liveness(phases(connection='retry_wait'),now)['state']=='not_connected'
    assert liveness(phases(connection='blocked'),now)['state']=='blocked'
    assert liveness(phases(running=False,capture='not_running'),now)['state']=='collector_not_running'
    assert liveness({},now)['state']=='collector_not_running'
    for state in ('unconfirmed','not_connected','blocked'):
        assert not liveness(phases(**({'refreshed':600} if state=='unconfirmed' else
            {'connection':'retry_wait' if state=='not_connected' else state})),now)['connected']


def test_status_cache_remains_scoped_and_reports_age(seeded):
    s,key,other=seeded
    first=s.get_sync_status(key,refresh=False)
    again=s.get_sync_status(key,refresh=False)
    assert first['cache']['hit'] is False
    assert again['cache']['hit'] is True
    assert again['cache']['max_age_seconds']==10
    assert {r['account'] for r in s.get_sync_status(other)['sources']}=={'telegram-demo'}
    s.set_access('agent-a',accounts=['telegram-demo'])
    restricted=s.get_sync_status(key,refresh=False)
    assert restricted['cache']['hit'] is False
    assert {r['account'] for r in restricted['sources']}=={'telegram-demo'}
