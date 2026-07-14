# n8n API Sync

Use `scripts/sync_workflow_to_n8n.py` to update an existing n8n workflow from the
JSON file stored in this repository.

## 1. Create an n8n API key

In n8n:

1. Open `Settings`.
2. Open `n8n API`.
3. Select `Create an API key`.
4. Copy the API key.

Do not commit the API key.

## 2. Configure `.env`

Copy `.env.example` to `.env`, then set:

```bash
N8N_API_BASE_URL=http://localhost:5678
N8N_API_KEY=your-api-key
N8N_WORKFLOW_ID=your-existing-workflow-id
```

If you do not know the workflow ID, list workflows:

```bash
python3 scripts/sync_workflow_to_n8n.py --list
```

You can also omit `N8N_WORKFLOW_ID` and set an exact workflow name:

```bash
N8N_WORKFLOW_NAME=Daily Keyword News Summary
```

If multiple workflows have the same name, use the workflow ID.

## 3. Dry run

```bash
python3 scripts/sync_workflow_to_n8n.py --dry-run
```

This validates the local JSON and prints the target workflow without updating
n8n.

## 4. Sync to n8n

```bash
python3 scripts/sync_workflow_to_n8n.py
```

By default, the script preserves the current active state of the workflow in
n8n. To force a state:

```bash
python3 scripts/sync_workflow_to_n8n.py --active-state activate
python3 scripts/sync_workflow_to_n8n.py --active-state deactivate
python3 scripts/sync_workflow_to_n8n.py --active-state from-json
```

## Notes

- The script updates an existing workflow. It does not create a new one.
- It updates workflow structure only: name, nodes, connections, settings, and
  static data.
- Credentials are not stored in workflow JSON and are not updated by this
  script.
- If you edited the workflow in the n8n UI after exporting, syncing from this
  repository can overwrite those UI changes.

