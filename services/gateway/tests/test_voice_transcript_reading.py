from agent_gateway.catalog import CATALOG, arguments


# spec: ANYM-453
def test_gateway_routes_an_omitted_content_to_the_transcript():
    tool = CATALOG["messages_get_file_text"]
    routed = arguments(tool, {"attachment_id": "synthetic-voice"})
    # Messaging resolves an omitted content: a voice note's transcript, else extracted text.
    assert "content" not in routed
    assert routed["offset"] == 0 and routed["limit"] == 4000
    assert arguments(tool, {"attachment_id": "synthetic-voice", "content": "extracted"})["content"] == "extracted"
    assert "transcript" in tool["description"]
    assert "excerpt_source" in CATALOG["messages_search_files"]["description"]


# spec: ANYM-851
def test_gateway_offers_and_routes_the_structured_content():
    tool = CATALOG["messages_get_file_text"]
    assert tool["inputSchema"]["properties"]["content"]["enum"] == ["extracted", "enriched", "structured"]
    assert arguments(tool, {"attachment_id": "synthetic-pdf", "content": "structured"})["content"] == "structured"
    assert "content=structured" in tool["description"] and "structured_text" in tool["description"]
