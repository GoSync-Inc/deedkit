import importlib
import importlib.util
import json

import httpx
import pytest


async def test_http_archive_client_uses_only_bounded_read_operations():
    assert importlib.util.find_spec("anymsg") is not None, "Scoped archive client is not implemented"
    requests=[]
    def handler(request):
        requests.append((request.url.path,json.loads(request.content),request.headers.get("Authorization")))
        if request.url.path.endswith('/get_context'):
            return httpx.Response(200,json={"message":{"id":"m1","body":"selected","revision":2},"context":[{"body":"PRIVATE_NEIGHBOR"}]})
        return httpx.Response(200,json={"items":[],"version":"fixture"})
    cls=importlib.import_module("anymsg").AnyMsg
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        archive=cls("https://archive.example/prefix","fake-token",http=http)
        await archive.capabilities()
        await archive.search("Доставка","wa1","vendor")
        row=await archive.message("m1")
        assert row["body"] == "selected" and "PRIVATE_NEIGHBOR" not in json.dumps(row)
        assert requests[-1][1] == {"message_id":"m1","before_count":0,"after_count":0,"detail":"full"}
        assert all(r[0].startswith('/prefix/v1/') for r in requests)
        assert requests[1][1]["limit"] == 12
        with pytest.raises(ValueError):
            await archive.call("ack_updates",{})
        with pytest.raises(ValueError):
            cls("http://remote.example","token")


async def test_deleted_archive_message_cannot_be_prepared_as_a_fact():
    cls=importlib.import_module('anymsg').AnyMsg
    def handler(request):
        return httpx.Response(200,json={'message':{'id':'deleted-1','body':'stale','deleted':True}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        archive=cls('https://archive.example','fixture',http=http)
        with pytest.raises(ValueError,match='archive_message_missing'):
            await archive.message('deleted-1')


# spec: ANYM-270
@pytest.mark.parametrize("old_api,status",[(False,200),(True,200),(False,503)])
async def test_capabilities_asks_for_the_full_payload_by_name(old_api,status):
    bodies=[]
    def handler(request):
        body=json.loads(request.content);bodies.append(body)
        if status!=200:return httpx.Response(status,json={"error":"backend_unavailable"})
        if old_api and body:return httpx.Response(400,json={"error":"invalid_parameters","fields":["detail"]})
        return httpx.Response(200,json={"version":"fixture","coverage":"synthetic_fixture_only","accounts":[],"synthetic":True})
    cls=importlib.import_module("anymsg").AnyMsg
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        archive=cls("https://archive.example","fake-token",http=http)
        if status!=200:
            with pytest.raises(ValueError):await archive.capabilities()
        else:
            assert await archive.capabilities()=={"version":"fixture","coverage":"synthetic_fixture_only","accounts":[],"synthetic":True}
    # An old API (400) gets one more request with no arguments; an outage is not retried.
    assert bodies==([{"detail":"full"},{}] if old_api else [{"detail":"full"}])
