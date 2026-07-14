"""Kieu dung chung giua cac service (tranh import vong)."""
from __future__ import annotations


class SyncCancelled(Exception):
    """Nguoi dung bam Huy — dung tac vu mot cach sach se."""
