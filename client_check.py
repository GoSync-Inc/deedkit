"""Real network MCP check using the pinned official SDK; never prints credentials."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def check(url, token_file, consumer):
    token=token_file.read_text().strip()
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+token}) as http:
        async with Client(streamable_http_client(url,http_client=http)) as client:
            names=sorted(t.name for t in (await client.list_tools()).tools)
            async def call(name,arguments=None):
                result=await client.call_tool(name,arguments or {})
                if result.is_error: raise RuntimeError('Tool failed: '+name)
                if result.structured_content is None: raise RuntimeError('Missing structured output: '+name)
                return result.structured_content
            caps=await call('capabilities',{'detail':'full'})
            page=await call('search',{'query':'Маяк'})
            assert page['items'][0]['id']=='demo-tg-1'
            denied=await call('search',{'query':'PRIVATE_SCOPE_SENTINEL'})
            assert denied['items']==[]
            context=await call('get_context',{'message_id':'demo-tg-2'})
            assert context['attachments'][0]['id']=='demo-file-1'
            artifact=await call('get_attachment',{'attachment_id':'demo-file-1'})
            async with httpx.AsyncClient(follow_redirects=False,timeout=20) as files:
                response=await files.get(artifact['download_url'])
                response.raise_for_status()
                sha=hashlib.sha256(response.content).hexdigest()
                assert sha==artifact['sha256']
            first=await call('get_updates',{'consumer':consumer})
            repeated=await call('get_updates',{'consumer':consumer})
            assert first['items']==repeated['items']
            receipt=await call('ack_updates',{'consumer':consumer,'receipt':first['receipt'],
                                             'expected_version':first['ack_version']})
            after=await call('get_updates',{'consumer':consumer})
            assert not after['items'] and receipt['acked']
            return {'status':'PASS','client':'official-python-sdk-2.3.0','tools':names,
                'principal':caps['principal'],'synthetic':caps['synthetic'],'consumer':consumer,
                'messages_before_ack':len(first['items']),'messages_after_ack':len(after['items']),
                'file_bytes':len(response.content),'file_sha256':sha,
                'secret_urls_and_tokens_omitted':True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',required=True)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--consumer',default='check-'+secrets.token_hex(4))
    args=parser.parse_args()
    print(json.dumps(asyncio.run(check(args.url,args.token_file,args.consumer)),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
