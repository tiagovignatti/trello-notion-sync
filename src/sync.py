from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.sources.trello import (
    TrelloClient,
    card_ids_from_actions,
    card_to_item,
    list_ids_to_resync,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
CONFIG_DIR = REPO_ROOT / "config"


def _log(msg: str) -> None:
    print(f"[sync] {msg}", file=sys.stderr, flush=True)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise SystemExit(f"missing env var: {key}")
    return value


def sync_trello(*, dry_run: bool) -> int:
    api_key = _require_env("TRELLO_API_KEY")
    token = _require_env("TRELLO_TOKEN")
    board_id = _require_env("TRELLO_BOARD_ID")

    state_path = STATE_DIR / "trello_last_action.json"
    state = load_state(state_path)
    last_action_id = state.get("last_action_id")

    mapping = load_mapping(CONFIG_DIR / "trello_mapping.yaml")
    status_mapping: dict[str, str] = mapping.get("status_by_list") or {}
    tag_mapping: dict[str, str] = mapping.get("tags_by_label") or {}

    trello = TrelloClient(api_key=api_key, token=token, board_id=board_id)
    try:
        # Fetch all lists (including closed). Backfill skips closed-list cards
        # at fetch time, but incremental needs the closed map to propagate
        # list-archive events into Notion-archive on each card.
        all_lists = trello.get_lists(only_open=False)
        list_name_by_id = {l["id"]: l["name"] for l in all_lists}
        list_closed_by_id = {l["id"]: bool(l["closed"]) for l in all_lists}
        labels = trello.get_labels()
        label_name_by_id = {l["id"]: l["name"] for l in labels}

        if not last_action_id:
            _log("no state — performing initial backfill")
            cards = [
                c for c in trello.get_all_cards()
                if not list_closed_by_id.get(c["idList"], True)
            ]
        else:
            _log(f"incremental sync since action {last_action_id}")
            actions = trello.get_actions_since(last_action_id)

            # Cards directly touched by card-related actions, plus cards in any
            # list whose state changed in a way that affects member cards
            # (archive toggle or rename) — force re-sync so propagation
            # reaches Notion.
            card_ids: set[str] = set(card_ids_from_actions(actions))
            for list_id in list_ids_to_resync(actions):
                try:
                    for card in trello.get_cards_in_list(list_id):
                        card_ids.add(card["id"])
                except Exception as e:
                    _log(f"skip list {list_id}: {e}")

            cards = []
            for cid in card_ids:
                try:
                    card = trello.get_card(cid)
                except Exception as e:
                    # 404s are expected for hard-deleted cards. Skip and continue.
                    _log(f"skip card {cid}: {e}")
                    continue
                if card["idList"] not in list_name_by_id:
                    continue
                cards.append(card)

        items = [
            card_to_item(
                c, list_name_by_id, label_name_by_id, status_mapping, tag_mapping,
                list_closed=list_closed_by_id.get(c["idList"], False),
            )
            for c in cards
        ]
        _log(f"{len(items)} item(s) to upsert")

        if dry_run:
            for item in items:
                print(json.dumps({
                    "external_id": item.external_id,
                    "title": item.title,
                    "status": item.status,
                    "tags": item.tags,
                    "archived": item.archived,
                    "url": item.url,
                    "body_chars": len(item.body),
                }, ensure_ascii=False))
            return 0

        # Defer Notion import until non-dry-run so dry-run works without
        # NOTION_TOKEN configured (useful for first-time validation).
        from src.notion_writer import NotionWriter

        notion_token = _require_env("NOTION_TOKEN")
        database_id = _require_env("NOTION_DATABASE_ID")
        writer = NotionWriter(token=notion_token, database_id=database_id)

        failures: list[str] = []
        success_count = 0
        for item in items:
            try:
                writer.upsert(item)
                success_count += 1
            except Exception as e:
                failures.append(item.external_id)
                _log(f"FAIL {item.external_id}: {e}")

        if failures:
            _log(f"{len(failures)} failure(s); state NOT advanced")
            return 1

        new_marker = trello.get_latest_action_id() or last_action_id
        save_state(state_path, {
            "board_id": board_id,
            "last_action_id": new_marker,
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "items_synced_total": state.get("items_synced_total", 0) + success_count,
        })

        # Best-effort cleanup of stale Status options. State is already saved,
        # so a failure here doesn't invalidate the sync.
        try:
            removed = writer.prune_unused_status_options()
            if removed:
                _log(f"pruned status options: {', '.join(removed)}")
        except Exception as e:
            _log(f"prune failed (non-fatal): {e}")
        finally:
            writer.close()

        _log(f"OK: {success_count} item(s); marker={new_marker}")
        return 0
    finally:
        trello.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="sync")
    sub = parser.add_subparsers(dest="source", required=True)
    p_trello = sub.add_parser("trello", help="sync Trello → Notion")
    p_trello.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and transform but don't write to Notion",
    )
    args = parser.parse_args()
    if args.source == "trello":
        return sync_trello(dry_run=args.dry_run)
    return 2


if __name__ == "__main__":
    sys.exit(main())
