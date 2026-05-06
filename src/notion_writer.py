from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from src.item import Item
from src.markdown import body_to_blocks

NOTION_API = "https://api.notion.com/v1"
# Pinned to the pre-data-sources API. Newer versions split databases into
# "data sources" and move /databases/{id}/query elsewhere; staying on this
# version keeps the simple model that fits a single-source-per-DB use case.
NOTION_VERSION = "2022-06-28"

# Retry transient Notion failures (timeouts, dropped connections, 5xx, 429).
# At-least-once semantics: a ReadTimeout that actually landed server-side
# could double-apply on retry — most visibly, a retried POST /pages would
# create a duplicate. The risk already exists today because a single failure
# aborts the batch and the next cron retries everything; per-call retry just
# narrows the window. Acceptable at personal volume.
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
)


class NotionWriter:
    """Idempotent upsert into a Notion database, keyed by Item.external_id.

    Looks up the existing page via the External ID rich-text property. The
    alternative — maintaining a local id-mapping file — was rejected: one
    extra read per item is cheap at personal volume, and a local mapping is
    a second source of truth that can drift from Notion's reality.
    """

    def __init__(self, token: str, database_id: str, *, timeout: float = 30.0):
        self._database_id = database_id
        self._client = httpx.Client(
            base_url=NOTION_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def upsert(self, item: Item) -> str:
        existing_id = self._find_page_id(item.external_id)
        properties = self._properties(item)

        if existing_id:
            self._patch(f"/pages/{existing_id}", {
                "properties": properties,
                "archived": item.archived,
            })
            # Notion 404s on /blocks/{id}/children for archived pages, so skip
            # the body refresh when archiving. The body stays frozen at archive
            # time; un-archiving in Trello triggers a re-sync that refreshes it.
            if not item.archived:
                self._replace_body(existing_id, item.body)
            return existing_id

        body = {
            "parent": {"database_id": self._database_id},
            "properties": properties,
            "children": body_to_blocks(item.body),
        }
        page = self._post("/pages", body)
        page_id = page["id"]
        if item.archived:
            self._patch(f"/pages/{page_id}", {"archived": True})
        return page_id

    def _find_page_id(self, external_id: str) -> str | None:
        result = self._post(
            f"/databases/{self._database_id}/query",
            {
                "filter": {
                    "property": "External ID",
                    "rich_text": {"equals": external_id},
                },
                "page_size": 1,
            },
        )
        results = result.get("results") or []
        return results[0]["id"] if results else None

    def _properties(self, item: Item) -> dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Notion title cells cap at 2000 chars per rich_text item.
        return {
            "Title": {
                "title": [{"type": "text", "text": {"content": item.title[:2000]}}]
            },
            "Source": {"select": {"name": item.source.capitalize()}},
            "External ID": {
                "rich_text": [
                    {"type": "text", "text": {"content": item.external_id}}
                ]
            },
            "Status": (
                {"select": {"name": item.status}} if item.status else {"select": None}
            ),
            "Tags": {"multi_select": [{"name": t} for t in item.tags]},
            "URL": {"url": item.url or None},
            "Last Synced": {"date": {"start": now_iso}},
        }

    def _replace_body(self, page_id: str, body: str) -> None:
        # Notion has no atomic "set children". Delete existing, append new.
        # On rare partial-failure here, the page body could be empty until the
        # next sync — acceptable, since the next run will re-replace it.
        existing = self._get(f"/blocks/{page_id}/children").get("results", [])
        for block in existing:
            self._delete(f"/blocks/{block['id']}")
        new_blocks = body_to_blocks(body)
        if new_blocks:
            self._patch(f"/blocks/{page_id}/children", {"children": new_blocks})

    def _send(self, method: str, path: str, body: dict | None = None) -> dict:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                r = self._client.request(method, path, json=body)
            except _RETRY_EXCEPTIONS as e:
                last_exc = e
                if attempt + 1 == _MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code in _RETRY_STATUSES and attempt + 1 < _MAX_RETRIES:
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2 ** attempt
                time.sleep(min(delay, 10))
                continue
            r.raise_for_status()
            return r.json()
        raise last_exc  # pragma: no cover

    def _post(self, path: str, body: dict) -> dict:
        return self._send("POST", path, body)

    def _patch(self, path: str, body: dict) -> dict:
        return self._send("PATCH", path, body)

    def _get(self, path: str) -> dict:
        return self._send("GET", path)

    def _delete(self, path: str) -> dict:
        return self._send("DELETE", path)

    def prune_unused_status_options(self) -> list[str]:
        """Remove Status select options no live page references. Returns removed names.

        Live = not page-archived. An option whose only references are on archived
        pages (e.g. cards from a now-closed Trello list) is dropped from the schema
        so the Notion Board view doesn't render an empty group for it. The archived
        pages keep the option name as a ghost value — invisible in the board view
        anyway because archived pages aren't surfaced there.
        """
        db = self._get(f"/databases/{self._database_id}")
        status = db.get("properties", {}).get("Status", {})
        options = (status.get("select") or {}).get("options") or []
        if not options:
            return []

        kept: list[dict] = []
        removed: list[str] = []
        for opt in options:
            if self._has_live_page_with_status(opt["name"]):
                kept.append(opt)
            else:
                removed.append(opt["name"])

        if not removed:
            return []

        self._patch(
            f"/databases/{self._database_id}",
            {"properties": {"Status": {"select": {"options": kept}}}},
        )
        return removed

    def _has_live_page_with_status(self, name: str) -> bool:
        # databases.query returns archived pages too, so we filter client-side.
        # 100 results per page is plenty at personal volume; paginate defensively.
        cursor: str | None = None
        while True:
            body: dict = {
                "filter": {"property": "Status", "select": {"equals": name}},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            result = self._post(f"/databases/{self._database_id}/query", body)
            for page in result.get("results") or []:
                if not page.get("archived", False):
                    return True
            if not result.get("has_more"):
                return False
            cursor = result.get("next_cursor")

    def close(self) -> None:
        self._client.close()
