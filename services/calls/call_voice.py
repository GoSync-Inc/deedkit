"""Show or set the owner's call voice through the local owner API (ANYM-359); never prints the credential.

Inside the service container:
  python call_voice.py                                             # show
  python call_voice.py '{"provider":"grok_realtime","grok_voice":"rex"}'   # set
"""
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def call_voice(value=None):
    token = (Path(os.environ['VOICE_DATA_DIR']) / 'owner.token').read_text().strip()
    base = f"http://127.0.0.1:{int(os.environ.get('VOICE_PORT', '13842'))}"
    headers = {'Authorization': 'Bearer ' + token}
    if value is not None:
        # The owner API accepts writes only with the local interface headers.
        headers.update({'Content-Type': 'application/json', 'Origin': base, 'X-Voice-Service': '1'})
    request = Request(base + '/api/call-voice', headers=headers, method='GET' if value is None else 'POST',
                      data=None if value is None else json.dumps(value).encode())
    with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
        return json.loads(response.read())


if __name__ == '__main__':
    try:
        print(json.dumps(call_voice(json.loads(sys.argv[1]) if len(sys.argv) > 1 else None)))
    except HTTPError as error:
        raise SystemExit(f'call voice refused: HTTP {error.code}') from None
    except json.JSONDecodeError:
        raise SystemExit('call voice refused: the argument is not JSON') from None
