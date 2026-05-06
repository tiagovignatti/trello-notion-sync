# trello-notion-sync

One-way sync from a Trello board into a Notion database, polled every 15 minutes by GitHub Actions. Designed to extend to Google Keep as a second source without changing the Notion writer or the shared `Item` model.

**Direction is one-way.** Edits made in Notion are overwritten on the next sync. Deletions in Notion that don't exist in Trello won't reappear unless the Trello card changes.

## Architecture at a glance

```
Trello API ──► sources/trello.py ──► Item ──► notion_writer.py ──► Notion API
                                       ▲
                  (future) sources/keep.py ─┘
```

State (the last-processed Trello action ID) lives in `state/trello_last_action.json` and is committed back to the repo after each successful run. The repo is the source of truth for sync state — no external DB.

## Setup

### 1. Trello credentials

1. Go to <https://trello.com/power-ups/admin>, create a new Power-Up (any name; iframe URL can be `https://example.com`).
2. Open the Power-Up → "API key" tab. Copy the **API key**, then click "Token" to generate a **token**. Both are user-scoped and don't expire until revoked.
3. Find your **board ID**: it's the short slug in the board URL, e.g. `xrwY5KDi` from `trello.com/b/xrwY5KDi/...`.

### 2. Notion database

Create a database in Notion with these exact property names and types:

| Name          | Type          | Notes                              |
|---------------|---------------|------------------------------------|
| `Title`       | Title         | (the default title column)         |
| `Source`      | Select        | options: `Trello` (add `Keep` later) |
| `External ID` | Text          | upsert key                         |
| `Status`      | Select        | options auto-created on first sync |
| `Tags`        | Multi-select  | options auto-created on first sync |
| `URL`         | URL           |                                    |
| `Last Synced` | Date          |                                    |

Then create a Notion **internal integration** at <https://www.notion.so/my-integrations>, copy the **Internal Integration Token**, and grant it access to the database via the "..." menu → "Connections" → add your integration.

The database ID is the 32-char hex string in the database URL: `notion.so/<workspace>/<DATABASE_ID>?v=...`.

### 3. Local development

```bash
cp .env.example .env
# fill in credentials
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Validate Trello fetch + transform without touching Notion:
python -m src.sync trello --dry-run

# Real sync (requires NOTION_TOKEN + NOTION_DATABASE_ID):
python -m src.sync trello
```

### 4. GitHub Actions

Add these as repository secrets (Settings → Secrets and variables → Actions):

- `TRELLO_API_KEY`
- `TRELLO_TOKEN`
- `TRELLO_BOARD_ID`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

The workflow at `.github/workflows/trello-sync.yml` runs every 15 minutes (`*/15 * * * *`) and on manual dispatch. It commits state changes back to the default branch, so the workflow needs `contents: write` permission (already declared in the YAML).

## Mapping configuration

Edit `config/trello_mapping.yaml` to rename Trello lists/labels on the way into Notion. Lists/labels not present in the file pass through with their Trello name.

## Behavior notes

- **Closed Trello lists at backfill** (first run): their cards are skipped to keep the initial import clean.
- **Archiving a Trello list during incremental sync** propagates to Notion: every card in the newly-closed list is force re-fetched and marked archived. Symmetric for un-archive — re-opening a list restores its cards' pages.
- **Renaming a Trello list** also force re-fetches every card in that list so the Notion `Status` property catches up. The auto-prune (below) cleans the old Status option once no live page references it.
- **Archived Trello cards** mark their Notion page archived (page-level archive) but keep the page in place. Un-archiving in Trello restores it.
- **First run** is a full backfill of every open card on every open list. The marker is then advanced to Trello's most recent action.
- **Failed item** during a batch: the run exits non-zero, state is NOT advanced, and the next run retries everything since the last good marker. Successful upserts are idempotent.
- **Body content**: Trello card description is rendered as plain paragraph blocks split on blank lines. Inline markdown (`**bold**`, `[link](...)`) currently renders as raw text.
- **Status option pruning**: after each successful sync, Status select options that no *live* (non-archived) page references are removed from the database schema. Keeps the Notion Board view from showing empty groups for old/archived list names. Archived pages keep the option name as a ghost value — invisible in board view anyway.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

HTTP is mocked at the transport layer with `respx`; no live API calls run in tests.
