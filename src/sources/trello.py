from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from src.item import Item

TRELLO_API = "https://api.trello.com/1"

# Trello action types that touch a card's representation. Used to decide which
# cards to re-fetch during incremental sync. We re-fetch the whole card rather
# than reconstruct state from action diffs — simpler and always correct.
CARD_RELATED_ACTIONS = frozenset({
    "createCard",
    "updateCard",
    "addLabelToCard",
    "removeLabelFromCard",
    "deleteCard",
    "moveCardToBoard",
    "copyCard",
    "convertToCardFromCheckItem",
})

CARD_FIELDS = "id,name,desc,idList,idLabels,shortUrl,closed,dateLastActivity,due"


class TrelloClient:
    def __init__(self, api_key: str, token: str, board_id: str, *, timeout: float = 30.0):
        self.api_key = api_key
        self.token = token
        self.board_id = board_id
        self._client = httpx.Client(timeout=timeout)

    def _params(self, **kw: str) -> dict[str, str]:
        return {"key": self.api_key, "token": self.token, **kw}

    def _get(self, path: str, **params: str) -> Any:
        r = self._client.get(f"{TRELLO_API}{path}", params=self._params(**params))
        r.raise_for_status()
        return r.json()

    def get_lists(self, *, only_open: bool = True) -> list[dict]:
        return self._get(
            f"/boards/{self.board_id}/lists",
            filter="open" if only_open else "all",
            fields="id,name,closed",
        )

    def get_labels(self) -> list[dict]:
        return self._get(f"/boards/{self.board_id}/labels", fields="id,name,color")

    def get_all_cards(self) -> list[dict]:
        return self._get(
            f"/boards/{self.board_id}/cards",
            filter="open",
            fields=CARD_FIELDS,
        )

    def get_card(self, card_id: str) -> dict:
        return self._get(f"/cards/{card_id}", fields=CARD_FIELDS)

    def get_cards_in_list(self, list_id: str) -> list[dict]:
        """All cards (open + archived) in a list, even if the list itself is closed."""
        return self._get(f"/lists/{list_id}/cards", filter="all", fields=CARD_FIELDS)

    def get_actions_since(self, last_action_id: str) -> list[dict]:
        """Return actions newer than last_action_id, oldest-first."""
        actions = self._get(
            f"/boards/{self.board_id}/actions",
            since=last_action_id,
            limit="1000",
            filter="all",
        )
        return list(reversed(actions))  # Trello returns newest-first

    def get_latest_action_id(self) -> str | None:
        actions = self._get(
            f"/boards/{self.board_id}/actions",
            limit="1",
            filter="all",
        )
        return actions[0]["id"] if actions else None

    def close(self) -> None:
        self._client.close()


def card_to_item(
    card: dict,
    list_name_by_id: dict[str, str],
    label_name_by_id: dict[str, str],
    status_mapping: dict[str, str],
    tag_mapping: dict[str, str],
    *,
    list_closed: bool = False,
) -> Item:
    """Transform a Trello card dict into a source-agnostic Item.

    Caller is responsible for filtering: cards whose idList isn't in
    list_name_by_id should not be passed in. Caller decides what "open lists"
    means and what to do with archived ones.

    Archive rule: archived = card.closed OR list_closed. A card whose list
    has been archived in Trello propagates to Notion as an archived page —
    matches user intuition that archiving a list removes its cards from the
    active workflow.
    """
    list_name = list_name_by_id[card["idList"]]
    status = status_mapping.get(list_name, list_name)

    tags: list[str] = []
    for lid in card.get("idLabels", []) or []:
        label_name = label_name_by_id.get(lid)
        if not label_name:
            continue
        tags.append(tag_mapping.get(label_name, label_name))

    # Trello card IDs are Mongo ObjectIDs; the leading 8 hex chars encode the
    # creation Unix timestamp. dateLastActivity gives us updated_at directly.
    created_at = datetime.fromtimestamp(int(card["id"][:8], 16), tz=timezone.utc)
    updated_at = datetime.fromisoformat(card["dateLastActivity"].replace("Z", "+00:00"))

    return Item(
        external_id=f"trello:{card['id']}",
        source="trello",
        title=card["name"],
        body=card.get("desc") or "",
        status=status,
        tags=tags,
        url=card["shortUrl"],
        created_at=created_at,
        updated_at=updated_at,
        archived=bool(card.get("closed", False)) or list_closed,
        source_metadata={
            "list_id": card["idList"],
            "list_name": list_name,
            "due": card.get("due"),
        },
    )


def list_ids_to_resync(actions: list[dict]) -> list[str]:
    """List IDs whose state changed in a way that invalidates member cards' sync.

    Captures `closed` toggles (archive/un-archive) and `name` changes — both
    affect Notion-side state for every card in the list (archive flag and
    Status text respectively). Other list updates (position, etc.) are ignored.
    """
    seen: set[str] = set()
    out: list[str] = []
    for action in actions:
        if action.get("type") != "updateList":
            continue
        data = action.get("data") or {}
        old = data.get("old") or {}
        if "closed" not in old and "name" not in old:
            continue
        list_obj = data.get("list") or {}
        list_id = list_obj.get("id")
        if not list_id or list_id in seen:
            continue
        seen.add(list_id)
        out.append(list_id)
    return out


def card_ids_from_actions(actions: list[dict]) -> list[str]:
    """Extract distinct card IDs from card-related actions, in encounter order."""
    seen: set[str] = set()
    out: list[str] = []
    for action in actions:
        if action.get("type") not in CARD_RELATED_ACTIONS:
            continue
        card = (action.get("data") or {}).get("card") or {}
        cid = card.get("id")
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out
