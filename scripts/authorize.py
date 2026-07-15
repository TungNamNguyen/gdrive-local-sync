#!/usr/bin/env python3
"""Host-side helper to generate secrets/token.json via a real browser.

The Streamlit app signs in through its own web redirect flow, but this script
is a convenient alternative on a normal desktop: it opens a browser, captures
the OAuth code on a local loopback server (port 8090), and writes
secrets/token.json (which the container then mounts).

Usage (from the repo root):

    python scripts/authorize.py

Requires secrets/credentials.json (OAuth client, Desktop app type) to exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the app package importable (config.py / services live under app/).
_APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(_APP_DIR))

import config  # noqa: E402
from services.gdrive import REDIRECT_URI, save_credentials  # noqa: E402  (sets OAUTHLIB_* env)

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402


def main() -> int:
    config.ensure_dirs()
    if not config.CREDENTIALS_FILE.exists():
        print(f"Khong thay {config.CREDENTIALS_FILE}.")
        print("Tai OAuth client (Desktop app) tu Google Cloud Console va dat vao do.")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.CREDENTIALS_FILE), scopes=config.SCOPES, redirect_uri=REDIRECT_URI
    )
    # Opens the default browser and runs a loopback server on port 8090 to
    # capture the authorization code automatically.
    creds = flow.run_local_server(port=8090, prompt="consent", open_browser=True)
    save_credentials(creds)
    print(f"Da luu token vao {config.TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
