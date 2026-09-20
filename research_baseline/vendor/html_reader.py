# Derived from Yiming Han, MIT; see LICENSE.award and SOURCES.json.
import json
from dataclasses import dataclass
from bs4 import BeautifulSoup

@dataclass(slots=True)
class ExtractionResult:
    rational: str = ""
    evidence: str = ""
    summary: str = ""
    raw: str = ""
    parsed: bool = False

    def render(self) -> str:
        if self.parsed:
            return (
                f"Rational:\n{self.rational}\n\n"
                f"Evidence in page:\n{self.evidence}\n\n"
                f"Summary:\n{self.summary}"
            )
        return f"Extracted content:\n{self.raw}"


def extract_main_text(html: str) -> str:
    """Extract main text content from HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove unwanted elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "iframe", "noscript", "svg", "form"]):
        tag.decompose()

    # Try to find article content first
    article = soup.find("article")
    if article:
        text = article.get_text(separator="\n", strip=True)
    else:
        # Fall back to body
        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

    # Clean up: remove excessive blank lines
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    text = "\n".join(lines)

    return text


def _parse_extraction(raw: str) -> ExtractionResult:
    raw_clean = raw.strip()
    if raw_clean.startswith("```"):
        fenced_parts = raw_clean.split("```")
        raw_clean = fenced_parts[1] if len(fenced_parts) > 1 else raw_clean
        if raw_clean.startswith("json"):
            raw_clean = raw_clean[4:]
        raw_clean = raw_clean.strip()

    candidates = [raw_clean]
    left = raw_clean.find("{")
    right = raw_clean.rfind("}")
    if left != -1 and right != -1 and left < right:
        candidates.append(raw_clean[left : right + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            return ExtractionResult(
                rational=str(data.get("rational", "")).strip(),
                evidence=str(data.get("evidence", "")).strip(),
                summary=str(data.get("summary", "")).strip(),
                raw=raw,
                parsed=True,
            )
        except json.JSONDecodeError:
            continue
    return ExtractionResult(raw=raw, parsed=False)
