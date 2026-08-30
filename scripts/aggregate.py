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
import sys
import glob
# model_squad sits alongside this file. The path is added explicitly rather
# than relying on the interpreter's default, and a missing module degrades to
# "no modelled squad" instead of taking the whole run down with it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import model_squad as MS
except ImportError:
    MS = None
    print(
        "WARNING: scripts/model_squad.py not found. The FPLdaq Template, the "
        "pitch view and the solid/dotted lines will all be empty until it is "
        "added to the scripts folder.",
        file=sys.stderr,
    )
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
    # One line per gameweek per timestamp, ordered and deduplicated.
    seen = {}
    for r in rows:
        seen[(r.get("event"), r.get("t"))] = r
    ordered = [seen[k] for k in sorted(seen, key=lambda k: (k[0] or 0, k[1] or ""))]

    # Readings pile up fast: a gameweek's matches span three days and are
    # polled every ten minutes. The two most recent gameweeks keep every
    # reading, so the live line stays smooth. Older ones are cut back to the
    # moments that actually mean something, half-time and full-time of each
    # kick-off slot, which preserves the step shape of a gameweek far better
    # than sampling at a fixed interval would.
    by_event = {}
    for r in ordered:
        by_event.setdefault(r.get("event"), []).append(r)

    kickoffs = kickoffs_by_event()
    recent = sorted(e for e in by_event if e is not None)[-2:]

    out = []
    for event in sorted(by_event, key=lambda e: e or 0):
        group = by_event[event]
        if event in recent or len(group) <= 13:
            out.extend(group)
        else:
            out.extend(match_phases(group, kickoffs.get(event, [])))
    return out


def kickoffs_by_event():
    """Distinct kick-off times per gameweek, from the cached fixture list."""
    path = os.path.join(ROOT, "data", "schedule.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            schedule = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}

    out = {}
    for f in schedule.get("fixtures", []):
        ko, event = f.get("kickoff"), f.get("event")
        if not ko or event is None:
            continue
        out.setdefault(event, set()).add(ko)
    return {e: sorted(v) for e, v in out.items()}


# Minutes after kick-off at which a match is halfway and finished. Full time
# allows for the interval and stoppage rather than a clean ninety.
PHASES = (("HT", 45), ("FT", 115))
PHASE_TOLERANCE_MINUTES = 25


def match_phases(rows, kickoff_times):
    """
    The readings nearest half-time and full-time of each kick-off slot.

    Falls back to even sampling when no fixture times are available, so a
    missing schedule thins the data rather than dropping it.
    """
    if not kickoff_times:
        step = max(1, len(rows) // 12)
        thinned = rows[::step]
        if thinned[-1] is not rows[-1]:
            thinned.append(rows[-1])
        return thinned

    stamped = []
    for r in rows:
        try:
            stamped.append((parse(r["t"]), r))
        except (KeyError, ValueError):
            continue
    if not stamped:
        return rows

    picked = {}
    for ko in kickoff_times:
        try:
            start = parse(ko.replace("Z", "").split("+")[0] + "Z")
        except ValueError:
            continue
        for label, minutes in PHASES:
            target = start + timedelta(minutes=minutes)
            best, gap = None, timedelta(minutes=PHASE_TOLERANCE_MINUTES)
            for when, row in stamped:
                delta = abs(when - target)
                if delta <= gap:
                    best, gap = row, delta
            if best is not None:
                picked[best["t"]] = dict(best, k=label)

    # The last reading is always kept: it is the only one with bonus confirmed.
    last = stamped[-1][1]
    picked.setdefault(last["t"], dict(last, k="close"))
    return [picked[t] for t in sorted(picked)]


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


def fixtures_for(event_id):
    """Cached fixtures for one gameweek, or an empty list."""
    path = os.path.join(ROOT, "data", "schedule.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            schedule = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []
    return [f for f in schedule.get("fixtures", []) if f.get("event") == event_id]


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

    # A gameweek's points are normally the difference between its own freeze
    # and the next one, which means waiting a week. Once every fixture is
    # confirmed the totals stop moving, so the closing reading can be taken
    # far sooner. FPL settles bonus within about an hour of the last whistle;
    # 10am the following day is a generous margin.
    def settled_cutoff(event_id):
        kickoffs = [
            parse(f["kickoff"].replace("Z", "").split("+")[0] + "Z")
            for f in fixtures_for(event_id)
            if f.get("kickoff")
        ]
        if not kickoffs or not all(f["confirmed"] for f in fixtures_for(event_id)):
            return None
        day_after = (max(kickoffs) + timedelta(days=1)).date()
        return datetime(day_after.year, day_after.month, day_after.day,
                        10, 0, tzinfo=timezone.utc)

    def points_at(stamp):
        """Cumulative points per player at a given snapshot time."""
        for snap in snaps:
            if snap["t"] == stamp:
                return {c: (el[2] if len(el) > 2 else 0) for c, el in snap["e"].items()}
        return None

    def points_after(cutoff):
        """Cumulative points at the first snapshot on or after `cutoff`."""
        if datetime.now(timezone.utc) < cutoff:
            return None
        for snap in snaps:
            if parse(snap["t"]) >= cutoff:
                return {c: (el[2] if len(el) > 2 else 0) for c, el in snap["e"].items()}
        return None

    out = {}
    for i, w in enumerate(frozen):
        start = points_at(w["frozen_from"])
        if start is None:
            continue

        if i + 1 < len(frozen):
            # The next deadline's freeze is the cleanest closing reading.
            end = points_at(frozen[i + 1]["frozen_from"])
        else:
            # No next freeze yet. If the gameweek has settled, close it on the
            # first snapshot taken after 10am the day after its last match,
            # which is deterministic and does not drift as more arrive.
            cutoff = settled_cutoff(w["event"])
            end = points_after(cutoff) if cutoff else None
        if end is None:
            continue
        # total_points is cumulative, so a gameweek's points can never be
        # negative. If they are, the two readings being differenced are not
        # what they are assumed to be: most often one predates points being
        # collected at all and reports zero for everyone. Publishing that
        # would put a large negative step on the chart, so the gameweek is
        # refused instead.
        diffs = {c: end[c] - start.get(c, 0) for c in end}

        def share_negative(d):
            return sum(1 for v in d.values() if v < 0) / max(1, len(d))

        # FPL carries the previous season's total_points on bootstrap-static
        # right up to the first deadline, then resets them. Differencing a
        # pre-deadline snapshot against a post-gameweek one therefore shows
        # nearly every established player going backwards by a whole season.
        # It happens exactly once, so it is recognised rather than treated as
        # corruption: after a reset the correct baseline is zero.
        if i == 0 and share_negative(diffs) > 0.25:
            print(
                f"GW{w['event']}: {sum(1 for v in diffs.values() if v < 0)} of "
                f"{len(diffs)} players went backwards, which is the season "
                f"rollover in total_points. Rebasing this gameweek to zero."
            )
            diffs = dict(end)

        # A few negatives are ordinary football: a red card, own goals or a
        # missed penalty can leave a player on negative points for a gameweek,
        # and a cumulative total can be negative early in a season. Only a
        # broad pattern of players going backwards indicates the two readings
        # are mismatched.
        if share_negative(diffs) > 0.10:
            closing = (
                frozen[i + 1]["frozen_from"] if i + 1 < len(frozen)
                else "the first settled reading"
            )
            worst = sorted(diffs, key=lambda c: diffs[c])[:5]
            detail = ", ".join(f"{c} {start.get(c, 0)}->{end[c]}" for c in worst)
            print(
                f"GW{w['event']}: {share_negative(diffs)*100:.0f}% of players "
                f"went backwards between {w['frozen_from']} and {closing}. "
                f"Worst: {detail}. Refusing to publish.",
                file=sys.stderr,
            )
            continue

        total = sum(diffs.values())
        if total <= 0:
            print(
                f"GW{w['event']}: total points came to {total}, which cannot be "
                f"right. Skipping.",
                file=sys.stderr,
            )
            continue

        out[str(w["event"])] = {
            "capital": w["capital"],
            "squad": w.get("squad"),
            "pts": {c: v for c, v in diffs.items() if v},
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
    if not total or MS is None:
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
    def solve_at(i):
        if MS is None:
            return set()
        """The FPLdaq Template as it stood at reading i."""
        total = managers[i]
        if not total:
            return set()
        cands, capital = [], 0.0
        for c, p in players.items():
            own_k, price = p["own"][i], p["price"][i]
            meta = players_meta.get(c)
            if own_k is None or price is None or not meta:
                continue
            frac = own_k * 1000 / total
            if frac <= 0 or meta.get("pos") not in MS.SLOTS:
                continue
            capital += frac * price / 10
            cands.append({"code": c, "name": meta.get("name", c), "pos": meta["pos"],
                          "team": meta.get("team", 0), "own": frac, "price": price})
        budget = int(round(capital * 10))
        squad = MS.improve(MS.greedy(cands, budget), cands, budget)
        return {p["code"] for p in squad}

    membership = [solve_at(i) for i in range(n)]
    for c in core_codes:
        players[c]["tpl"] = [1 if c in membership[i] else 0 for i in range(n)]

    # Money in each position per player slot, which is what makes positions
    # comparable: five defenders naturally hold more in total than two
    # keepers, but the per-slot figure says which is actually the expensive
    # place to be. In thousands of pounds of the average manager's squad.
    SLOTS = MS.SLOTS if MS else {1: 2, 2: 5, 3: 5, 4: 3}
    per_slot = {str(pos): [None] * n for pos in SLOTS}
    for i in range(n):
        total = managers[i]
        if not total:
            continue
        sums = {pos: 0.0 for pos in SLOTS}
        for c, p in players.items():
            own_k, price = p["own"][i], p["price"][i]
            pos = players_meta.get(c, {}).get("pos")
            if own_k is None or price is None or pos not in sums:
                continue
            sums[pos] += (price / 10) * (own_k * 1000 / total)
        for pos, slots in SLOTS.items():
            per_slot[str(pos)][i] = round(sums[pos] / slots * 1000)
    core["per_slot"] = per_slot
    core["slots"] = {str(k): v for k, v in SLOTS.items()}

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

    if MS is None:
        print("model_squad missing -> model_now is null, tpl flags omitted")
    print(
        f"{len(players)} players, {len(clubs)} clubs, {n} points, "
        f"{len(header['deadlines'])} deadline(s) passed\n"
        f"  core.json {os.path.getsize(CORE_PATH)/1024:>7.0f} KB\n"
        f"  full.json {os.path.getsize(FULL_PATH)/1024:>7.0f} KB"
    )


if __name__ == "__main__":
    main()
