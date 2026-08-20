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
import model_squad as MS
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_GLOB = os.path.join(ROOT, "data", "snapshots", "*.jsonl")
PLAYERS_PATH = os.path.join(ROOT, "data", "players.json")
CORE_PATH = os.path.join(ROOT, "docs", "data", "core.json")
FULL_PATH = os.path.join(ROOT, "docs", "data", "full.json")
WEIGHTS_DIR = os.path.join(ROOT, "data", "weights")

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


def load_performance(times):
    """
    The SWAPPI series, aligned to the market timeline where possible.

    Written by collect_live.py on its own schedule, which is far denser during
    matches than the market collector, so it is passed through as its own
    series rather than forced onto the market's timestamps.
    """
    path = os.path.join(ROOT, "data", "performance.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # One line per gameweek per timestamp; keep them ordered and deduplicated.
    seen = {}
    for r in rows:
        seen[(r.get("event"), r.get("t"))] = r
    return [seen[k] for k in sorted(seen, key=lambda k: (k[0] or 0, k[1] or ""))]


def load_models():
    """Modelled affordable squads, one per gameweek, if built."""
    d = os.path.join(ROOT, "data", "models")
    if not os.path.isdir(d):
        return {}
    out = {}
    for name in sorted(os.listdir(d)):
        if not name.startswith("gw") or not name.endswith(".json"):
            continue
        with open(os.path.join(d, name), "r", encoding="utf-8") as fh:
            m = json.load(fh)
        out[str(m["event"])] = {
            "overlap": m["overlap"],
            "spend": m["spend"],
            "budget": m["budget"],
            "squad": m["squad"],
            "xi": m["xi"],
            "bench": m["bench"],
        }
    return out


def gameweek_points(snaps, weights_dir):
    """
    Points scored by each player in each gameweek, plus that gameweek's
    ownership weights.

    The market collector stores cumulative total_points on every snapshot, so
    a gameweek's points are the difference between the reading at its deadline
    and the reading at the next one. Nothing extra needs collecting.

    Only gameweeks that have actually finished get a row, so a part-played
    gameweek never appears as a suspiciously low score.
    """
    if not os.path.isdir(weights_dir):
        return {}

    frozen = []
    for name in sorted(os.listdir(weights_dir)):
        if not name.startswith("gw") or not name.endswith(".json"):
            continue
        with open(os.path.join(weights_dir, name), "r", encoding="utf-8") as fh:
            frozen.append(json.load(fh))
    frozen.sort(key=lambda w: w["event"])
    if not frozen:
        return {}

    def points_at(stamp):
        """Cumulative points per player at a given snapshot time."""
        for snap in snaps:
            if snap["t"] == stamp:
                return {c: (el[2] if len(el) > 2 else 0) for c, el in snap["e"].items()}
        return None

    out = {}
    for i, w in enumerate(frozen):
        start = points_at(w["frozen_from"])
        # The next deadline's freeze marks the end of this gameweek. Without
        # one, the gameweek is still in progress.
        if start is None or i + 1 >= len(frozen):
            continue
        end = points_at(frozen[i + 1]["frozen_from"])
        if end is None:
            continue
        out[str(w["event"])] = {
            "capital": w["capital"],
            "squad": w.get("squad"),
            "pts": {
                c: end[c] - start.get(c, 0)
                for c in end
                if end[c] - start.get(c, 0) != 0
            },
            "w": w["weights"],
        }
    return out


def last_value(seq):
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def model_now(players, players_meta, managers):
    """
    The affordable modelled squad as of the latest reading.

    Runs the same optimiser the per-gameweek models use, but against current
    ownership and prices rather than a frozen deadline, so the pitch view has
    something to show before any deadline has passed.
    """
    total = last_value(managers)
    if not total:
        return None

    candidates, capital = [], 0.0
    for c, p in players.items():
        own_k, price = last_value(p["own"]), last_value(p["price"])
        meta = players_meta.get(c)
        if own_k is None or price is None or not meta:
            continue
        frac = own_k * 1000 / total          # thousands of managers -> fraction
        if frac <= 0:
            continue
        capital += frac * price / 10
        candidates.append({
            "code": c, "name": meta.get("name", c), "pos": meta.get("pos", 0),
            "team": meta.get("team", 0), "own": frac, "price": price,
        })

    candidates = [c for c in candidates if c["pos"] in MS.SLOTS]
    budget = int(round(capital * 10))
    squad = MS.improve(MS.greedy(candidates, budget), candidates, budget)
    if len(squad) < 15:
        return None

    xi, bench = MS.pick_xi(squad)
    return {
        "budget": round(budget / 10, 1),
        "spend": round(MS.spend(squad) / 10, 1),
        "overlap": round(MS.overlap(squad), 4),
        "unconstrained": round(MS.upper_bound(candidates), 4),
        "squad": [{"code": p["code"], "n": p["name"], "pos": p["pos"],
                   "team": p["team"], "own": round(p["own"], 6), "price": p["price"]}
                  for p in squad],
        "xi": [p["code"] for p in xi],
        "bench": [p["code"] for p in bench],
    }


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

    # Share of the average squad sitting in each position, over time. Derived
    # from what the club splits already hold, so it costs nothing extra.
    allocation = {name: [None] * n for name, _ in POS_GROUPS}
    for i in range(n):
        for name, _ in POS_GROUPS:
            total = sum(
                c["pos"][name][i] for c in clubs.values() if c["pos"][name][i] is not None
            )
            if any(c["pos"][name][i] is not None for c in clubs.values()):
                allocation[name][i] = total

    core = dict(header)
    core["allocation"] = allocation
    core["performance"] = load_performance(times)
    core["gameweeks"] = gameweek_points(snaps, WEIGHTS_DIR)
    core["models"] = load_models()
    # Template membership at every point in time, so the composition chart can
    # draw a player solid while they were in the template and dotted while they
    # were not. Only computed for players in core, which is where it is drawn.
    def template_at(i):
        out = set()
        for pos, slots in TEMPLATE_SLOTS.items():
            ranked_i = sorted(
                (c for c in players if players[c]["p"] == pos and players[c]["share"][i] is not None),
                key=lambda c: -players[c]["share"][i],
            )
            out.update(ranked_i[:slots])
        return out

    membership = [template_at(i) for i in range(n)]
    for c in core_codes:
        players[c]["tpl"] = [1 if c in membership[i] else 0 for i in range(n)]

    # A modelled affordable squad for right now, not just for past deadlines,
    # so the pitch view has something to show before the season starts.
    core["model_now"] = model_now(players, players_meta, managers)

    core["teams"] = teams
    # element id -> permanent code, so picks from the API can be joined to
    # everything else here. Absent for any player predating the id capture.
    core["ids"] = {
        str(m["id"]): c
        for c, m in players_meta.items()
        if m.get("id") is not None
    }
    core["template"] = template
    core["flows"] = find_flows(players, managers, times)
    core["clubs"] = clubs
    core["players"] = {c: players[c] for c in core_codes}
    # Full name is carried so search can match a first name: the display name
    # is often just the surname, or an initialled form like "B.Fernandes".
    core["directory"] = {
        c: [
            p["n"],
            p["t"],
            p["p"],
            latest[c],
            players_meta.get(c, {}).get("full", p["n"]),
        ]
        for c, p in players.items()
    }

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
