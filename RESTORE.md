# Restoring from the remote backup

The app's backup is **push-only** (Phase 20, `app/backup.py`): every send /
generation-complete / `write_file` mirrors the database and the agent
workspaces up to a remote host. Nothing is ever pulled automatically —
**restoring is a manual step you run yourself**, documented here.

> ⚠️ **Restore is a mirror overwrite, not a merge.** Pulling replaces local
> state with the remote copy. Only restore *onto* a machine whose local copy
> you're willing to discard. If the local machine has newer chats than the
> remote (e.g. the remote was offline during your last session), pulling will
> clobber them.

## What lives where on the remote

Configured in `.env` (current values shown):

| Thing | Local | Remote (`REMOTE_*`) | Layout on remote |
|---|---|---|---|
| Database | `DB_PATH` (`./data/chats.db`) | `REMOTE_DB_PATH` (`host1:/home/kyle/Documents/projects/olliellama_chats`) | flat `chats.db` mirror (always-latest) + dated `…_snapshots/<ts>/chats.db` |
| Workspaces | `FILE_TOOL_ROOT` (`./agent_workspace`) | `REMOTE_PATH` (`host1:/home/kyle/Documents/projects/agent_workspaces`) | flat tree mirror (always-latest) + dated `…_snapshots/<ts>/` |

The mirror is overwritten on every push (the latest). A server-side snapshot
is taken at most once a day into the sibling `*_snapshots/<timestamp>/` folder.

## Restore everything at once

Pull **both** the database and the workspaces from their remote mirrors in one
command (uses `REMOTE_DB_PATH`/`DB_PATH` and `REMOTE_PATH`/`FILE_TOOL_ROOT` from
`.env`):

```bash
python app/copy_agent_workspace.py --all
```

Add `--snapshot` to restore both halves from their latest dated snapshot
instead of the live mirror. **Stop `uvicorn` first** (the DB half overwrites the
live database). This is the seed-a-new-machine / full-restore path; the
per-half commands below are for when you only want one.

## Restore the database

The pushed `chats.db` is a transactionally-consistent standalone copy (made via
the SQLite backup API), so it has no `-wal`/`-shm` sidecars and is safe to copy.

**Stop the app (`uvicorn`) before restoring** — overwriting a live database can
corrupt it.

Latest mirror:

```bash
python app/copy_agent_workspace.py --db
```

Latest dated snapshot instead of the live mirror:

```bash
python app/copy_agent_workspace.py --db --snapshot
```

This pulls `REMOTE_DB_PATH/chats.db` → `DB_PATH`, then deletes any stale local
`chats.db-wal` / `chats.db-shm` (they belong to the *old* database and would
corrupt the freshly restored one). Restart with `uvicorn main:app --reload`.

Override the defaults if needed:

```bash
python app/copy_agent_workspace.py --db \
  --source host1:/home/kyle/Documents/projects/olliellama_chats \
  --dest ./data/chats.db
```

`--db` is **pull-only**: `--source` must be remote (`host:/path`) and `--dest`
must be local. Pushing the DB is the app's job (it ships a consistent copy);
pushing the *live* file by hand could ship a torn WAL state.

### Manual equivalent (no script)

```bash
# stop uvicorn first
rsync -avz host1:/home/kyle/Documents/projects/olliellama_chats/chats.db ./data/chats.db
rm -f ./data/chats.db-wal ./data/chats.db-shm
# from a snapshot: list timestamps, then pull one
ssh host1 'ls -t /home/kyle/Documents/projects/olliellama_chats_snapshots'
rsync -avz host1:/home/kyle/Documents/projects/olliellama_chats_snapshots/<TS>/chats.db ./data/chats.db
```

## Restore the workspaces

The script pulls the workspace tree (`REMOTE_PATH` → `FILE_TOOL_ROOT`) by
default — no flags needed:

```bash
python app/copy_agent_workspace.py
```

From the latest dated snapshot instead of the live mirror:

```bash
python app/copy_agent_workspace.py --snapshot
```

The sync is additive (no `--delete`), so local-only files survive.

### Manual equivalent (no script)

```bash
rsync -avz host1:/home/kyle/Documents/projects/agent_workspaces/ ./agent_workspace/
```

## Seeding a brand-new machine

Same commands — pull the DB into `DB_PATH` and the workspaces into
`FILE_TOOL_ROOT`, then start the app. Make sure `.env` exists first
(`cp .env.example .env`) so the paths resolve.
