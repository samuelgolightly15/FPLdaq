#!/usr/bin/env python3
"""
Turn the raw hourly snapshots into one small JSON file for the website.

Reads : data/snapshots/*.jsonl  and  data/players.json
Writes: docs/data/index.json

Resolution is mixed on purpose. Every hour is kept for the last RECENT_DAYS,
and older history is thinned to one point per UTC day (the last snapshot of
that day). Without this the file grows without limit: 577 players times one
number per hour is about 14k numbers a day, which is fine in August and
unusable by Christmas.

Numbers are stored as integers to keep the file small:

  cap    thousands of pounds   (price x owners)
  own    thousands of managers
  price  tenths of a million   (60 = 6.0m)

A null means no snapshot for that hour, which happens whenever a scheduled
run was skipped. The site draws straight through those gaps.
"""

import json
import os
import glob
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_GLOB = os.path.join(ROOT, "data", "snapshots", "*.jsonl")
PLAYERS_PATH = os.path.join(ROOT, "data", "players.json")
OUT_PATH = os.path.join(ROOT, "docs", "data", "core.json")
FULL_PATH = os.path.join(ROOT, "docs", "data", "full.json")

RECENT_DAYS = 7
CORE_PLAYERS = 40


def load_snapshots():
    """Every snapshot, de-duplicated by timestamp, oldest first."""
    by_time = {}
    for path in sorted(glob.glob(SNAP_GLOB)):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a truncated final line; skip it
                by_time[snap["t"]] = snap
    return [by_time[t] for t in sorted(by_time)]


def choose_times(snaps):
    """Hourly for the last RECENT_DAYS, one per day before that."""
    if not snaps:
        return []

    latest = datetime.strptime(snaps[-1]["t"], "%Y-%m-%dT%H:%M:%SZ")
    cutoff = latest.replace(tzinfo=timezone.utc) - timedelta(days=RECENT_DAYS)

    keep, last_of_day = [], {}
    for snap in snaps:
        stamp = datetime.strptime(snap["t"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        if stamp >= cutoff:
            keep.append(snap["t"])
        else:
            last_of_day[stamp.date()] = snap["t"]

    return sorted(set(list(last_of_day.values()) + keep))


def main():
    snaps = load_snapshots()
    if not snaps:
        raise SystemExit("No snapshots found. Run scripts/collect.py first.")

    with open(PLAYERS_PATH, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    players_meta, teams = meta["players"], meta["teams"]

    times = choose_times(snaps)
    index = {t: i for i, t in enumerate(times)}
    n = len(times)

    managers = [None] * n
    for snap in snaps:
        i = index.get(snap["t"])
        if i is not None:
            managers[i] = snap["total_players"]

    # Every player who has ever appeared, so search covers the whole game.
    codes = sorted({c for snap in snaps for c in snap["e"]})

    players = {
        c: {
            "n": players_meta.get(c, {}).get("name", c),
            "t": players_meta.get(c, {}).get("team", 0),
            "p": players_meta.get(c, {}).get("pos", 0),
            "cap": [None] * n,
            "own": [None] * n,
            "price": [None] * n,
        }
        for c in codes
    }

    clubs = {
        tid: {"cap": [0] * n, "own": [0] * n, "seen": [False] * n} for tid in teams
    }

    for snap in snaps:
        i = index.get(snap["t"])
        if i is None:
            continue
        total = snap["total_players"]

        for code, (cost, pct) in snap["e"].items():
            owners = pct / 1000 * total
            cap = owners * cost / 10

            p = players[code]
            p["cap"][i] = round(cap / 1000)
            p["own"][i] = round(owners / 1000)
            p["price"][i] = cost

            tid = str(players_meta.get(code, {}).get("team", 0))
            if tid in clubs:
                clubs[tid]["cap"][i] += cap / 1000
                clubs[tid]["own"][i] += owners / 1000
                clubs[tid]["seen"][i] = True

    for tid, club in clubs.items():
        club["cap"] = [
            round(v) if seen else None for v, seen in zip(club["cap"], club["seen"])
        ]
        club["own"] = [
            round(v) if seen else None for v, seen in zip(club["own"], club["seen"])
        ]
        del club["seen"]

    # The site loads core.json on open and full.json only when someone
    # searches, so the first paint stays fast on a phone. Core carries the
    # clubs, the current top CORE_PLAYERS, and a name/team entry for every
    # player so search can offer the whole game immediately.
    latest_cap = {c: (p["cap"][-1] or 0) for c, p in players.items()}
    core_codes = sorted(latest_cap, key=lambda c: -latest_cap[c])[:CORE_PLAYERS]

    header = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "units": {
            "cap": "thousands of pounds",
            "own": "thousands of managers",
            "price": "tenths of a million",
        },
        "times": times,
        "managers": managers,
    }

    core = dict(header)
    core["teams"] = teams
    core["clubs"] = clubs
    core["players"] = {c: players[c] for c in core_codes}
    core["directory"] = {
        c: [p["n"], p["t"], p["p"], latest_cap[c]] for c, p in players.items()
    }

    full = dict(header)
    full["players"] = {c: players[c] for c in players if c not in core_codes}

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    for path, payload in ((OUT_PATH, core), (FULL_PATH, full)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

    hourly = sum(1 for t in times if t >= times[-1][:10])
    print(
        f"{len(players)} players, {len(clubs)} clubs, {n} points "
        f"({hourly} today)\n"
        f"  core.json {os.path.getsize(OUT_PATH)/1024:>7.0f} KB "
        f"({len(core_codes)} players + directory)\n"
        f"  full.json {os.path.getsize(FULL_PATH)/1024:>7.0f} KB "
        f"({len(full['players'])} players, loaded on first search)"
    )


if __name__ == "__main__":
    main()
