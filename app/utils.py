"""Number-formatting helpers for the UI (sizes, speeds, durations)."""
from __future__ import annotations

import datetime as _dt

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def human_size(num: float | int | None) -> str:
    """1234567 -> '1.2 MB'. None -> ''."""
    if num is None:
        return ""
    n = float(num)
    for unit in _UNITS:
        if abs(n) < 1024.0 or unit == _UNITS[-1]:
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} PB"


def human_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec <= 0:
        return "—"
    return f"{human_size(bytes_per_sec)}/s"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or seconds != seconds:  # NaN check
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}g {m:02d}p"
    if m:
        return f"{m}p {s:02d}s"
    return f"{s}s"


def ts_to_str(ts: float | None) -> str:
    """Unix timestamp -> local-time string 'YYYY-MM-DD HH:MM'."""
    if not ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""
