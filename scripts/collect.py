#!/usr/bin/env python3
"""
Take an hourly snapshot of FPL player price and ownership.

Writes two things under data/:

  snapshots/YYYY-MM-DD.jsonl   one JSON object per line, one line per hour
  players.json                 code -> name / team / position, plus the team map

Snapshot line format (kept short because there is one per hour, all season):

  {"t": "2026-08-09T14:00:00Z",
   "total_players": 3041555,
   "next_event": 1,
   "e": {"154561": [60, 309], ...}}

where each element is [now_cost, selected_by_percent * 10], both integers.
now_cost is in tenths of a million (60 = 6.0m). Ownership is stored as the
raw percentage the API gives, NOT a headcount: owners are derived later as
pct/1000 * total_players, so that total_players can be held fixed if you
want an index that is not dominated by new managers registering.

Keys are the player's permanent `code`, not the per-season `id`.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://fantasy.premierleague.com/api/bootstrap-static/"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SNAP_DIR = os.path.join(DATA, "snapshots")
PLAYERS_PATH = os.path.join(DATA, "players.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fpl-index/1.0)",
    "Accept": "application/json",
}


def fetch(attempts=4):
    """Fetch bootstrap-static, retrying with backoff. Raises if all fail."""
    req = urllib.request.Request(API, headers=HEADERS)
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except Exception as exc:
            if i == attempts - 1:
                raise
            wait = 5 * (2 ** i)
            print(f"attempt {i + 1} failed ({exc}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)


def hour_bucket():
    """The current UTC hour, floored. Cron can drift by 10-20 minutes."""
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def already_recorded(path, stamp):
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and json.loads(line).get("t") == stamp:
                return True
    return False


def build_snapshot(data, stamp):
    elements = {}
    for p in data["elements"]:
        if p.get("removed"):
            continue
        pct = int(round(float(p["selected_by_percent"]) * 10))
        elements[str(p["code"])] = [p["now_cost"], pct]

    next_event, deadline = None, None
    for ev in data.get("events", []):
        if ev.get("is_current"):
            next_event = ev["id"]
        if ev.get("is_next") or (next_event is None and not ev.get("finished")):
            if deadline is None:
                next_event = next_event or ev["id"]
                deadline = ev.get("deadline_time")

    return {
        "t": stamp,
        "total_players": data["total_players"],
        "next_event": next_event,
        "deadline": deadline,
        "e": elements,
    }


def build_players(data):
    teams = {
        str(t["id"]): {"name": t["name"], "short": t["short_name"]}
        for t in data["teams"]
    }
    players = {}
    for p in data["elements"]:
        if p.get("removed"):
            continue
        players[str(p["code"])] = {
            "name": p["web_name"],
            "full": f"{p['first_name']} {p['second_name']}".strip(),
            "team": p["team"],
            "pos": p["element_type"],
        }
    return {"teams": teams, "players": players}


def write_if_changed(path, payload):
    """Avoid a commit every hour for metadata that rarely moves."""
    new = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == new:
                return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


def main():
    os.makedirs(SNAP_DIR, exist_ok=True)

    bucket = hour_bucket()
    stamp = bucket.strftime("%Y-%m-%dT%H:00:00Z")
    path = os.path.join(SNAP_DIR, bucket.strftime("%Y-%m-%d") + ".jsonl")

    force = "--force" in sys.argv
    if already_recorded(path, stamp) and not force:
        print(f"{stamp} already recorded, nothing to do")
        return

    data = fetch()
    snapshot = build_snapshot(data, stamp)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")

    changed = write_if_changed(PLAYERS_PATH, build_players(data))

    print(
        f"{stamp}: {len(snapshot['e'])} players, "
        f"{snapshot['total_players']:,} managers, "
        f"metadata {'updated' if changed else 'unchanged'}"
    )


if __name__ == "__main__":
    main()
