# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Streamlit web app that compares and syncs files between a locally mounted **Seagate external drive** and **Google Drive (My Drive)**, deployed as a hardened Docker Compose service. UI language is **Vietnamese**; code, comments, and docs are English.

Core features: scan both sides → compare by relative path (size match by default, optional MD5) → build a sync plan (up / down / two-way, conflict policy, optional mirror deletions) → execute in a background thread with live progress (files, bytes, speed, ETA, cancel) → record every run in SQLite history.

## Tech Stack

- Python 3.12, Streamlit ~1.41
- `google-api-python-client` (Drive v3), `google-auth-oauthlib` (OAuth Desktop-app flow)
- SQLite (WAL) for sync history, `pandas` for tables/CSV export
- Docker Compose: non-root user, read-only rootfs, localhost-only binding

## Repository Layout

```
app/
  main.py            # Streamlit UI (4 tabs: Compare / Sync / History / Guide) — Vietnamese strings
  config.py          # env-driven paths & constants (SCOPES, chunk sizes, DEFAULT_EXCLUDES)
  security.py        # require_login(): APP_PASSWORD gate (hmac.compare_digest + 1s delay)
  utils.py           # human_size / human_rate / human_eta / ts_to_str
  services/          # pure Python — NO Streamlit imports allowed here
    common.py        #   SyncCancelled exception
    scanner.py       #   LocalFile, scan_local(), md5_of()
    gdrive.py        #   RemoteFile, OAuth helpers, DriveClient (list_tree / upload / download / trash)
    compare.py       #   compare_maps() → (items, counts, byte_totals); statuses below
    scan.py          #   ScanState, ScanRunner (background thread: scan both sides + compare)
    sync.py          #   build_plan(), Action, ProgressState, SyncRunner (background thread)
    history.py       #   SQLite sessions: init_db / start_session / finish_session / fetch_sessions
scripts/authorize.py # optional host-side helper: generates secrets/token.json via browser
tests/test_logic.py  # stdlib assert-based tests (no pytest dependency)
secrets/             # credentials.json (user-provided) + token.json (generated) — gitignored
data/                # sync_history.db — gitignored
Dockerfile, docker-compose.yml, .env(.example), requirements.txt, README.md (Vietnamese)
```

## Commands

```bash
# Production (Docker)
cp .env.example .env          # then set SEAGATE_MOUNT and APP_PASSWORD
docker compose up -d --build
docker compose logs -f        # app at http://localhost:8501

# Local development (from repo root)
pip install -r requirements.txt
SEAGATE_PATH=/path/to/seagate streamlit run app/main.py

# Tests (plain python, no runner needed)
python tests/test_logic.py
```

## Environment Variables

| Var                            | Default                                                                               | Purpose                                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `SEAGATE_MOUNT`              | — (required, compose only)                                                           | Host path of the Seagate drive, bind-mounted to`/data/seagate`                      |
| `SEAGATE_PATH`               | `/data/seagate`                                                                     | In-container/local path the app scans                                                 |
| `SECRETS_DIR` / `DATA_DIR` | `<repo>/secrets`, `<repo>/data` (dev) · `/app/secrets`, `/app/data` (Docker) | credentials/token · SQLite DB                                                        |
| `APP_PASSWORD`               | empty (warns)                                                                         | Login gate for the UI                                                                 |
| `DRIVE_ROOT_FOLDER`          | `root`                                                                              | Drive folder to sync against (`root` = entire My Drive, or e.g. `Backup/Seagate`) |
| `TZ`                         | —                                                                                    | e.g.`Asia/Ho_Chi_Minh`                                                              |

## Architecture & Data Flow

1. **Auth** (`gdrive.py`): Web-application OAuth. `build_web_auth_url()` → user authorizes on Google → Google redirects back to the app with `?code=` (`OAUTH_REDIRECT_URI`, default `http://localhost:8501/`) → `main._handle_oauth_callback()` → `exchange_code()` → token saved to `secrets/token.json` (chmod 600, auto-refresh on load). No `Flow` object needs to survive the rerun; the page reload wipes `st.session_state`.
2. **Scan**: `scanner.scan_local()` (os.walk, exclude patterns, regular files only) and `DriveClient.list_tree()` (BFS, pageSize 1000, skips shortcuts, warns on duplicate names, returns file map + folder-id map keyed by POSIX relpath).
3. **Compare** (`compare.py`): statuses `IDENTICAL / DIFFERENT / LOCAL_ONLY / REMOTE_ONLY / GOOGLE_NATIVE` (Vietnamese labels in `STATUS_VI`). Two passes: cheap size check first, then MD5 only for opted-in size-match candidates. mtime tolerance: 2s.
   Steps 2–3 run inside `ScanRunner` (`scan.py`), a `threading.Thread` mirroring `SyncRunner`: own `DriveClient`, thread-safe `ScanState` (phase `local → drive → compare`, counters, MD5 %, cancel `Event`). Scanning **must not** run inline in the Streamlit script — that blocks the run and no button, including Stop, can be clicked. `scan_local` / `list_tree` / `compare_maps` all take `cancel` and raise `SyncCancelled`; a cancelled scan yields **no** partial result (partial MD5 results would mislabel unhashed same-size files as identical).
4. **Plan** (`sync.build_plan()`): directions `DIR_UP / DIR_DOWN / DIR_BOTH`, conflict policies `CONFLICT_NEWER / CONFLICT_FORCE / CONFLICT_SKIP`, ops `OP_UPLOAD / OP_UPDATE_REMOTE / OP_DOWNLOAD / OP_UPDATE_LOCAL / OP_TRASH_REMOTE / OP_DELETE_LOCAL` (labels in `OP_VI`). Transfers ordered by relpath; deletions (mirror mode, one-way only) always last.
5. **Execute** (`SyncRunner`, a `threading.Thread`): creates its **own** `DriveClient` (httplib2 is not thread-safe), reports via `ProgressState` (thread-safe: lock, cancel `Event`, rolling 8s speed, ETA, log deque), writes a `history` session. UI polls `progress.snapshot()` + `time.sleep(0.7)` + `st.rerun()`; the Cancel button sets the cancel event.

## Guardrails — do not change without an explicit user request

- OAuth scope stays exactly `https://www.googleapis.com/auth/drive` (single scope; never expand silently).
- Deletions are always recoverable: Drive → Trash (`trash_file`), local → `.sync_trash/<timestamp>/` on the Seagate drive. Never hard-delete. Mirror mode requires the user to type `XOA` in the UI.
- Keep port binding `127.0.0.1:8501` in compose; recommend a reverse proxy for remote exposure instead of `0.0.0.0`.
- Container stays non-root (UID 1000), `read_only: true` rootfs + tmpfs `/tmp` + `HOME=/tmp`, `cap_drop: [ALL]`, `no-new-privileges`.
- Keep `hmac.compare_digest` and the ~1s failure delay in `security.py`. Never log tokens or credential contents.
- **No Streamlit imports/calls inside `app/services/` or inside `SyncRunner`** — services communicate only via callbacks and `ProgressState`.
- User-facing strings are Vietnamese; identifiers/comments English.

## Key Behaviors & Gotchas

- mtime is preserved both ways (upload sets `modifiedTime`; download calls `os.utime`), so "newer wins" is trustworthy from the first sync onward. Default comparison is size-match (Drive mtimes differ before the first sync).
- Downloads write to `<name>.syncpart` then `os.replace()` (atomic). Uploads are resumable, 8 MiB chunks, `num_retries=5`; **0-byte files must use the non-resumable path** (resumable upload fails on empty files).
- Google-native files (Docs/Sheets/Slides) have no size/MD5 → status `GOOGLE_NATIVE`, always skipped.
- Drive allows duplicate names in one folder; `list_tree` keeps the first and logs a warning. `/` in Drive filenames is replaced with `_` locally.
- OAuth loopback over http requires `OAUTHLIB_INSECURE_TRANSPORT=1` and `OAUTHLIB_RELAX_TOKEN_SCOPE=1` (set at module import in `gdrive.py`).
- Warn users not to modify either side while a sync is running (folder cache may create duplicates otherwise).
- `history.py` opens a fresh SQLite connection per call (WAL) — safe across the UI thread and SyncRunner.

## Current Status

Everything in the layout above is written and the app runs under Docker Compose. Dependencies are pinned in `requirements.txt` (streamlit==1.41.1, google-api-python-client==2.156.0, google-auth==2.37.0, google-auth-oauthlib==1.2.1, google-auth-httplib2==0.2.0, pandas==2.2.3) — keep them pinned.
