# Local <-> Google Drive Sync

A Streamlit web app that **compares and syncs** files between a locally mounted
drive (e.g. a Seagate external disk) and **Google Drive** (My Drive), deployed
as a hardened Docker Compose service. Both sides are scoped freely from the
sidebar: any Drive folder against any subfolder inside the mounted drive.

## Features

- **Scan** both sides and **compare** by relative path (same path + same size =
  identical), with live progress and a **Stop** button at any time. From the
  second scan onward the Drive side only asks for **what changed** (Changes
  API), so it takes just a few seconds.
- **Plan** the sync: upload / download / two-way, conflict policy, optional
  **mirror** mode (delete files that no longer exist on the source side).
- **Google-native files** (Docs/Sheets/Slides) are skipped by default; an
  optional one-way **export** saves `.docx`/`.xlsx`/`.pptx` copies to the
  Seagate drive and refreshes them whenever the Drive version is newer.
- **Background execution** with live progress: files, bytes, speed, ETA, a
  Cancel button, and several files transferred in parallel. Dropped
  connections mid-transfer are retried automatically with backoff.
- **Explorer**: after a scan, browse the merged folder tree of both sides —
  drill into folders, see per-folder difference counts, filter by side or by
  differences only.
- **History** of every sync session stored in SQLite, exportable as CSV.
- **Storage at a glance**: sidebar gauges show used/total space of both the
  Seagate filesystem and the Google Drive quota; the plan warns when a
  download would not fit in the remaining free space.
- **Safety**: deletions are always recoverable (Drive -> Trash, Seagate ->
  `.sync_trash/`); nothing is ever hard-deleted.

## Requirements

- Docker + Docker Compose (recommended), or Python 3.12 for local development.
- A Seagate drive mounted on the host.
- A Google **OAuth client** (type **Desktop app**) — see below.

## 1. Create the OAuth client on Google Cloud

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. **APIs & Services -> Library** -> enable the **Google Drive API**.
3. **APIs & Services -> OAuth consent screen**: choose *External*, fill in the
   basics, and add your own email under **Test users**.
4. **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**
   -> application type **Desktop app** -> **Download JSON**.
5. Rename the downloaded file to `credentials.json` and put it in `secrets/`.

> The app uses exactly one scope: `https://www.googleapis.com/auth/drive`.

## 2. Run with Docker (recommended)

```bash
cp .env.example .env          # then set SEAGATE_MOUNT and APP_PASSWORD
docker compose up -d --build
docker compose logs -f        # open http://localhost:8501
```

## 3. Local development (no Docker)

```bash
pip install -r requirements.txt
SEAGATE_PATH=/path/to/seagate streamlit run app/main.py
```

## Connecting Google Drive

In the sidebar, click **Dang nhap voi Google** (Sign in with Google). Google
opens its consent page and then redirects straight back to the app with the
authorization code — nothing to copy or paste. The token is saved to
`secrets/token.json` and refreshes automatically.

Alternative:

- `python scripts/authorize.py` — generates `secrets/token.json` on the host
  using your browser (useful before the first container start).

The app is not Google-verified, so the consent page shows a warning; click
**Advanced -> Go to app** to continue.

## Usage

1. **Compare** — in the sidebar pick the Drive root folder and the local
   subfolder (empty = the whole drive; a not-yet-existing subfolder is created
   on the first download), then click *Quet & So sanh*. The first scan is a
   full listing; later scans are incremental (only changes are fetched). If a
   fast scan ever looks wrong, click *Quet lai toan bo* (full rescan). Click
   *Dung quet* to stop mid-scan — scanning only reads, so stopping is always
   safe; you just get no comparison result and need to rescan.
2. **Explore** — browse the merged tree of both sides, folder by folder, with
   per-folder difference counts and side/difference filters.
3. **Sync** — pick a direction (defaults to Drive -> local) and conflict
   policy -> *Lap ke hoach* to preview the plan -> *Bat dau dong bo*.
   **Mirror** mode requires typing `XOA` to confirm the deletions. The *Xuat
   file Google* checkbox (download directions only) exports Docs/Sheets/Slides
   as Office copies on the Seagate side; the copies are never uploaded back
   and the Drive originals are never touched.
4. **History** — review past sessions, download CSV.

> **Do not modify either side** while a sync is running.

## Environment variables

| Variable             | Default          | Purpose                                                        |
| -------------------- | ---------------- | -------------------------------------------------------------- |
| `SEAGATE_MOUNT`      | *(required)*     | Host path of the Seagate drive, mounted at `/data/seagate`     |
| `SEAGATE_PATH`       | `/data/seagate`  | Path the app scans (inside the container / in local dev)       |
| `SEAGATE_SUBDIR`     | `googledrive`    | Subfolder preselected in the UI (changeable there; empty = whole drive) |
| `APP_PASSWORD`       | *(empty)*        | UI login password (empty shows a warning)                      |
| `DRIVE_ROOT_FOLDER`  | `root`           | Drive folder to compare against (`root` = entire My Drive)     |
| `SYNC_WORKERS`       | `4`              | Parallel transfer workers during sync (1 = sequential)         |
| `OAUTH_REDIRECT_URI` | `http://localhost:8501/` | The app's own URL, used as the OAuth redirect          |
| `SECRETS_DIR`        | `./secrets`      | Location of `credentials.json` + `token.json`                  |
| `DATA_DIR`           | `./data`         | Location of `sync_history.db` and the Drive scan cache         |
| `TZ`                 | —                | Timezone, e.g. `Asia/Ho_Chi_Minh`                              |

## Data safety

- **Never hard-deletes**: Drive -> **Trash**; Seagate -> `.sync_trash/<timestamp>/`.
- **Mirror** works only for one-way syncs and requires typing `XOA` to confirm.
- `mtime` is preserved in both directions, so "newer wins" is trustworthy.
- Google-native files (Docs/Sheets/Slides) are never synced as-is — **skipped**
  unless the optional export is enabled, which only writes Office copies on the
  Seagate side (mirror mode never deletes those copies).

## Deployment security

- The port binds to `127.0.0.1:8501` only. For remote access put a **reverse
  proxy** (HTTPS + auth) in front; do **not** switch to `0.0.0.0`.
- The container runs **non-root** (UID 1000) with a **read-only** rootfs +
  tmpfs `/tmp`, `cap_drop: [ALL]`, `no-new-privileges`.
- Tokens and credential contents are never logged.

## Tests

```bash
python tests/test_logic.py
```

## Repository layout

```
app/
  main.py            # Streamlit UI (4 tabs)
  config.py          # env-driven configuration
  security.py        # APP_PASSWORD login gate
  utils.py           # size/speed/time formatting
  services/          # pure Python logic (no Streamlit imports)
scripts/authorize.py # generate token.json via a local browser (optional)
tests/test_logic.py  # plain assert-based tests (no pytest needed)
secrets/             # credentials.json + token.json (gitignored)
data/                # sync_history.db + drive_cache.json (gitignored)
```

## Troubleshooting

- **Missing `credentials.json`** -> download the OAuth client (Desktop app) and
  put it in `secrets/`.
- **Seagate drive not found** -> check `SEAGATE_MOUNT` in `.env` and that the
  drive is mounted.
- **`access_denied` during consent** -> add your email to *Test users* on the
  OAuth consent screen.
- **Token expired / refresh error** -> sign out of Google in the sidebar and
  sign in again.
