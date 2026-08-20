#!/usr/bin/env python3
"""
Collect the FPL performance index: ownership-weighted points, as they land.

This runs on its own schedule, separate from the market collector. Trading
flows move slowly and steadily; points move in bursts while matches are on and
not at all in between, so the two want different cadences and different
endpoints.

Three things happen here.

1. The fixture list is cached and refreshed once a day. It carries every
   kickoff time, so the script can tell whether anything is actually being
   played rather than polling the live endpoint around the clock.

2. When a gameweek deadline passes, the ownership and price of every player at
   that moment are frozen into data/weights/gw{n}.json. Squads lock at the
   deadline, but selected_by_percent keeps moving afterwards as managers
   transfer for the NEXT gameweek. Weighting live points by live ownership
   would mix this week's performance with next week's transfer activity.

3. While matches are live, /event/{gw}/live/ is polled and the index written
   to data/performance.jsonl:

     index_points = sum over players of (ownership fraction x points)

   This is deliberately unscaled. It assumes every owned player featured,
   which no real manager does: they field 11 of 15 and double a captain.
   Rather than guess a correction, the actual average score the game
   publishes is recorded alongside, so the gap can be measured instead.

   capital = sum over players of (ownership fraction x price), fixed for the
   gameweek, so points per million invested is derivable without another pass.

Bonus points are provisional until a match is confirmed, so values revise
upward after each fixture. Each line records whether every fixture in the
gameweek had been confirmed at the time, so a revision never silently
rewrites what was shown live.
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# model_squad sits alongside this file. The path is added explicitly rather
# than relying on the interpreter's default, and a missing module degrades to
# "no modelled squad" instead of taking the whole run down with it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import model_squad as MS
except ImportError:
    MS = None
from datetime import datetime, timedelta, timezone

BASE = "https://fantasy.premierleague.com/api"
BOOTSTRAP = f"{BASE}/bootstrap-static/"
FIXTURES = f"{BASE}/fixtures/"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SNAP_DIR = os.path.join(DATA, "snapshots")
WEIGHTS_DIR = os.path.join(DATA, "weights")
SCHEDULE_PATH = os.path.join(DATA, "schedule.json")
OUT_PATH = os.path.join(DATA, "performance.jsonl")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fpl-index/1.0)",
    "Accept": "application/json",
}

SCHEDULE_MAX_AGE_HOURS = 20   # refreshed daily, with slack for cron drift
LIVE_LEAD_MINUTES = 15        # start polling shortly before kickoff
LIVE_TAIL_MINUTES = 150       # keep polling after kickoff for stoppages
MIN_GAP_MINUTES = 8           # don't record twice inside one polling window


def fetch(url, attempts=4):
    req = urllib.request.Request(url, headers=HEADERS)
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except Exception as exc:
            if i == attempts - 1:
                raise
            wait = 5 * (2**i)
            print(f"attempt {i+1} on {url} failed ({exc}); retry in {wait}s", file=sys.stderr)
            time.sleep(wait)


def parse_iso(value):
    if not value:
        return None
    return datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")


def now():
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


# --------------------------------------------------------------------------
# schedule


def load_schedule(force=False):
    """
    Fixtures and gameweek deadlines, refreshed once a day.

    Kickoff times do move, but rarely and never by minutes, so a daily refresh
    is ample and keeps the request count low. The API throttles hard around
    deadlines, which is exactly when this script runs most often.
    """
    if not force and os.path.exists(SCHEDULE_PATH):
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        age = now() - parse_iso(cached["fetched"])
        if age < timedelta(hours=SCHEDULE_MAX_AGE_HOURS):
            return cached, False

    boot = fetch(BOOTSTRAP)
    fixtures = fetch(FIXTURES)

    schedule = {
        "fetched": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": [
            {
                "id": ev["id"],
                "deadline": ev["deadline_time"],
                "finished": ev.get("finished", False),
                # Populated by the game only once the gameweek completes.
                "average": ev.get("average_entry_score"),
                "highest": ev.get("highest_score"),
            }
            for ev in boot.get("events", [])
        ],
        "fixtures": [
            {
                "id": f["id"],
                "event": f.get("event"),
                "kickoff": f.get("kickoff_time"),
                "started": f.get("started", False),
                "finished": f.get("finished", False),
                "confirmed": f.get("finished_provisional", False),
            }
            for f in fixtures
            if f.get("event")
        ],
    }

    os.makedirs(DATA, exist_ok=True)
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as fh:
        json.dump(schedule, fh, ensure_ascii=False, indent=1)
    return schedule, True


def live_window(schedule, when):
    """
    The gameweek being played right now, or None.

    A fixture counts as live from shortly before kickoff until well after, so
    that stoppages and long added time do not end polling early. Anything the
    API has marked started but not confirmed also counts, however old, because
    bonus can land late.
    """
    for f in schedule["fixtures"]:
        ko = parse_iso(f["kickoff"])
        if ko is None:
            continue
        if f["started"] and not f["confirmed"]:
            return f["event"]
        if ko - timedelta(minutes=LIVE_LEAD_MINUTES) <= when <= ko + timedelta(
            minutes=LIVE_TAIL_MINUTES
        ):
            return f["event"]
    return None


def passed_deadlines(schedule, when):
    """Gameweeks whose deadline has gone, most recent first."""
    out = []
    for ev in schedule["events"]:
        dl = parse_iso(ev["deadline"])
        if dl and dl <= when:
            out.append((ev["id"], dl))
    return sorted(out, key=lambda x: -x[1].timestamp())


# --------------------------------------------------------------------------
# frozen weights


def snapshot_at_or_before(deadline):
    """
    The last market snapshot taken at or before a deadline.

    The market collector already writes ownership and price every half hour, so
    there is no need for a separate call: the reading just before the deadline
    is the state squads locked in at.
    """
    best = None
    for name in sorted(os.listdir(SNAP_DIR)) if os.path.isdir(SNAP_DIR) else []:
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(SNAP_DIR, name), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = json.loads(line)
                    t = parse_iso(snap["t"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if t <= deadline and (best is None or t > parse_iso(best["t"])):
                    best = snap
    return best


def freeze_weights(event_id, deadline):
    """Write gw{n}.json if it does not exist yet. Returns the weights."""
    path = os.path.join(WEIGHTS_DIR, f"gw{event_id}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    snap = snapshot_at_or_before(deadline)
    if snap is None:
        print(f"no market snapshot at or before the GW{event_id} deadline", file=sys.stderr)
        return None

    weights, capital, squad = {}, 0.0, 0.0
    for code, el in snap["e"].items():
        price, pct = el[0] / 10, el[1] / 1000   # £m, ownership fraction
        if pct <= 0:
            continue
        weights[code] = round(pct, 6)
        capital += pct * price
        squad += pct

    payload = {
        "event": event_id,
        "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frozen_from": snap["t"],
        "total_players": snap["total_players"],
        # Money the average manager had committed at the deadline, in £m.
        "capital": round(capital, 4),
        # Ownership fractions summed across every player, which comes to
        # about 15 because that is how many players a squad holds. Used to
        # express how much of the average squad has played.
        "squad": round(squad, 4),
        "weights": weights,
    }
    os.makedirs(WEIGHTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"froze GW{event_id} weights from {snap['t']}: capital £{capital:.2f}m")
    return payload


# --------------------------------------------------------------------------
# the index


def element_points(live):
    """code-keyed points from /event/{gw}/live/, which is keyed by element id."""
    out = {}
    for el in live.get("elements", []):
        stats = el.get("stats", {})
        out[el["id"]] = {
            "points": stats.get("total_points", 0),
            "minutes": stats.get("minutes", 0),
            "bonus": stats.get("bonus", 0),
        }
    return out


def build_index(weights, live, id_to_code, positions):
    """
    Ownership-weighted points, overall and split by position.

    Unscaled on purpose: it counts every owned player, where a real manager
    fields eleven and doubles a captain. The published average score is
    recorded next to it so the difference can be measured rather than assumed.
    """
    pts = element_points(live)
    total, by_pos, played = 0.0, {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0}, 0.0

    for eid, stat in pts.items():
        code = id_to_code.get(eid)
        if code is None:
            continue
        w = weights["weights"].get(str(code))
        if not w:
            continue
        contribution = w * stat["points"]
        total += contribution
        pos = str(positions.get(str(code), 0))
        if pos in by_pos:
            by_pos[pos] += contribution
        if stat["minutes"] > 0:
            played += w

    # Weights sum to roughly fifteen, not one, so normalise before reporting
    # this as a share of the squad.
    squad = weights.get("squad") or sum(weights["weights"].values()) or 1
    return {
        "points": round(total, 4),
        "by_pos": {k: round(v, 4) for k, v in by_pos.items()},
        # How much of the average squad has taken the field, which says how
        # far through the gameweek the index is.
        "played": round(played / squad, 4),
    }


def model_points(event_id, pts_by_code, weights):
    """
    What the modelled template has scored so far this gameweek.

    Builds the model if it does not exist yet, so the first live poll after a
    deadline does not have to wait for a separate job. Captain is the same rule
    the benchmark uses everywhere: highest-owned outfielder in the eleven.
    """
    if MS is None:
        return None
    path = os.path.join(DATA, "models", f"gw{event_id}.json")
    if not os.path.exists(path):
        try:
            MS.build(event_id)
        except Exception as exc:
            print(f"could not build the GW{event_id} model ({exc})", file=sys.stderr)
            return None
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as fh:
        model = json.load(fh)

    positions = {p["code"]: p["pos"] for p in model["squad"]}
    total = sum(pts_by_code.get(c, 0) for c in model["xi"])

    captain, best = None, -1
    for c in model["xi"]:
        if positions.get(c) == 1:
            continue
        w = weights["weights"].get(c, 0)
        if w > best:
            best, captain = w, c
    if captain:
        total += pts_by_code.get(captain, 0)

    return round(total, 2)


def recent_enough(when):
    if not os.path.exists(OUT_PATH):
        return False
    cutoff = when - timedelta(minutes=MIN_GAP_MINUTES)
    last = None
    with open(OUT_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return False
    try:
        return parse_iso(json.loads(last)["t"]) > cutoff
    except (json.JSONDecodeError, KeyError, ValueError):
        return False


def main():
    when = now()
    force = "--force" in sys.argv

    schedule, refreshed = load_schedule(force="--refresh" in sys.argv)
    if refreshed:
        print("schedule refreshed")

    # Freeze weights for any deadline that has passed and is not yet frozen.
    for event_id, deadline in passed_deadlines(schedule, when)[:3]:
        freeze_weights(event_id, deadline)

    event_id = live_window(schedule, when)
    if event_id is None and not force:
        print("no fixture in play, nothing to record")
        return

    if event_id is None:
        recent = passed_deadlines(schedule, when)
        if not recent:
            print("no gameweek has started yet")
            return
        event_id = recent[0][0]

    if recent_enough(when) and not force:
        print(f"recorded within the last {MIN_GAP_MINUTES} minutes, skipping")
        return

    weights_path = os.path.join(WEIGHTS_DIR, f"gw{event_id}.json")
    if not os.path.exists(weights_path):
        print(f"no frozen weights for GW{event_id}, cannot build the index", file=sys.stderr)
        return
    with open(weights_path, "r", encoding="utf-8") as fh:
        weights = json.load(fh)

    # bootstrap-static is needed once here for the element id to code mapping,
    # which the live endpoint does not carry.
    boot = fetch(BOOTSTRAP)
    id_to_code = {p["id"]: p["code"] for p in boot["elements"]}
    positions = {str(p["code"]): p["element_type"] for p in boot["elements"]}

    live = fetch(f"{BASE}/event/{event_id}/live/")
    index = build_index(weights, live, id_to_code, positions)

    # Points keyed by permanent code, so the modelled template can be scored
    # from the same response rather than fetching anything else.
    pts_by_code = {}
    for eid, stat in element_points(live).items():
        code = id_to_code.get(eid)
        if code is not None:
            pts_by_code[str(code)] = stat["points"]
    model = model_points(event_id, pts_by_code, weights)

    fixtures = [f for f in schedule["fixtures"] if f["event"] == event_id]
    confirmed = bool(fixtures) and all(f["confirmed"] for f in fixtures)
    average = next(
        (ev["average"] for ev in schedule["events"] if ev["id"] == event_id), None
    )

    line = {
        "t": when.strftime("%Y-%m-%dT%H:%M:00Z"),
        "event": event_id,
        "index": index["points"],
        # The modelled template's live score, on the same clock.
        "model": model,
        "by_pos": index["by_pos"],
        "played": index["played"],
        "capital": weights["capital"],
        # Null until the gameweek finishes; the game does not publish it live.
        "average": average,
        "confirmed": confirmed,
    }

    os.makedirs(DATA, exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")

    yield_pts = index["points"] / weights["capital"] if weights["capital"] else 0
    print(
        f"GW{event_id} {line['t']}: index {index['points']:.2f} pts, "
        f"{yield_pts:.3f} pts per £m, model {model if model is not None else '-'}, "
        f"{index['played']*100:.0f}% of the squad played"
        + ("" if confirmed else ", bonus provisional")
    )


if __name__ == "__main__":
    main()
