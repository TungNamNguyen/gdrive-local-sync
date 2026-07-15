"""Types shared between services (avoids circular imports)."""
from __future__ import annotations


class SyncCancelled(Exception):
    """The user pressed Cancel/Stop — end the task cleanly."""
