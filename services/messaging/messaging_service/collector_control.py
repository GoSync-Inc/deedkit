"""Owner-only resume of a connection block, clear of a history-gap flag and a read-only failed-media inventory.

No pairing or message sends."""
import argparse
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .identifiers import ACCOUNT_RE, WA_CHAT_RE
from .enrichment_providers import private_text


def resume(c,account):
    if not isinstance(account,str) or not ACCOUNT_RE.fullmatch(account):
        raise ValueError('invalid_account')
    row=c.execute('SELECT operations,clock_timestamp() AS observed FROM collector_health '
        'WHERE account=%s FOR UPDATE',(account,)).fetchone()
    if not row or row['operations'].get('connection',{}).get('state')!='blocked':
        raise ValueError('connection_not_blocked')
    value=row['operations'];now=row['observed'].isoformat()
    value['connection']={'state':'stopped','reason_code':'operator_resumed','next_action':None,
        'retry_at':None,'retry_attempts':0,'changed_at':now,'observed_at':now}
    c.execute('UPDATE collector_health SET operations=%s,updated_at=clock_timestamp() WHERE account=%s',
        (Jsonb(value),account))


def clear_gap(c,account,chat):
    """ANYM-700: one UPDATE on one checkpoint row. A judgement from the archive, not a continuity claim."""
    if not isinstance(account,str) or not ACCOUNT_RE.fullmatch(account):
        raise ValueError('invalid_account')
    if not isinstance(chat,str) or not WA_CHAT_RE.fullmatch(chat):
        raise ValueError('invalid_chat')
    c.execute("SET LOCAL lock_timeout='2s'");c.execute("SET LOCAL statement_timeout='5s'")
    if c.execute("""UPDATE whatsapp_checkpoints SET progress=(progress-'gap')||jsonb_build_object(
        'gap_cleared_at',extract(epoch FROM clock_timestamp()),'gap_cleared_by','operator')
        WHERE account=%s AND provider_chat=%s AND progress->'gap'='true'::jsonb RETURNING 1 AS cleared""",
        (account,chat)).fetchone():
        return 'gap_cleared'
    row=c.execute('SELECT progress FROM whatsapp_checkpoints WHERE account=%s AND provider_chat=%s',
        (account,chat)).fetchone()
    if not row:raise ValueError('chat_not_found')
    if row['progress'].get('gap_cleared_at') is not None:return 'gap_already_cleared'
    raise ValueError('gap_not_set')


def run_clear_gap(dsn_file,account,chat):
    try:
        with psycopg.connect(private_text(dsn_file).strip(),row_factory=dict_row,connect_timeout=10) as c:
            result=clear_gap(c,account,chat)
    except ValueError as e:
        code=str(e)
        print(code if code in ('invalid_account','invalid_chat','chat_not_found','gap_not_set') else 'clear_gap_failed')
        raise SystemExit(1) from None
    except (OSError,psycopg.Error):
        print('clear_gap_failed')
        raise SystemExit(1) from None
    # After the connection context: only a committed clear reports success.
    print(result)


def run_media_inventory(dsn_file,account,config):
    """ANYM-815: grouped counts of the account's failed WhatsApp jobs; writes nothing, asks no provider."""
    from .media_inventory import run_inventory
    try:result=run_inventory(dsn_file,account,config)
    except ValueError as e:
        print('invalid_account' if str(e)=='invalid_account' else 'media_inventory_failed')
        raise SystemExit(1) from None
    except (OSError,psycopg.Error):
        print('media_inventory_failed')
        raise SystemExit(1) from None
    print(json.dumps(result,separators=(',',':'),sort_keys=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file',type=Path,required=True)
    parser.add_argument('--account',required=True)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--resume',action='store_true')
    action.add_argument('--clear-gap',metavar='CHAT')
    action.add_argument('--media-inventory',action='store_true')
    parser.add_argument('--collector-config',type=Path,
        help='the account\'s native WhatsApp collector config: adds media_key held/none from its capture journal')
    args=parser.parse_args()
    if args.media_inventory:
        run_media_inventory(args.dsn_file,args.account,args.collector_config)
        return
    if args.clear_gap is not None:
        run_clear_gap(args.dsn_file,args.account,args.clear_gap)
        return
    with psycopg.connect(private_text(args.dsn_file).strip(),row_factory=dict_row) as c:resume(c,args.account)
    print('connection_resume_recorded')


if __name__=='__main__':main()
