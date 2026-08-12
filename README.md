# FPL index

Hourly collection of Fantasy Premier League price and ownership, so that
price x owners can be charted as a time series. History starts the hour the
collector first runs, so get it going early.

## Repo layout

```
.github/workflows/collect.yml   hourly Actions job
scripts/collect.py              the collector (Python stdlib only)
data/snapshots/YYYY-MM-DD.jsonl one JSON object per line, one line per hour
data/players.json               code -> name / team / position, plus team map
```

## Setup

1. Create a **public** repo. Actions minutes are unlimited on public repos;
   private ones get 2,000 minutes a month, and this job burns roughly 700.
2. Add these files, keeping the paths exactly as above.
3. In **Settings > Actions > General**, set Workflow permissions to
   "Read and write permissions". Without it the job cannot push.
4. Go to the **Actions** tab, pick "Collect FPL snapshot", and use
   "Run workflow" to trigger the first run by hand.
5. Check that `data/snapshots/` now has a file with one line in it.

To run it locally instead: `python3 scripts/collect.py`.

## Data format

Each line of a snapshot file:

```json
{"t":"2026-08-09T14:00:00Z","total_players":3041555,"next_event":1,
 "e":{"154561":[60,309]}}
```

Keys are the player's permanent `code`, which survives season rollovers,
unlike the `id` that bootstrap-static sorts on. Values are
`[now_cost, selected_by_percent * 10]` as integers: 60 means 6.0m, 309 means
30.9 per cent.

Owners are deliberately not stored, only derived:

```
owners = pct / 1000 * total_players
```

`total_players` was 3.0m in early August and typically triples before the
Gameweek 1 deadline as managers register. Keeping the components separate
means the site can show either a true market cap or a version holding
`total_players` fixed, which strips out the registration effect.

## Things worth knowing

- Scheduled Actions are best effort. Runs are commonly 5 to 20 minutes late
  and the occasional hour gets skipped entirely, so treat the series as
  roughly hourly rather than exactly hourly. The script buckets each run to
  the top of the hour and skips a bucket it has already recorded.
- `selected_by_percent` is rounded to 0.1 of a percentage point. At 3m
  managers that is about 3,000 owners of quantisation noise, which is
  invisible for premiums and dominant for the tail.
- Price changes land around 01:30 UK time. Ownership drifts continuously.
- JSON Lines is used so each hour appends a single line, which keeps the git
  history small even after thousands of snapshots.
- GitHub disables scheduled workflows in repos with no commits for 60 days.
  This one commits hourly, so it stays alive on its own.

## The site

`docs/index.html` is the whole website: one file, no build step, no
dependencies. GitHub Pages serves it from the `docs/` folder on `main`
(Settings > Pages > Source: Deploy from a branch, `main`, `/docs`).

Everything on the page is a **share of the total market**, not a cash figure.
For each player, price times owners, divided by the same sum across all
players. The manager count is the same for every player at a given hour, so
it cancels out of the ratio: shares are unaffected by the millions of teams
registered before the first deadline, which was distorting the cash version
badly. Shares are stored as parts per million and always add to 1,000,000.

The collector records the deadline the game is counting down to. When that
value changes, a deadline has passed, and the "Since deadline" toggle rebases
every line to 100 at that point. Before the season starts it rebases to the
first snapshot instead and the button reads "Rebased to 100".

`scripts/aggregate.py` turns the raw snapshots into what the page reads. It
runs in the same workflow, straight after the collector, and writes:

- `docs/data/core.json` loaded on open: the 20 clubs, the current top 40
  players, and a name/club entry for all 577 so search works instantly
- `docs/data/full.json` fetched only when someone searches for a player
  outside the top 40

The split matters on mobile. Together the files are about six times the size
of core alone, and most visits never need the larger one.

History is kept hourly for the last 7 days and thinned to one point per day
before that. Without thinning the file grows by roughly 14,000 numbers a day
and becomes unusable within a couple of months. Change `RECENT_DAYS` if you
want a longer hourly window.

To rebuild locally after collecting:

```
python3 scripts/collect.py
python3 scripts/aggregate.py
```

Then open `docs/index.html` through a local server rather than as a file, or
the `fetch` calls will be blocked:

```
python3 -m http.server -d docs 8000
```
