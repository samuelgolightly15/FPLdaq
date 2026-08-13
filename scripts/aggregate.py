#!/usr/bin/env python3
"""
Turn the raw hourly snapshots into the JSON the website reads.

Reads : data/snapshots/*.jsonl  and  data/players.json
Writes: docs/data/core.json  and  docs/data/full.json

Everything is expressed as a SHARE of the whole market rather than as a cash
market cap. For player i at one point in time:

    share_i = price_i * owners_i / sum over all players of (price * owners)

Owners are ownership percentage times the number of managers, and that
manager count is identical for every player at a given hour, so it cancels
top and bottom. Share is therefore completely unaffected by people
registering new teams, which was distorting the cash version badly: the
manager count roughly triples between the API opening and the first deadline.

Shares are stored as parts per million (110,000 = 11%) so they stay integers.

Resolution is mixed on purpose. Every hour is kept for the last RECENT_DAYS,
and older history is thinned to one point per UTC day. Without this the file
grows by about 14,000 numbers a day and stops being loadable on a phone.
"""

import json
import os
import glob
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_GLOB = os.path.join(ROOT, "data", "snapshots", "*.jsonl")
PLAYERS_PATH = os.path.join(ROOT, "data", "players.json")
CORE_PATH = os.path.join(ROOT, "docs", "data", "core.json")
FULL_PATH = os.path.join(ROOT, "docs", "data", "full.json")

RECENT_DAYS = 7
CORE_PLAYERS = 40

# The template squad: how many of each position a manager actually picks.
# element_type 1 = GK, 2 = DEF, 3 = MID, 4 = FWD.
TEMPLATE_SLOTS = {1: 2, 2: 5, 3: 5, 4: 3}

# element_type 1 = goalkeeper, 2 = defender, 3 = midfielder, 4 = forward.
# Keepers and defenders are shown together: both are bought for clean sheets,
# and most clubs have only two or three owned keepers at any time.
POS_GROUPS = [("gd", (1, 2)), ("mid", (3,)), ("fwd", (4,))]
POS_OF = {p: name for name, ps in POS_GROUPS for p in ps}


def load_snapshots():
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
                    continue  # truncated final line
                by_time[snap["t"]] = snap
    return [by_time[t] for t in sorted(by_time)]


def parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def choose_times(snaps):
    """Hourly for the last RECENT_DAYS, one point per day before that."""
    latest = parse(snaps[-1]["t"])
    cutoff = latest - timedelta(days=RECENT_DAYS)

    keep, last_of_day = [], {}
    for snap in snaps:
        stamp = parse(snap["t"])
        if stamp >= cutoff:
            keep.append(snap["t"])
        else:
            last_of_day[stamp.date()] = snap["t"]
    return sorted(set(list(last_of_day.values()) + keep))


def find_deadlines(snaps, times):
    """
    Deadlines that have already passed, and where each one falls in `times`.

    Snapshots carry the next deadline the game is counting down to. When that
    value changes, the previous one has passed. Snapshots taken before the
    collector recorded deadlines simply contribute nothing.
    """
    seen, passed = None, []
    for snap in snaps:
        d = snap.get("deadline")
        if d and d != seen:
            if seen is not None:
                passed.append(seen)
            seen = d

    out = []
    for d in passed:
        idx = next((i for i, t in enumerate(times) if t >= d), None)
        if idx is not None:
            out.append({"time": d, "index": idx})
    return out


def find_flows(players, managers, times, hours=24, top=12):
    """
    Who gained and lost the most money over the last `hours`.

    Measured in the same unit as everything else on the page: price times
    ownership fraction, which is the money the average manager holds in that
    player. Differencing it over a day gives the money moving in or out.

    Ownership is a percentage, so the millions of managers registering before
    the deadline do not inflate this. Price changes do move it, which is
    correct: a player getting more expensive genuinely ties up more of a
    squad. Reported in thousands of pounds.
    """
    if len(times) < 2:
        return {"hours": hours, "from": times[-1] if times else None, "in": [], "out": []}

    target = parse(times[-1]) - timedelta(hours=hours)
    start = 0
    for i, t in enumerate(times):
        if parse(t) <= target:
            start = i
        else:
            break

    def cash(p, i):
        """Money the average manager holds, in thousands of pounds."""
        if p["price"][i] is None or p["own"][i] is None or not managers[i]:
            return None
        return (p["price"][i] / 10) * (p["own"][i] * 1000 / managers[i]) * 1000

    moves = []
    for code, p in players.items():
        a, z = cash(p, start), cash(p, -1)
        if a is None or z is None:
            continue
        moves.append(
            {
                "n": p["n"],
                "t": p["t"],
                "d": round(z - a, 1),
                "to": round(z),
                "pr": p["price"][-1],
            }
        )

    moves.sort(key=lambda m: m["d"])
    out = [m for m in moves if m["d"] < 0][:top]
    gain = [m for m in reversed(moves) if m["d"] > 0][:top]
    return {"hours": hours, "from": times[start], "in": gain, "out": out}


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
    players = {}
    clubs = {
        tid: {
            "share": [None] * n,
            "cash": [None] * n,
            "pos": {name: [None] * n for name, _ in POS_GROUPS},
        }
        for tid in teams
    }

    for snap in snaps:
        i = index.get(snap["t"])
        if i is None:
            continue
        managers[i] = snap["total_players"]

        # Raw weights first. The manager count cancels in the ratio, so it is
        # left out entirely rather than multiplied in and divided back out.
        weights = {c: (el[0] / 10) * el[1] for c, el in snap["e"].items()}
        total = sum(weights.values())
        if total <= 0:
            continue

        club_acc = {tid: {name: 0.0 for name, _ in POS_GROUPS} for tid in teams}
        cash_acc = {tid: 0.0 for tid in teams}

        for code, w in weights.items():
            share = w / total * 1_000_000
            el = snap["e"][code]
            cost, pct = el[0], el[1]
            pts = el[2] if len(el) > 2 else None

            p = players.get(code)
            if p is None:
                m = players_meta.get(code, {})
                p = players[code] = {
                    "n": m.get("name", code),
                    "t": m.get("team", 0),
                    "p": m.get("pos", 0),
                    "share": [None] * n,
                    "own": [None] * n,
                    "price": [None] * n,
                    "pts": [None] * n,
                }
            p["share"][i] = round(share)
            p["own"][i] = round(pct / 1000 * snap["total_players"] / 1000)
            p["price"][i] = cost
            p["pts"][i] = pts

            tid = str(players_meta.get(code, {}).get("team", 0))
            group = POS_OF.get(players_meta.get(code, {}).get("pos"))
            if tid in club_acc and group:
                club_acc[tid][group] += share
                # Money committed by the average manager: price times
                # ownership fraction. Summed over every player this comes to
                # roughly £100m, which is the FPL squad budget, so it reads
                # as "of the average £100m squad, this much sits here".
                # Stored in thousands of pounds.
                cash_acc[tid] += (cost / 10) * (pct / 1000) * 1000

        for tid, groups in club_acc.items():
            clubs[tid]["share"][i] = round(sum(groups.values()))
            clubs[tid]["cash"][i] = round(cash_acc[tid])
            for name, _ in POS_GROUPS:
                clubs[tid]["pos"][name][i] = round(groups[name])

    def last(a):
        for v in reversed(a):
            if v is not None:
                return v
        return 0

    latest = {c: last(p["share"]) for c, p in players.items()}
    ranked = sorted(latest, key=lambda c: -latest[c])

    # The most invested-in players in each position, which is as close as
    # ownership alone gets to the template team.
    template = {
        str(pos): [c for c in ranked if players[c]["p"] == pos][:slots]
        for pos, slots in TEMPLATE_SLOTS.items()
    }

    # Core must carry every template pick, otherwise the default view would
    # have to wait on full.json to draw a defender ranked outside the top 40.
    core_codes = list(
        dict.fromkeys(ranked[:CORE_PLAYERS] + [c for cs in template.values() for c in cs])
    )

    header = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "units": {
            "share": "parts per million of total market cap",
            "cash": "thousands of pounds of the average manager's squad",
            "own": "thousands of managers",
            "price": "tenths of a million",
        },
        "times": times,
        "managers": managers,
        "deadlines": find_deadlines(snaps, times),
    }

    core = dict(header)
    core["teams"] = teams
    core["template"] = template
    core["flows"] = find_flows(players, managers, times)
    core["clubs"] = clubs
    core["players"] = {c: players[c] for c in core_codes}
    core["directory"] = {c: [p["n"], p["t"], p["p"], latest[c]] for c, p in players.items()}

    full = dict(header)
    full["players"] = {c: p for c, p in players.items() if c not in core_codes}

    os.makedirs(os.path.dirname(CORE_PATH), exist_ok=True)
    for path, payload in ((CORE_PATH, core), (FULL_PATH, full)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

    print(
        f"{len(players)} players, {len(clubs)} clubs, {n} points, "
        f"{len(header['deadlines'])} deadline(s) passed\n"
        f"  core.json {os.path.getsize(CORE_PATH)/1024:>7.0f} KB\n"
        f"  full.json {os.path.getsize(FULL_PATH)/1024:>7.0f} KB"
    )


if __name__ == "__main__":
    main()
