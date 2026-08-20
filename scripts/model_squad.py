#!/usr/bin/env python3
"""
Build the modelled squad: the most-owned fifteen a manager could actually field.

The raw fifteen most invested-in players cost more than a squad budget, so they
are not an investable benchmark. This picks the affordable fifteen that overlap
most with what the market owns, subject to the real rules of the game:

  2 goalkeepers, 5 defenders, 5 midfielders, 3 forwards
  no more than 3 players from any one club
  total price within budget

Budget is the average manager's own squad value at deadline prices, which the
weights file already carries as `capital`. Using that rather than a flat £100m
keeps the modelled squad affordable as prices inflate through the season,
exactly as a real squad's value rises with it.

"Overlap" is the sum of ownership fractions across the chosen fifteen, which is
the same quantity SWAPPI weights by. A squad scoring 8.0 holds, on average,
8 of the 15 players a typical manager holds.

The club cap couples the positions together, so this is a multi-dimensional
knapsack rather than four independent ones. At this size a greedy start
followed by exhaustive single and double swaps reaches the optimum or very
near it in well under a second, and the gap to a relaxed upper bound is
reported so you can see how close it got.

Writes data/models/gw{n}.json, once per deadline.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
WEIGHTS_DIR = os.path.join(DATA, "weights")
MODELS_DIR = os.path.join(DATA, "models")
PLAYERS_PATH = os.path.join(DATA, "players.json")
SNAP_DIR = os.path.join(DATA, "snapshots")

SLOTS = {1: 2, 2: 5, 3: 5, 4: 3}
MAX_PER_CLUB = 3


def load_candidates(weights, players_meta, prices):
    """Every player with an ownership weight, priced at the deadline."""
    out = []
    for code, w in weights["weights"].items():
        meta = players_meta.get(code)
        price = prices.get(code)
        if not meta or price is None:
            continue
        out.append(
            {
                "code": code,
                "name": meta.get("name", code),
                "pos": meta.get("pos", 0),
                "team": meta.get("team", 0),
                "own": w,
                "price": price,          # in tenths of a million
            }
        )
    return [c for c in out if c["pos"] in SLOTS]


def feasible(squad, candidate, budget):
    if sum(1 for p in squad if p["pos"] == candidate["pos"]) >= SLOTS[candidate["pos"]]:
        return False
    if sum(1 for p in squad if p["team"] == candidate["team"]) >= MAX_PER_CLUB:
        return False
    if any(p["code"] == candidate["code"] for p in squad):
        return False
    return spend(squad) + candidate["price"] <= budget


def spend(squad):
    return sum(p["price"] for p in squad)


def overlap(squad):
    return sum(p["own"] for p in squad)


def cheapest_remaining(candidates, squad):
    """
    Minimum cost of filling every slot still empty.

    Used to stop the greedy pass spending so much on a premium that it cannot
    legally complete the squad afterwards.
    """
    need = {pos: SLOTS[pos] - sum(1 for p in squad if p["pos"] == pos) for pos in SLOTS}
    total = 0
    for pos, n in need.items():
        if n <= 0:
            continue
        pool = sorted(
            (c["price"] for c in candidates
             if c["pos"] == pos and not any(p["code"] == c["code"] for p in squad))
        )
        if len(pool) < n:
            return None
        total += sum(pool[:n])
    return total


def greedy(candidates, budget):
    """Most-owned first, skipping anyone who would make the squad impossible."""
    squad = []
    for c in sorted(candidates, key=lambda x: -x["own"]):
        if len(squad) == 15:
            break
        if not feasible(squad, c, budget):
            continue
        trial = squad + [c]
        rest = cheapest_remaining(candidates, trial)
        if rest is None or spend(trial) + rest > budget:
            continue
        squad = trial
    return squad


def improve(squad, candidates, budget, rounds=6):
    """
    Swap one player at a time for a better-owned alternative, repeatedly.

    Each pass considers every player in the squad against every legal
    replacement, which is small enough to do exhaustively.
    """
    by_pos = {}
    for c in candidates:
        by_pos.setdefault(c["pos"], []).append(c)
    for pool in by_pos.values():
        pool.sort(key=lambda x: -x["own"])

    for _ in range(rounds):
        best_gain, best_move = 1e-9, None
        for i, out in enumerate(squad):
            rest = squad[:i] + squad[i + 1:]
            room = budget - spend(rest)
            clubs = {}
            for p in rest:
                clubs[p["team"]] = clubs.get(p["team"], 0) + 1
            held = {p["code"] for p in rest}
            for cand in by_pos.get(out["pos"], []):
                if cand["own"] - out["own"] <= best_gain:
                    break  # sorted by ownership, so nothing later can beat it
                if cand["code"] in held or cand["price"] > room:
                    continue
                if clubs.get(cand["team"], 0) >= MAX_PER_CLUB:
                    continue
                best_gain, best_move = cand["own"] - out["own"], (i, cand)
        if not best_move:
            break
        i, cand = best_move
        squad = squad[:i] + [cand] + squad[i + 1:]
    return squad


def upper_bound(candidates):
    """
    Best possible overlap ignoring budget and club caps.

    Not reachable in general, but it bounds how much the constraints cost, so
    the reported gap is a genuine ceiling rather than a guess.
    """
    total = 0.0
    for pos, n in SLOTS.items():
        pool = sorted((c["own"] for c in candidates if c["pos"] == pos), reverse=True)
        total += sum(pool[:n])
    return total


def pick_xi(squad):
    """
    The eleven to start, and the bench order.

    Most-owned first in a legal formation, never by points: choosing on points
    would be hindsight and would make the benchmark unbeatable. At least one
    goalkeeper, three defenders and one forward, as the game requires.
    """
    by_pos = {pos: sorted([p for p in squad if p["pos"] == pos], key=lambda x: -x["own"])
              for pos in SLOTS}

    xi = [by_pos[1][0]] + by_pos[2][:3] + by_pos[4][:1]
    pool = sorted(
        by_pos[2][3:] + by_pos[3] + by_pos[4][1:], key=lambda x: -x["own"]
    )
    for p in pool:
        if len(xi) == 11:
            break
        xi.append(p)

    started = {p["code"] for p in xi}
    # Outfield bench in ascending ownership, because autosubs come on in order.
    bench = sorted(
        [p for p in squad if p["code"] not in started and p["pos"] != 1],
        key=lambda x: x["own"],
    )
    spare_gk = [p for p in squad if p["pos"] == 1 and p["code"] not in started]
    return xi, spare_gk + bench


def build(event_id):
    with open(os.path.join(WEIGHTS_DIR, f"gw{event_id}.json"), encoding="utf-8") as fh:
        weights = json.load(fh)
    with open(PLAYERS_PATH, encoding="utf-8") as fh:
        players_meta = json.load(fh)["players"]

    # Prices as at the snapshot the weights were frozen from.
    prices = {}
    for name in sorted(os.listdir(SNAP_DIR)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(SNAP_DIR, name), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                snap = json.loads(line)
                if snap.get("t") == weights["frozen_from"]:
                    prices = {c: el[0] for c, el in snap["e"].items()}
    if not prices:
        raise SystemExit(f"could not find the snapshot {weights['frozen_from']}")

    budget = int(round(weights["capital"] * 10))   # £m to tenths
    candidates = load_candidates(weights, players_meta, prices)

    squad = improve(greedy(candidates, budget), candidates, budget)
    if len(squad) < 15:
        raise SystemExit(f"could only fill {len(squad)} of 15 slots within budget")

    xi, bench = pick_xi(squad)
    bound = upper_bound(candidates)

    payload = {
        "event": event_id,
        "deadline": weights["deadline"],
        "budget": round(budget / 10, 1),
        "spend": round(spend(squad) / 10, 1),
        "overlap": round(overlap(squad), 4),
        "unconstrained": round(bound, 4),
        "squad": [
            {"code": p["code"], "n": p["name"], "pos": p["pos"], "team": p["team"],
             "own": round(p["own"], 6), "price": p["price"]}
            for p in squad
        ],
        "xi": [p["code"] for p in xi],
        "bench": [p["code"] for p in bench],
    }
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(os.path.join(MODELS_DIR, f"gw{event_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    return payload


def main():
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    if not ids:
        ids = [
            int(f[2:-5]) for f in os.listdir(WEIGHTS_DIR)
            if f.startswith("gw") and f.endswith(".json")
        ] if os.path.isdir(WEIGHTS_DIR) else []
    if not ids:
        raise SystemExit("no frozen weights found; run collect_live.py first")

    for event_id in sorted(ids):
        out = os.path.join(MODELS_DIR, f"gw{event_id}.json")
        if os.path.exists(out) and "--force" not in sys.argv:
            continue
        p = build(event_id)
        print(
            f"GW{p['event']}: overlap {p['overlap']:.2f} of {p['unconstrained']:.2f} "
            f"possible, £{p['spend']:.1f}m of £{p['budget']:.1f}m"
        )


if __name__ == "__main__":
    main()
