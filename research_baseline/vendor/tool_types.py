"""Shared structured types for tool execution and evidence tracking."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(slots=True)
class EvidenceItem:
    """A single piece of information returned by a retrieval tool."""

    text: str
    source_url: str = ""
    kind: str = "lead"  # Search snippets are leads; visited-page extracts are verified.
    quality: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    """Prompt-facing tool content plus runtime-only evidence metadata."""

    content: str
    evidence_items: list[EvidenceItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    success: bool = True

    def with_content(self, content: str) -> "ToolResult":
        """Return a copy with different prompt-facing content."""
        return replace(self, content=content)
