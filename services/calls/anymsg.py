"""Read-only, bounded REST adapter. Messenger credentials never reach the model."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

# The person lookup behind calls_call (ANYM-548): messaging down or slow fails the call within this bound.
RESOLVE_TIMEOUT_S=3


class ArchiveArgumentsRejected(ValueError):
    """A messaging API from before an argument answered 400 to it (ANYM-270)."""


class AnyMsg:
    allowed = {"capabilities", "search", "get_context", "resolve_target"}

    def __init__(self, base_url, token, http=None):
        u=urlsplit(base_url)
        if u.scheme != "https" or not u.hostname or u.username or u.query or u.fragment:
            raise ValueError("invalid_archive_url")
        self.base=base_url.rstrip("/")
        self._token=token
        self.http=http or httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False)
        self.owned=http is None

    @classmethod
    def from_config(cls, path):
        cfg=json.loads(Path(path).read_text())
        token=Path(cfg["token_file"]).read_text().strip()
        return cls(cfg["base_url"],token)

    async def call(self, operation, arguments):
        if operation not in self.allowed:
            raise ValueError("archive_operation_not_allowed")
        r=await self.http.post(self.base+"/v1/"+operation,json=arguments,
                               headers={"Authorization":"Bearer "+self._token})
        if r.status_code==400 and operation=="capabilities" and arguments:
            raise ArchiveArgumentsRejected("archive_read_failed")
        if r.status_code != 200 or len(r.content)>1024*1024:
            raise ValueError("archive_read_failed")
        v=r.json()
        if not isinstance(v,dict) or "error" in v:
            raise ValueError("archive_read_failed")
        return v

    async def capabilities(self):
        # Full payload by name (ANYM-270); a messaging API from before `detail` rejects it.
        try:v=await self.call("capabilities",{"detail":"full"})
        except ArchiveArgumentsRejected:v=await self.call("capabilities",{})
        return {k:v.get(k) for k in ("version","coverage","accounts","synthetic")}

    async def search(self,query,account="",chat=""):
        return await self.call("search",{"query":query,"account":account,"chat":chat,
                                          "limit":12,"detail":"compact","order":"desc"})

    async def message(self,identity):
        v=await self.call("get_context",{"message_id":identity,"before_count":0,"after_count":0,"detail":"full"})
        row=v.get("message")
        if not isinstance(row,dict) or row.get("id") != identity or row.get('deleted'):
            raise ValueError("archive_message_missing")
        return row

    async def resolve_target(self,to,purpose,account=""):
        """(status, body) of ANYM-545's resolver: its refusals (selection_required, not_found) are answers, not failures.
        Sends only `to`, `purpose`, an asked account and the caller's key; a slow messaging fails in 3 s (ANYM-548)."""
        # One deadline for the whole exchange: httpx's own timeout restarts with every phase and chunk.
        async with asyncio.timeout(RESOLVE_TIMEOUT_S):
            r=await self.http.post(self.base+"/v1/resolve_target",json={"to":to,"purpose":purpose,**({"account":account} if account else {})},
                                   headers={"Authorization":"Bearer "+self._token},timeout=RESOLVE_TIMEOUT_S)
        if r.status_code not in (200,400,404,409) or len(r.content)>1024*1024:
            raise ValueError("archive_read_failed")
        v=r.json()
        if not isinstance(v,dict):
            raise ValueError("archive_read_failed")
        return r.status_code,v

    async def close(self):
        if self.owned:
            await self.http.aclose()


class UnconfiguredArchive:
    async def capabilities(self):
        return {"configured":False,"accounts":[],"coverage":"not_checked"}

    async def search(self,*args):
        raise ValueError("archive_not_configured")

    async def message(self,*args):
        raise ValueError("archive_not_configured")

    async def close(self):
        pass
