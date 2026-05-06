from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Item:
    """Source-agnostic representation of a single synced object.

    All sources (Trello today, Keep later) transform their native objects into
    Item before reaching NotionWriter. This is the seam that lets one writer
    serve all sources without per-source branching.

    `archived` is not in the original brief but is needed to satisfy the
    acceptance criterion "archived Trello cards mark Notion archived". Encoding
    it via `status` would lose the original list/status when un-archiving.
    """

    external_id: str
    source: str
    title: str
    body: str
    status: str | None
    tags: list[str]
    url: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    source_metadata: dict[str, Any] = field(default_factory=dict)
