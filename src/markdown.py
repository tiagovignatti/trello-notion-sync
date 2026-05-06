from __future__ import annotations

from typing import Any

NOTION_TEXT_LIMIT = 2000  # Notion's per-rich_text-item character cap


def _chunk(text: str, limit: int = NOTION_TEXT_LIMIT) -> list[str]:
    if not text:
        return [""]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _paragraph_block(text: str) -> dict[str, Any]:
    rich_text = [
        {"type": "text", "text": {"content": chunk}}
        for chunk in _chunk(text)
    ]
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text},
    }


def body_to_blocks(body: str) -> list[dict[str, Any]]:
    """Convert body text into a list of Notion block objects.

    v0.1: paragraphs only, split on blank lines. Inline markdown (bold,
    italic, links) is rendered as raw text — fine for typical Trello desc
    passthrough; upgrade to a real markdown parser if descriptions grow rich.
    """
    if not body or not body.strip():
        return []
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    return [_paragraph_block(p) for p in paragraphs]
