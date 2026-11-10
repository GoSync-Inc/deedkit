"""Connection guidance remains useful without extra discovery hops."""
from starlette.testclient import TestClient

from test_android import setup
from test_module_selection import call


# spec: ANYM-953
def test_capabilities_explains_module_connections_for_frequent_work(tmp_path):
    app, _ = setup(tmp_path)
    with TestClient(app) as client:
        result = call(client, 'platform_capabilities', {}, path='/mcp/compact')['structuredContent']
        guidance = result['connection_guidance']
        assert guidance['combined_example'] == '/mcp/modules/android,messaging'
        assert 'same key' in guidance['frequent_work']
        assert '/mcp/compact' in guidance['occasional_work']
