# Local <-> Google Drive Sync

A self-hosted Streamlit web app that **compares and syncs** files between a
locally mounted drive (e.g. a Seagate external disk) and **Google Drive**
(My Drive), deployed as a hardened Docker Compose service.

Both sides are scoped freely from the sidebar: any Drive folder against any
subfolder inside the mounted drive. The UI is in Vietnamese; the primary use
case is one-way download (Drive -> local), but upload and two-way sync are
fully supported.

## Features

- **Scan & compare** both sides by relative path (same path + same size =
  identical), with live progress and a Stop button. From the second scan
  onward the Drive side only asks for **what changed** (Changes API), so a
  rescan takes seconds instead of minutes.
- **Explorer** tab: after a scan, browse the merged folder tree of both sides
  — drill into folders, see per-folder difference counts, filter by side or
  show differences only.
- **Sync plan as a dry run**: pick direction (download / upload / two-way,
  defaults to download) and conflict policy (newer wins / force / skip), then
  preview every action before anything runs. Optional **mirror** mode deletes
  files missing on the source side and requires typing `XOA` to confirm.
- **Google-native files** (Docs/Sheets/Slides) are skipped by default; an
  optional one-way **export** saves `.docx`/`.xlsx`/`.pptx`/`.png` copies to
  the local drive and refreshes them whenever the Drive version is newer. The
  copies are never uploaded back and the Drive originals are never touched.
- **Background execution** with live progress: files, bytes, speed, ETA,
  cancel button, and parallel transfers (`SYNC_WORKERS`, default 4). Dropped
  connections mid-transfer (e.g. `IncompleteRead`) are retried automatically
  with exponential backoff.
- **Storage at a glance**: sidebar gauges show used/total space of both the
  local drive and the Google Drive quota (including Trash usage); the plan
  warns when a download would not fit in the remaining free space.
- **History**: every sync session is recorded in SQLite and exportable as CSV.
- **Safety**: deletions are always recoverable (Drive -> Trash, local ->
  `.sync_trash/<timestamp>/` on the same drive); nothing is ever hard-deleted.
  Downloads write to a `.syncpart` file first and are renamed atomically.

## Repository layout

```
app/
  main.py              # Streamlit UI (5 tabs: Compare / Explore / Sync / History / Guide)
  config.py            # env-driven paths & constants
  security.py          # APP_PASSWORD login gate
  utils.py             # human-readable size/speed/ETA formatting
  services/            # pure Python logic (no Streamlit imports)
    common.py          #   SyncCancelled exception
    scanner.py         #   local scan, disk usage, subfolder resolver
    gdrive.py          #   Drive API: OAuth, listing, transfers, export, quota
    compare.py         #   comparison engine + explorer folder listing
    drive_cache.py     #   cached Drive listing + changes token (incremental scans)
    scan.py            #   background scan thread (local + Drive + compare)
    sync.py            #   plan builder + background sync thread with progress
    history.py         #   SQLite session history
scripts/authorize.py   # optional: generate secrets/token.json on the host
tests/test_logic.py    # plain assert-based tests (no pytest needed)
.streamlit/config.toml # Streamlit toolbar/theme settings
Dockerfile             # Python 3.12 slim image, non-root
docker-compose.yml     # hardened service (localhost-only, read-only rootfs)
.env.example           # configuration template (copy to .env)
requirements.txt       # pinned dependencies
secrets/               # credentials.json + token.json (gitignored)
data/                  # sync_history.db + drive_cache.json (gitignored)
```

## Requirements

- Docker + Docker Compose (recommended), or Python 3.12 for local development.
- A drive (or any folder) mounted on the host.
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

The container only sees what `SEAGATE_MOUNT` points to — everything outside
that path is invisible to the app by construction.

## 3. Local development (no Docker)

```bash
pip install -r requirements.txt
SEAGATE_PATH=/path/to/drive streamlit run app/main.py

# tests
python tests/test_logic.py
```

## Connecting Google Drive

In the sidebar, click **Dang nhap voi Google** (Sign in with Google). Google
opens its consent page and then redirects straight back to the app with the
authorization code — nothing to copy or paste. The token is saved to
`secrets/token.json` (chmod 600) and refreshes automatically.

Alternative: `python scripts/authorize.py` generates `secrets/token.json` on
the host using your browser — useful before the first container start.

The app is not Google-verified, so the consent page shows a warning; click
**Advanced -> Go to app** to continue. To switch accounts, sign out in the
sidebar (this also clears the scan cache) and sign in again.

## Usage

1. **Compare** — in the sidebar pick the scope: the Drive root folder (empty =
   the whole My Drive) and the local subfolder (empty = the whole drive; a
   not-yet-existing subfolder is created on the first download). Click
   *Quet & So sanh*. If a fast incremental scan ever looks wrong, click
   *Quet lai toan bo* (full rescan). Stopping mid-scan is always safe —
   scanning only reads.
2. **Explore** — browse the merged tree of both sides, folder by folder, with
   per-folder difference counts and side/difference filters.
3. **Sync** — pick a direction and conflict policy -> *Lap ke hoach* to
   preview the plan -> *Bat dau dong bo*. Mirror mode requires typing `XOA`.
   The *Xuat file Google* checkbox (download directions only) exports
   Docs/Sheets/Slides as Office copies.
4. **History** — review past sessions, download CSV.

> **Do not modify either side** while a sync is running.

## Environment variables

| Variable             | Default                  | Purpose                                                          |
| -------------------- | ------------------------ | ---------------------------------------------------------------- |
| `SEAGATE_MOUNT`      | *(required, Docker)*     | Host path bind-mounted into the container at `/data/seagate`     |
| `SEAGATE_PATH`       | `/data/seagate`          | Path the app scans (inside the container / in local dev)         |
| `SEAGATE_SUBDIR`     | `googledrive`            | Subfolder preselected in the UI; set empty for the whole drive   |
| `APP_PASSWORD`       | *(empty)*                | UI login password (empty = no gate, a warning is shown)          |
| `DRIVE_ROOT_FOLDER`  | `root`                   | Drive folder to compare against (`root` = entire My Drive)       |
| `SYNC_WORKERS`       | `4`                      | Parallel transfer workers during sync (1 = sequential)           |
| `OAUTH_REDIRECT_URI` | `http://localhost:8501/` | The app's own URL, used as the OAuth redirect                    |
| `SECRETS_DIR`        | `./secrets`              | Location of `credentials.json` + `token.json`                    |
| `DATA_DIR`           | `./data`                 | Location of `sync_history.db` and the Drive scan cache           |
| `TZ`                 | —                        | Timezone, e.g. `Asia/Ho_Chi_Minh`                                |

## How the comparison works

- Files are matched by **relative path**; same path + same size = identical.
  No content hashing — fast, and reliable together with preserved mtimes.
- **mtime is preserved in both directions** (upload sets Drive's
  `modifiedTime`, download sets the local mtime), so "newer wins" is
  trustworthy from the first sync onward. A 2-second tolerance absorbs
  filesystem timestamp differences.
- The Drive listing is **one flat paginated sweep** (about 1 API call per
  1000 items) rebuilt into a tree in memory; later scans use the **Changes
  API** with a saved token, so only modified items are fetched. The cache is
  tied to the signed-in account and cleared on logout.
- Google-native files have no stable binary form; they are compared only via
  the optional export copies (matched as `<name>.docx` etc., refreshed when
  the Drive `modifiedTime` is newer).
- Each parallel worker uses its own Drive client (the underlying HTTP library
  is not thread-safe); folder creation is serialized to avoid duplicates.

## Data safety

- **Never hard-deletes**: Drive -> **Trash**; local -> `.sync_trash/<timestamp>/`.
- **Mirror** works only for one-way syncs and requires typing `XOA` to confirm.
- Export copies are excluded from uploads and mirror deletions.
- Downloads are atomic (`.syncpart` + rename) — a crash never leaves a half
  file in place.

## Deployment security

- The port binds to `127.0.0.1:8501` only. For remote access put a **reverse
  proxy** (HTTPS + auth) in front; do **not** switch to `0.0.0.0`.
- The container runs **non-root** (UID 1000) with a **read-only** rootfs +
  tmpfs `/tmp`, `cap_drop: [ALL]`, and `no-new-privileges`.
- The login gate compares passwords with `hmac.compare_digest` and delays
  failed attempts. Tokens and credential contents are never logged.

## Troubleshooting

- **Missing `credentials.json`** -> download the OAuth client (Desktop app)
  and put it in `secrets/`.
- **Drive not found** -> check `SEAGATE_MOUNT` in `.env` and that the drive
  is mounted; changing the mount requires `docker compose up -d`.
- **`access_denied` during consent** -> add your email to *Test users* on the
  OAuth consent screen.
- **Token expired / refresh error** -> sign out of Google in the sidebar and
  sign in again.
- **A fast scan looks stale** -> click *Quet lai toan bo* to force a full
  listing.
