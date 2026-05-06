from datetime import datetime, timezone

from src.sources.trello import (
    card_ids_from_actions,
    card_to_item,
    list_ids_with_closed_toggle,
)


def _card(**overrides):
    base = {
        # ObjectID with leading timestamp = 2026-01-01T00:00:00Z (0x67751ba8 = 1735689000 — close enough)
        "id": "67751ba8000000000000abcd",
        "name": "Buy milk",
        "desc": "1L semi-skimmed",
        "idList": "list-1",
        "idLabels": ["lbl-prio"],
        "shortUrl": "https://trello.com/c/xyz",
        "closed": False,
        "dateLastActivity": "2026-05-06T10:00:00.000Z",
        "due": None,
    }
    base.update(overrides)
    return base


def test_card_to_item_basic_mapping():
    item = card_to_item(
        _card(),
        list_name_by_id={"list-1": "A fazer"},
        label_name_by_id={"lbl-prio": "Prioridade"},
        status_mapping={"A fazer": "A fazer"},
        tag_mapping={},
    )
    assert item.external_id == "trello:67751ba8000000000000abcd"
    assert item.source == "trello"
    assert item.title == "Buy milk"
    assert item.body == "1L semi-skimmed"
    assert item.status == "A fazer"
    assert item.tags == ["Prioridade"]
    assert item.url == "https://trello.com/c/xyz"
    assert item.archived is False
    assert item.updated_at == datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    assert item.source_metadata["list_id"] == "list-1"


def test_card_to_item_passes_through_unmapped_status_and_tags():
    item = card_to_item(
        _card(idLabels=["lbl-design"]),
        list_name_by_id={"list-1": "Concluído"},
        label_name_by_id={"lbl-design": "Time de Design"},
        status_mapping={},  # empty -> passthrough
        tag_mapping={},
    )
    assert item.status == "Concluído"
    assert item.tags == ["Time de Design"]


def test_card_to_item_handles_archived_card():
    item = card_to_item(
        _card(closed=True, desc=""),
        list_name_by_id={"list-1": "A fazer"},
        label_name_by_id={},
        status_mapping={},
        tag_mapping={},
    )
    assert item.archived is True
    assert item.body == ""


def test_card_to_item_drops_unknown_label_ids():
    item = card_to_item(
        _card(idLabels=["lbl-prio", "lbl-deleted"]),
        list_name_by_id={"list-1": "A fazer"},
        label_name_by_id={"lbl-prio": "Prioridade"},  # lbl-deleted not present
        status_mapping={},
        tag_mapping={},
    )
    assert item.tags == ["Prioridade"]


def test_card_to_item_archives_when_list_is_closed():
    item = card_to_item(
        _card(closed=False),
        list_name_by_id={"list-1": "Bloqueio"},
        label_name_by_id={},
        status_mapping={},
        tag_mapping={},
        list_closed=True,
    )
    # Card itself is open in Trello, but its list was archived → archived in Notion.
    assert item.archived is True


def test_card_to_item_archives_when_card_closed_even_if_list_open():
    item = card_to_item(
        _card(closed=True),
        list_name_by_id={"list-1": "A fazer"},
        label_name_by_id={},
        status_mapping={},
        tag_mapping={},
        list_closed=False,
    )
    assert item.archived is True


def test_list_ids_with_closed_toggle_picks_up_both_directions():
    actions = [
        {"type": "updateList", "data": {
            "list": {"id": "L1", "closed": True},
            "old": {"closed": False},
        }},
        {"type": "updateList", "data": {
            "list": {"id": "L2", "closed": False},
            "old": {"closed": True},
        }},
        {"type": "updateList", "data": {
            "list": {"id": "L3", "name": "Renamed"},
            "old": {"name": "Old name"},  # not a closed-toggle
        }},
        {"type": "createCard", "data": {"card": {"id": "c1"}}},  # ignored
    ]
    assert sorted(list_ids_with_closed_toggle(actions)) == ["L1", "L2"]


def test_list_ids_with_closed_toggle_dedupes():
    actions = [
        {"type": "updateList", "data": {
            "list": {"id": "L1", "closed": True},
            "old": {"closed": False},
        }},
        {"type": "updateList", "data": {
            "list": {"id": "L1", "closed": False},
            "old": {"closed": True},
        }},
    ]
    assert list_ids_with_closed_toggle(actions) == ["L1"]


def test_card_ids_from_actions_dedupes_in_order():
    actions = [
        {"type": "createCard", "data": {"card": {"id": "c1"}}},
        {"type": "updateCard", "data": {"card": {"id": "c2"}}},
        {"type": "addLabelToCard", "data": {"card": {"id": "c1"}}},
        {"type": "commentCard", "data": {"card": {"id": "c3"}}},  # ignored
        {"type": "updateCard", "data": {"card": {"id": "c1"}}},
    ]
    assert card_ids_from_actions(actions) == ["c1", "c2"]


def test_card_ids_from_actions_handles_malformed_entries():
    actions = [
        {"type": "updateCard"},  # no data
        {"type": "updateCard", "data": {}},  # no card
        {"type": "updateCard", "data": {"card": {}}},  # no id
        {"type": "updateCard", "data": {"card": {"id": "ok"}}},
    ]
    assert card_ids_from_actions(actions) == ["ok"]
