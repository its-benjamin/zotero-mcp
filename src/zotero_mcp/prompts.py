"""MCP Prompts for common Zotero research workflows.

Prompts are reusable templates that guide AI assistants through
multi-step research tasks using the available tools.
"""

from fastmcp.prompts import Message

from zotero_mcp._app import mcp


@mcp.prompt
def summarize_paper(item_key: str) -> list[Message]:
    """Summarize a paper from your Zotero library."""
    return [
        Message(
            f"Please summarize the paper with Zotero item key `{item_key}`.\n\n"
            "Steps:\n"
            "1. Call zotero_get_item_metadata to get title, authors, abstract, and date.\n"
            "2. If the abstract provides enough context, summarize from that.\n"
            "3. Only call zotero_get_item_fulltext if a deeper summary is needed "
            "(it returns 10K+ tokens).\n"
            "4. Provide a structured summary: main contribution, methods, key findings, "
            "and limitations."
        )
    ]


@mcp.prompt
def compare_papers(item_keys: str) -> list[Message]:
    """Compare multiple papers from your Zotero library."""
    return [
        Message(
            f"Please compare the papers with Zotero item keys: {item_keys}\n\n"
            "Steps:\n"
            "1. Call zotero_get_item_metadata for each paper to get titles, authors, "
            "abstracts, and dates.\n"
            "2. Identify shared themes, methodological differences, and contrasting findings.\n"
            "3. Only call zotero_get_item_fulltext for specific papers if abstracts are "
            "insufficient for comparison.\n"
            "4. Present a structured comparison: shared goals, different approaches, "
            "key agreements/disagreements, and relative strengths."
        )
    ]


@mcp.prompt
def literature_review(topic: str) -> list[Message]:
    """Conduct a literature review on a topic using your Zotero library."""
    return [
        Message(
            f"Please conduct a literature review on: {topic}\n\n"
            "Steps:\n"
            "1. Call zotero_search_items to find relevant papers by title/author.\n"
            "2. If available, call zotero_semantic_search for conceptual matches.\n"
            "3. Call zotero_get_item_metadata for the top results to get abstracts.\n"
            "4. Group papers by sub-theme or methodology.\n"
            "5. Synthesize findings into a coherent narrative with citations "
            "(Author, Year) referencing the item keys.\n"
            "6. Identify gaps in the literature and suggest future directions."
        )
    ]


@mcp.prompt
def annotated_bibliography(collection: str) -> list[Message]:
    """Generate an annotated bibliography from a Zotero collection."""
    return [
        Message(
            f"Please create an annotated bibliography from the collection: {collection}\n\n"
            "Steps:\n"
            "1. Call zotero_search_collections to find the collection key.\n"
            "2. Call zotero_get_collection_items to list all items.\n"
            "3. For each item, call zotero_get_item_metadata (format='bibtex') for the citation.\n"
            "4. Use the abstract from metadata to write a 2-3 sentence annotation per entry.\n"
            "5. Order entries alphabetically by first author and format as a proper "
            "annotated bibliography."
        )
    ]
