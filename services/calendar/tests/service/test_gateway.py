"""Real isolated calendar DB and HTTP transport through the provider-free gateway."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'tests/platform'))
from runtime_helpers import call, gateway, port, server
from family_calendar.service.api import create_app
from family_calendar.service.core import FamilyService
from tests.service.test_policy import SCOPES


def test_gateway_preserves_busy_only_missing_coverage_and_calendar_grants(household, tmp_path):
    household.grant('family', 'adult-a', 'cal-a', SCOPES, 'details')
    key = household.agent('agent', 'family', 'adult-a', SCOPES, ['cal-a'], 'busy_only')
    selected_port = port()
    base = f'http://127.0.0.1:{selected_port}'
    direct = FamilyService(household)
    tools = ['calendar_' + n for n in ['calendars', 'members', 'agenda', 'slots']]
    with server(create_app(household, port=selected_port), selected_port), gateway(tmp_path, {'calendar': (base, key)}, {'calendar': SCOPES}, tools) as (c, _, _):
        assert call(c, 'calendar_calendars', {}) == direct.calendars(key)
        assert call(c, 'calendar_members', {}) == direct.family(key)
        window = {'start': '2026-10-16T00:00:00Z', 'end': '2026-10-16T03:00:00Z', 'refresh': False}
        agenda = call(c, 'calendar_agenda', {**window, 'calendar_refs': ['cal-a']})
        assert agenda == direct.agenda(key, ['cal-a'], window['start'], window['end'])
        denied = c.post('/v1/calendar_agenda', json={**window, 'calendar_refs': ['foreign']})
        assert denied.status_code == 403
        slots = call(c, 'calendar_slots', {**window, 'member_ids': ['child'], 'duration_minutes': 30})
        assert slots == direct.slots(key, ['child'], window['start'], window['end'], duration_minutes=30)
        assert slots['availability'] != 'verified' and slots['verified_slots'] == []
