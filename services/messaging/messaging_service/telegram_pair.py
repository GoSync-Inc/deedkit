"""Owner-operated terminal sign-in. OTP/password never go through MCP or a web endpoint."""
import asyncio
from contextlib import suppress
import getpass
import json
import os
from pathlib import Path
import secrets
import sys

from .archive import Archive
from .collector_state import private_session_directory,session_lock
from .store import Store
from . import session_claim
from .telegram_collector import configuration,TelethonProvider


def require_owner_terminal():
    if not sys.stdin.isatty() or not sys.stderr.isatty(): raise RuntimeError('owner_terminal_required')


async def pair_config(path):
    require_owner_terminal()  # Before parsing secrets, opening a session or connecting.
    cfg,auth=configuration(path)
    os.close(private_session_directory(cfg['session_file']))  # a shared directory is refused before any lock
    # ANYM-914: canonical host lock, session flock, claim, then the TelegramClient (it opens the session store).
    with session_claim.host_lock(cfg.get('account'),cfg.get('session_lock_directory')) as host,session_lock(cfg['session_file']):
        store=Store(Path(os.environ['DATABASE_URL_FILE']).read_text().strip(),
                    Path(os.environ['PROBE_FILES']),'http://collector.invalid')
        archive=Archive(store); owner='pair-'+secrets.token_hex(12); epoch=None; provider=None; tasks=[]; claim=None
        verified=False
        try:
            with store.pool.connection() as c:
                row=c.execute('SELECT platform,synthetic FROM accounts WHERE id=%s',(cfg['account'],)).fetchone()
                if not row or row['platform']!='telegram' or row['synthetic']: raise RuntimeError('live_account_required')
            epoch=await asyncio.to_thread(archive.acquire,cfg['account'],owner)
            # The owner is at the terminal: a refusal is reported at once, never waited out.
            claim=await asyncio.to_thread(session_claim.acquire,store.pool,host,'pair')
            provider=TelethonProvider(cfg,auth,receive_updates=False)
            async def answer(prompt):
                value=await asyncio.to_thread(getpass.getpass,prompt)
                # Recheck ownership before returning a secret to a network call.
                await asyncio.to_thread(archive.renew,cfg['account'],owner,epoch)
                return value
            async def authorize():
                await provider.client.start(phone=lambda:answer('Telegram phone (hidden): '),
                    code_callback=lambda:answer('Telegram login code (hidden): '),
                    password=lambda:answer('Telegram two-step password (hidden): '),max_attempts=3)
                me=await provider.client.get_me()
                if me.id!=auth['expected_user_id']: raise RuntimeError('telegram_identity_mismatch')
                await asyncio.to_thread(archive.renew,cfg['account'],owner,epoch)
            async def renew():
                while True:
                    await asyncio.sleep(15)
                    await asyncio.to_thread(archive.renew,cfg['account'],owner,epoch)
            auth_task=asyncio.create_task(authorize()); heartbeat=asyncio.create_task(renew())
            tasks=[auth_task,heartbeat,asyncio.create_task(session_claim.keep(store.pool,claim))]
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            if auth_task not in done: raise RuntimeError('collector_lease_lost')
            Path(cfg['session_file']).chmod(0o600)
            print(json.dumps({'status':'paired','collector_started':False}),flush=True)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if provider:
                try: await asyncio.wait_for(provider.disconnect(),15); verified=True
                except Exception: pass
            else: verified=True  # no client was built, so no session store was opened
            if claim is not None and verified: await asyncio.to_thread(session_claim.release,store.pool,claim)
            if epoch is not None:
                with suppress(Exception): await asyncio.to_thread(archive.release,cfg['account'],owner,epoch)
            store.close()


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args(); os.umask(0o077)
    try: asyncio.run(pair_config(args.config))
    except Exception as exc:
        safe={'owner_terminal_required','collector_disabled','session_handover_not_confirmed','private_session_required',
              'telegram_identity_mismatch','live_account_required','collector_already_owned',
              'session_already_owned','collector_lease_lost',*session_claim.CODES}
        print(json.dumps({'status':'not_paired','code':str(exc) if str(exc) in safe else 'pairing_failed'}),flush=True)
        raise SystemExit(1) from None


if __name__=='__main__': main()
