# Derived from Yiming Han, MIT; see LICENSE.award and SOURCES.json.
import urllib.parse
from .tool_types import EvidenceItem

def _contains_chinese(text: str) -> bool:
    return any('\u4E00' <= char <= '\u9FFF' for char in text)


def _serper_evidence_items(organic: list) -> list[EvidenceItem]:
    evidence = []
    for item in organic:
        title = str(item.get("title", "")).strip()
        snippet = str(item.get("snippet", "")).strip()
        text = "\n".join(part for part in (title, snippet) if part)
        if text:
            evidence.append(
                EvidenceItem(
                    text=text,
                    source_url=str(item.get("link", "")),
                    kind="lead",
                    quality=0.45 if snippet else 0.3,
                    metadata={"provider": "serper"},
                )
            )
    return evidence


def _iqs_evidence_items(page_items: list) -> list[EvidenceItem]:
    evidence = []
    for item in page_items:
        title = str(item.get("title", "")).strip()
        snippet = str(item.get("snippet") or item.get("htmlSnippet") or "").strip()
        text = "\n".join(part for part in (title, snippet) if part)
        if text:
            evidence.append(
                EvidenceItem(
                    text=text,
                    source_url=str(item.get("link", "")),
                    kind="lead",
                    quality=0.45 if snippet else 0.3,
                    metadata={"provider": "iqs"},
                )
            )
    return evidence


def _format_serper_results(query: str, data: dict) -> str:
    """Format Serper JSON response into the same style as IQS results."""
    organic = data.get("organic", [])
    if not organic:
        return f"No results found for '{query}'. Try with a more general query."

    web_snippets = []
    for idx, item in enumerate(organic, 1):
        title = item.get("title", "Untitled")
        link = item.get("link", "")
        snippet = item.get("snippet", "")
        date = item.get("date", "")

        date_str = ""
        if date:
            date_str = f"\nDate published: {date}"

        source_str = ""
        if link:
            try:
                hostname = urllib.parse.urlparse(link).hostname or ""
                if hostname:
                    source_str = f"\nSource: {hostname}"
            except Exception:
                pass

        snippet_text = ""
        if snippet:
            snippet_text = "\n" + snippet

        entry = f"{idx}. [{title}]({link}){date_str}{source_str}{snippet_text}"
        web_snippets.append(entry)

    content = (
        f"A search for '{query}' found {len(web_snippets)} results:\n\n"
        f"## Web Results\n"
        + "\n\n".join(web_snippets)
    )
    return content


def _format_search_results(query: str, page_items: list) -> str:
    """Format IQS search results in DeepResearch style."""
    if not page_items:
        return f"No results found for '{query}'. Try with a more general query."

    web_snippets = []
    for idx, item in enumerate(page_items, 1):
        title = item.get("title", "Untitled")
        link = item.get("link", "")
        snippet = item.get("snippet", "")
        html_snippet = item.get("htmlSnippet", "")
        publish_time = item.get("publishTime", "")
        hostname = item.get("hostname", "")

        date_str = ""
        if publish_time:
            date_str = f"\nDate published: {publish_time}"

        source_str = ""
        if hostname:
            source_str = f"\nSource: {hostname}"

        snippet_text = snippet if snippet else html_snippet
        if snippet_text:
            snippet_text = "\n" + snippet_text

        entry = f"{idx}. [{title}]({link}){date_str}{source_str}{snippet_text}"
        web_snippets.append(entry)

    content = (
        f"A search for '{query}' found {len(web_snippets)} results:\n\n"
        f"## Web Results\n"
        + "\n\n".join(web_snippets)
    )
    return content
