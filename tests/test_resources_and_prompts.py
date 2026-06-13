"""Tests for MCP Resources and Prompts."""

import asyncio

import pytest

from zotero_mcp.server import mcp


@pytest.fixture
def fake_zot(monkeypatch):
    """Patch get_zotero_client to return a stub."""

    class FakeRecentZotero:
        library_id = "99999"
        library_type = "user"

        def __init__(self):
            self._params = {}

        def add_parameters(self, **kwargs):
            self._params.update(kwargs)

        def items(self, **kwargs):
            return [
                {
                    "key": "AAAA1111",
                    "data": {"title": "Recent Paper", "itemType": "journalArticle", "date": "2025"},
                }
            ]

        def collections(self, **kwargs):
            return [
                {"key": "COL1", "data": {"name": "Research", "parentCollection": False}},
                {"key": "COL2", "data": {"name": "Drafts", "parentCollection": "COL1"}},
            ]

        def tags(self):
            return [{"tag": "ml"}, {"tag": "nlp"}, {"tag": "ml"}]

        def everything(self, _result):
            return self.tags()

    fake = FakeRecentZotero()
    monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: fake)
    monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: False)
    return fake


class TestResources:
    def test_resources_registered(self):
        resources = asyncio.run(mcp.list_resources())
        uris = {str(r.uri) for r in resources}
        assert "zotero://library/info" in uris
        assert "zotero://collections" in uris
        assert "zotero://tags" in uris
        assert "zotero://recent" in uris

    def test_resource_templates_registered(self):
        templates = asyncio.run(mcp.list_resource_templates())
        uris = {str(t.uri_template) for t in templates}
        assert "zotero://item/{item_key}" in uris
        assert "zotero://item/{item_key}/children" in uris
        assert "zotero://collection/{collection_key}/items" in uris
        assert "zotero://tag/{tag_name}/items" in uris

    @pytest.mark.asyncio
    async def test_library_info_resource(self, fake_zot):
        from zotero_mcp.resources import library_info

        result = await library_info()
        assert result["library_id"] == "99999"
        assert result["library_type"] == "user"
        assert result["local_mode"] is False

    @pytest.mark.asyncio
    async def test_collections_resource(self, fake_zot):
        from zotero_mcp.resources import collections_list

        result = await collections_list()
        assert len(result) == 2
        assert result[0]["key"] == "COL1"
        assert result[0]["name"] == "Research"
        assert result[1]["parent"] == "COL1"

    @pytest.mark.asyncio
    async def test_tags_resource_dedupes(self, fake_zot):
        from zotero_mcp.resources import tags_list

        result = await tags_list()
        assert result == ["ml", "nlp"]

    @pytest.mark.asyncio
    async def test_recent_resource(self, fake_zot):
        from zotero_mcp.resources import recent_items

        result = await recent_items()
        assert len(result) == 1
        assert result[0]["key"] == "AAAA1111"
        assert result[0]["title"] == "Recent Paper"


class TestPrompts:
    def test_prompts_registered(self):
        prompts = asyncio.run(mcp.list_prompts())
        names = {p.name for p in prompts}
        assert {"summarize_paper", "compare_papers", "literature_review", "annotated_bibliography", "find_relevant_papers", "prepare_citation_context"} <= names

    def test_summarize_paper_mentions_metadata_first(self):
        from zotero_mcp.prompts import summarize_paper

        messages = summarize_paper(item_key="ABCD1234")
        text = messages[0].content.text
        assert "ABCD1234" in text
        assert "zotero_get_item_metadata" in text
        assert "zotero_get_item_fulltext" in text

    def test_compare_papers_includes_keys(self):
        from zotero_mcp.prompts import compare_papers

        messages = compare_papers(item_keys="AAA, BBB")
        text = messages[0].content.text
        assert "AAA" in text and "BBB" in text
        assert "zotero_get_item_metadata" in text

    def test_literature_review_uses_search_tools(self):
        from zotero_mcp.prompts import literature_review

        messages = literature_review(topic="transformer attention")
        text = messages[0].content.text
        assert "transformer attention" in text
        assert "zotero_search_items" in text
        assert "zotero_semantic_search" in text


    def test_find_relevant_papers_uses_semantic_search(self):
        from zotero_mcp.prompts import find_relevant_papers

        messages = find_relevant_papers(topic="climate adaptation", max_results=5)
        text = messages[0].content.text
        assert "climate adaptation" in text
        assert "zotero_semantic_search" in text
        assert "zotero://item/{item_key}" in text

    def test_prepare_citation_context_uses_item_resources(self):
        from zotero_mcp.prompts import prepare_citation_context

        messages = prepare_citation_context(item_keys="AAAA1111, BBBB2222")
        text = messages[0].content.text
        assert "AAAA1111" in text and "BBBB2222" in text
        assert "zotero_get_item_metadata" in text
        assert "zotero://item/{item_key}" in text

    def test_annotated_bibliography_uses_collection_tools(self):
        from zotero_mcp.prompts import annotated_bibliography

        messages = annotated_bibliography(collection="NLP Papers")
        text = messages[0].content.text
        assert "NLP Papers" in text
        assert "zotero_search_collections" in text
        assert "zotero_get_collection_items" in text
