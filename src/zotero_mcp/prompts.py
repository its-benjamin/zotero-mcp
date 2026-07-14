"""MCP Prompts for common Zotero research workflows.

Prompts are reusable templates that guide AI assistants through
multi-step research tasks using the available tools.

Importing this module registers each ``@mcp.prompt`` with the FastMCP app (a
side effect, mirroring the tool modules).
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
            "3. For a deeper summary, call zotero_get_pdf_outline to find key sections, "
            "then use zotero_extract_pdf_pages to read specific sections.\n"
            "4. Only call zotero_get_item_fulltext if you need the entire paper "
            "(it returns 10K+ tokens).\n"
            "5. Provide a structured summary: main contribution, methods, key findings, "
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
            "6. Identify gaps in the literature and suggest future directions.\n"
            "7. Optionally, call zotero_generate_bibliography with the item keys "
            "and a citation style (e.g., 'apa') to produce a formatted reference list."
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
            "3. For each item, call zotero_get_item_metadata (format='bibtex') for the citation, "
            "or use zotero_generate_bibliography for all items at once with a CSL style.\n"
            "4. Use the abstract from metadata to write a 2-3 sentence annotation per entry.\n"
            "5. Order entries alphabetically by first author and format as a proper "
            "annotated bibliography."
        )
    ]


@mcp.prompt
def find_relevant_papers(topic: str, max_results: int = 10) -> list[Message]:
    """Find papers relevant to a research topic without reading full PDFs first."""
    return [
        Message(
            f"Please find up to {max_results} Zotero library items relevant to: {topic}\n\n"
            "Steps:\n"
            "1. Start with zotero_semantic_search for conceptual matches.\n"
            "2. If semantic search is unavailable or sparse, use zotero_search_items with short keyword queries.\n"
            "3. Read zotero://item/{item_key} resources or call zotero_get_item_metadata for top candidates.\n"
            "4. Rank results by relevance and explain why each item matches.\n"
            "5. Avoid zotero_get_item_fulltext unless metadata and abstracts are insufficient."
        )
    ]


@mcp.prompt
def prepare_citation_context(item_keys: str) -> list[Message]:
    """Prepare compact citation context for drafting with selected Zotero items."""
    return [
        Message(
            f"Please prepare drafting context for these Zotero item keys: {item_keys}\n\n"
            "Steps:\n"
            "1. Read metadata for each item using zotero_get_item_metadata or zotero://item/{item_key}.\n"
            "2. Extract citation labels as Author (Year), title, venue, DOI/URL, and item key.\n"
            "3. Summarize each item in 1-2 sentences from metadata/abstract only.\n"
            "4. Group items by theme and note likely use cases in a manuscript.\n"
            "5. Return a concise citation-ready context block."
        )
    ]


@mcp.prompt
def check_retractions(collection: str | None = None) -> list[Message]:
    """Check your Zotero library for retracted papers."""
    scope = f" in collection '{collection}'" if collection else ""
    return [
        Message(
            f"Please check for retracted papers{scope} in my Zotero library.\n\n"
            "Steps:\n"
            "1. If a collection is specified, call zotero_search_collections to find the key, "
            "then zotero_get_collection_items to list items.\n"
            "2. Otherwise, call zotero_get_recent to get recent items.\n"
            "3. For each item with a DOI, call scite_check_retractions to check for retractions.\n"
            "4. Report any retracted items with their titles, DOIs, and retraction status.\n"
            "5. If no retractions found, confirm the library is clean."
        )
    ]


@mcp.prompt
def add_papers_by_identifier(identifiers: str) -> list[Message]:
    """Add papers to your Zotero library by DOI, ISBN, arXiv ID, or URL."""
    return [
        Message(
            f"Please add these papers to my Zotero library: {identifiers}\n\n"
            "Steps:\n"
            "1. Parse the identifiers. DOIs start with '10.', ISBNs are 10-13 digits, "
            "arXiv IDs contain '.', and URLs start with 'http'.\n"
            "2. For each identifier, call the appropriate tool:\n"
            "   - DOI: zotero_add_by_doi\n"
            "   - ISBN: zotero_add_by_isbn\n"
            "   - URL: zotero_add_by_url\n"
            "3. Report which items were added successfully and which failed.\n"
            "4. For successfully added items, show the item key and title."
        )
    ]


@mcp.prompt
def read_paper_section(item_key: str, section: str) -> list[Message]:
    """Read a specific section of a paper by extracting relevant PDF pages."""
    return [
        Message(
            f"Please read the '{section}' section of paper with item key `{item_key}`.\n\n"
            "Steps:\n"
            "1. Call zotero_get_pdf_outline to get the table of contents.\n"
            "2. Identify which page range contains the '{section}' section.\n"
            "3. Call zotero_extract_pdf_pages with the relevant page range.\n"
            "4. If the section is not in the outline, try zotero_get_item_fulltext "
            "and search for the section heading.\n"
            "5. Summarize the section content."
        )
    ]


@mcp.prompt
def search_my_notes(query: str) -> list[Message]:
    """Search through your Zotero notes and annotations."""
    return [
        Message(
            f"Please search my Zotero notes for: {query}\n\n"
            "Steps:\n"
            "1. Call zotero_search_notes with the query.\n"
            "2. For each result, show the note content and which item it belongs to.\n"
            "3. If results are sparse, try zotero_semantic_search as a broader search.\n"
            "4. Group results by topic or source item.\n"
            "5. Highlight the most relevant passages."
        )
    ]


@mcp.prompt
def write_related_work(topic: str, collection: str | None = None) -> list[Message]:
    """Draft a Related Work section for a research paper."""
    return [
        Message(
            f"Please draft a Related Work section on: {topic}\n\n"
            "Steps:\n"
            "1. Call zotero_semantic_search to find relevant papers in my library.\n"
            "2. If a collection is specified, also call zotero_get_collection_items "
            "for additional context.\n"
            "3. For the top 10-15 results, call zotero_get_item_metadata to get abstracts.\n"
            "4. Group papers by sub-theme (e.g., 'approaches using X', 'methods for Y').\n"
            "5. Draft 3-5 paragraphs that:\n"
            "   - Introduce the research area\n"
            "   - Discuss key approaches and their trade-offs\n"
            "   - Identify gaps that your work addresses\n"
            "   - Use inline citations as (Author, Year) with item keys for reference.\n"
            "6. End with a transition paragraph connecting to your contribution."
        )
    ]


@mcp.prompt
def organize_library(collection: str | None = None) -> list[Message]:
    """Help organize and clean up your Zotero library."""
    return [
        Message(
            f"Please help organize my Zotero library{f' (collection: {collection})' if collection else ''}.\n\n"
            "Steps:\n"
            "1. If a collection is specified, call zotero_get_collection_items to list items.\n"
            "2. Otherwise, call zotero_get_recent to see recent items.\n"
            "3. Call zotero_find_duplicates to identify potential duplicates.\n"
            "4. For each duplicate set, recommend which to keep based on metadata completeness.\n"
            "5. Suggest tag improvements:\n"
            "   - Call zotero_get_tags to see current tag usage\n"
            "   - Identify inconsistent or overly specific tags\n"
            "   - Recommend tag consolidations using zotero_rename_tag\n"
            "6. Suggest collection organization if items are uncategorized."
        )
    ]


@mcp.prompt
def write_introduction(topic: str, item_keys: str | None = None) -> list[Message]:
    """Draft an Introduction section for a research paper."""
    return [
        Message(
            f"Please draft an Introduction section on: {topic}\n\n"
            "Steps:\n"
            "1. If item keys are provided, read metadata for those items to understand the research context.\n"
            "2. Otherwise, call zotero_semantic_search to find relevant background papers.\n"
            "3. For the top 5-8 results, call zotero_get_item_metadata to get abstracts.\n"
            "4. Draft 3-5 paragraphs that:\n"
            "   - Introduce the research area and its importance\n"
            "   - Discuss the current state of the art\n"
            "   - Identify the gap or limitation in existing work\n"
            "   - State your contribution and how it addresses the gap\n"
            "5. Use inline citations as (Author, Year) with item keys for reference.\n"
            "6. End with a brief outline of the paper structure."
        )
    ]


@mcp.prompt
def tag_audit(collection: str | None = None) -> list[Message]:
    """Audit and consolidate tags in your Zotero library."""
    return [
        Message(
            f"Please audit tags in my Zotero library{f' (collection: {collection})' if collection else ''}.\n\n"
            "Steps:\n"
            "1. Call zotero_get_tags to get all tags with usage counts.\n"
            "2. Identify issues:\n"
            "   - Case inconsistencies (e.g., 'ML' vs 'ml' vs 'Ml')\n"
            "   - Duplicates (e.g., 'machine learning' vs 'machine-learning' vs 'machinelearning')\n"
            "   - Overly specific tags (e.g., 'deep learning CNN architecture')\n"
            "   - Unused tags (tags with 0 items)\n"
            "3. For each issue, recommend a canonical tag and suggest merges.\n"
            "4. Use zotero_merge_tags to consolidate duplicates.\n"
            "5. Use zotero_rename_tag to fix case inconsistencies.\n"
            "6. Report the changes made and remaining issues."
        )
    ]


@mcp.prompt
def extract_key_findings(item_key: str) -> list[Message]:
    """Extract structured key findings from a paper for evidence tables."""
    return [
        Message(
            f"Please extract key findings from paper with item key `{item_key}`.\n\n"
            "Steps:\n"
            "1. Call zotero_get_item_metadata to get title, authors, abstract, and date.\n"
            "2. Call zotero_get_pdf_outline to identify key sections (Methods, Results, Discussion).\n"
            "3. Use zotero_extract_pdf_pages to read the Results and Discussion sections.\n"
            "4. Extract and structure the findings as:\n"
            "   - **Claim**: What the paper asserts\n"
            "   - **Evidence**: Data, experiments, or analysis supporting the claim\n"
            "   - **Strength**: How strong the evidence is (strong/moderate/weak)\n"
            "   - **Relevance**: How this relates to the research question\n"
            "5. Present as a structured evidence table."
        )
    ]


@mcp.prompt
def export_for_latex(item_keys: str, style: str = "apa") -> list[Message]:
    """Export items in BibTeX format for LaTeX documents."""
    return [
        Message(
            f"Please export these items for LaTeX: {item_keys}\n\n"
            "Steps:\n"
            "1. Call zotero_export_items with format='bibtex' for the given keys.\n"
            "2. Check for duplicate citation keys and resolve conflicts.\n"
            "3. Verify that all required fields are present for each entry.\n"
            "4. Format the output as a valid .bib file.\n"
            "5. Report any items that had missing or incomplete metadata."
        )
    ]


# ---------------------------------------------------------------------------
# Upstream-unique prompts (adapted to Message format)
# ---------------------------------------------------------------------------


@mcp.prompt(
    name="zotero_synthesize_my_notes",
    description="Synthesize your own highlights and notes across a topic or collection.",
)
def synthesize_my_notes(scope: str) -> list[Message]:
    """Turn the user's annotations into a themed synthesis."""
    return [
        Message(
            f"Synthesize my own reading notes and highlights for: **{scope}**.\n\n"
            "1. Call `zotero_search_notes` (and `zotero_get_annotations` if available) "
            f"scoped to '{scope}' when it names a collection or tag; otherwise filter library-wide.\n"
            "2. Identify cross-cutting THEMES and TENSIONS across sources.\n"
            "3. Produce a synthesis organized by theme, quoting highlights and attributing each to its paper.\n"
            "4. End with the 3-5 most important takeaways and any gaps where you have no notes yet.\n\n"
            "Use only actual annotations as evidence; do not invent claims."
        )
    ]


@mcp.prompt(
    name="zotero_find_contradicting_evidence",
    description="Stress-test a claim by finding supporting and contradicting papers.",
)
def find_contradicting_evidence(claim: str) -> list[Message]:
    """Search the library for evidence for and against *claim*."""
    return [
        Message(
            f"Stress-test this claim against my Zotero library: **{claim}**\n\n"
            "1. `zotero_semantic_search(query=<the claim>, limit=10)` — find papers directly on this topic.\n"
            "2. `zotero_semantic_search` again with an INVERTED / skeptical phrasing of "
            "the claim (e.g. limitations, null results, criticisms) to surface disconfirming work.\n"
            "3. Sort results into SUPPORTS / CONTRADICTS / MIXED, quoting matched passages and citing item keys.\n"
            "4. Weigh the evidence and state how well-supported the claim is overall.\n\n"
            "Be even-handed — actively look for the strongest contradicting evidence."
        )
    ]


@mcp.prompt(
    name="zotero_expand_from_paper",
    description="Snowball a reading list outward from one seed paper via its citation graph.",
)
def expand_from_paper(identifier: str) -> list[Message]:
    """Grow a reading list outward from a seed paper."""
    return [
        Message(
            f"Expand my reading list outward from this seed paper: **{identifier}**\n\n"
            f"1. Look up the item (`zotero_get_item_metadata` / DOI search) for `{identifier}`.\n"
            "2. Use related-paper tools if available, or semantic search on title/abstract themes.\n"
            "3. Rank related papers by relevance; highlight ones not yet in the library.\n"
            "4. For top not-in-library papers, offer to add them with `zotero_add_by_doi`.\n"
            "5. Summarize how the seed paper sits in its neighborhood: foundations and follow-ups."
        )
    ]
