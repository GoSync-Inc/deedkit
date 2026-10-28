"""Owner-only resume of a diagnosed connection block. No pairing or message sends."""
import argparse
from pathlib import Path
import re

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .enrichment_providers import private_text


def resume(c,account):
    if not isinstance(account,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',account):
        raise ValueError('invalid_account')
    row=c.execute('SELECT operations,clock_timestamp() AS observed FROM collector_health '
        'WHERE account=%s FOR UPDATE',(account,)).fetchone()
    if not row or row['operations'].get('connection',{}).get('state')!='blocked':
        raise ValueError('connection_not_blocked')
    value=row['operations'];now=row['observed'].isoformat()
    # The connect count survives a resume (ANYM-535 source.health).
    count=value['connection'].get('connect_count')
    value['connection']={'state':'stopped','reason_code':'operator_resumed','next_action':None,
        'retry_at':None,'retry_attempts':0,'changed_at':now,'observed_at':now,
        **({'connect_count':count} if count is not None else {})}
    c.execute('UPDATE collector_health SET operations=%s,updated_at=clock_timestamp() WHERE account=%s',
        (Jsonb(value),account))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file',type=Path,required=True)
    parser.add_argument('--account',required=True)
    parser.add_argument('--resume',action='store_true',required=True)
    args=parser.parse_args()
    with psycopg.connect(private_text(args.dsn_file).strip(),row_factory=dict_row) as c:resume(c,args.account)
    print('connection_resume_recorded')


if __name__=='__main__':main()
