"""The first day a routine run reads, so a gap closes by itself.

A routine run reads a few days back (LOOKBACK_DAYS), enough when it runs
every day.  When it has not run for longer (the owner's laptop, which runs
the jobs, was off), the days between the last one on file and that window
would stay empty: start(path, usual) moves the start back to the last day
on file, at most MAX_GAP_DAYS before the usual start.

The last day on file is the latest any series of the file (compact-v1,
etl/compact.py: {"start": "YYYY-MM-DD", "step": minutes, "values": [...]}
or "days": [[...], ...]) reaches; a series that stopped long ago does not
pull the start back.
"""
import gzip
import json
import math
import os
from datetime import date, timedelta

MAX_GAP_DAYS = 30


def _series_end(node):
    start, step = node.get("start"), node.get("step")
    if not isinstance(start, str) or len(start) < 10:
        return None
    try:
        first = date.fromisoformat(start[:10])
    except ValueError:
        return None
    if isinstance(node.get("days"), list):
        count = len(node["days"])
    elif isinstance(node.get("values"), list) and isinstance(step, (int, float)) and step > 0:
        count = math.ceil(len(node["values"]) * step / 1440)
    else:
        return None
    return first + timedelta(days=count - 1) if count else None


def last_day(path):
    """The latest day any series of the file at path reaches; None without one."""
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    latest, stack = None, [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            end = _series_end(node)
            if end is not None:
                latest = end if latest is None or end > latest else latest
                continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))
    return latest


def start(path, usual):
    """usual, or the last day on file when that is earlier (by MAX_GAP_DAYS at most)."""
    last = last_day(path) if os.path.exists(path) else None
    if last is None or last >= usual:
        return usual
    first = max(last, usual - timedelta(days=MAX_GAP_DAYS))
    print(f"Catching up: {path} ends on {last}, so reading from {first} (not {usual})")
    return first
