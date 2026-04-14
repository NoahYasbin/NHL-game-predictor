# NHL 2025-2026 Pipeline — Data Dictionary

All three output files share `team_id` (three-letter franchise code, e.g. `BOS`, `UTA`)
as the canonical join key. `team_id` is drawn from an internal registry, not from the
source HTML, so it survives franchise renames (Utah Hockey Club → Utah Mammoth) and
typographical variants across pages.

---

## `games.csv`

One row per scheduled regular-season + playoff game. Unplayed / postponed games are
**kept** with null scores so downstream pipelines can distinguish "not played yet"
from "played, 0 goals".

| Column            | Type   | Description                                                       |
|-------------------|--------|-------------------------------------------------------------------|
| `date`            | string | Game date, ISO 8601 (`YYYY-MM-DD`)                                |
| `visitor_team_id` | string | FK → `teams.team_id` — the away team                              |
| `visitor_goals`   | Int64  | Goals scored by the visitor; null if not yet played / postponed   |
| `home_team_id`    | string | FK → `teams.team_id` — the home team                              |
| `home_goals`      | Int64  | Goals scored by the home team; null if not yet played / postponed |

Sorted ascending by `(date, visitor_team_id, home_team_id)` for reproducibility.

---

## `teams.csv`

One row per franchise, sourced from Hockey Reference's **Team Statistics** table
(`#stats`). All ranking columns (`Rk`, `*_rk`, `*_rank`) are dropped per spec. All
remaining columns are coerced to numeric where possible and snake_cased.

| Column       | Type   | Description                                              |
|--------------|--------|----------------------------------------------------------|
| `team_id`    | string | **Primary key**. Three-letter franchise code.            |
| `team_name`  | string | Canonical display name from internal registry.           |
| `avage`      | float  | Average age of skaters                                   |
| `gp`         | int    | Games played                                             |
| `w`          | int    | Wins                                                     |
| `l`          | int    | Losses                                                   |
| `ol`         | int    | Overtime / shootout losses                               |
| `pts`        | int    | Standings points                                         |
| `pts_1`      | float  | Points percentage (`PTS%`; renamed to avoid dup of `pts`)|
| `gf`         | int    | Goals for                                                |
| `ga`         | int    | Goals against                                            |
| `sow`        | int    | Shootout wins                                            |
| `sol`        | int    | Shootout losses                                          |
| `srs`        | float  | Simple Rating System (goal diff + strength of schedule)  |
| `sos`        | float  | Strength of schedule                                     |
| `tg_g`       | float  | Total goals per game                                     |
| `evgf`       | int    | Even-strength goals for                                  |
| `evga`       | int    | Even-strength goals against                              |
| `pp`         | int    | Power-play goals                                         |
| `ppo`        | int    | Power-play opportunities                                 |
| `pp_1`       | float  | Power-play percentage                                    |
| `pk`         | int    | Power-play goals allowed (shorthanded against)           |
| `pko`        | int    | Times shorthanded                                        |
| `pk_1`       | float  | Penalty-kill percentage                                  |
| `sh`         | int    | Shorthanded goals for                                    |
| `sha`        | int    | Shorthanded goals against                                |
| `pim_g`      | float  | Penalty minutes per game                                 |
| `opim_g`     | float  | Opponent penalty minutes per game                        |
| `s`          | int    | Shots on goal                                            |
| `s_1`        | float  | Shooting percentage                                      |
| `sa`         | int    | Shots against                                            |
| `sv`         | int    | Saves                                                    |
| `sv_1`       | float  | Save percentage                                          |
| `so`         | int    | Shutouts                                                 |

Exact column set depends on Hockey Reference's current schema; unexpected columns
flow through automatically (snake_cased). Columns with identical snake_cased names
are disambiguated with `_1`, `_2`, etc.

---

## `team_analytics.csv`

One row per franchise from **Team Analytics — 5-on-5** (`#stats_adv_5on5`), joined
to `teams.csv` on `team_id` so `team_name` is guaranteed identical across files.
Rank columns dropped.

| Column      | Type   | Description                                         |
|-------------|--------|-----------------------------------------------------|
| `team_id`   | string | **Primary key**, FK → `teams.team_id`               |
| `team_name` | string | Canonical display name (joined from teams.csv)      |
| `s`         | float  | 5v5 shooting percentage                             |
| `sv`        | float  | 5v5 save percentage                                 |
| `pdo`       | float  | PDO (shooting % + save % × 1000)                    |
| `cf`        | int    | Corsi For (all shot attempts for)                   |
| `ca`        | int    | Corsi Against                                       |
| `cf_1`      | float  | Corsi For %                                         |
| `ff`        | int    | Fenwick For (unblocked shot attempts for)           |
| `fa`        | int    | Fenwick Against                                     |
| `ff_1`      | float  | Fenwick For %                                       |
| `xgf`       | float  | Expected goals for                                  |
| `xga`       | float  | Expected goals against                              |
| `xgf_1`     | float  | Expected goals for %                                |
| `agf`       | int    | Actual goals for (5v5)                              |
| `aga`       | int    | Actual goals against (5v5)                          |
| `sca`       | int    | Scoring-chance attempts (if present in source)      |
| `hdf`       | int    | High-danger chances for (if present)                |
| `hda`       | int    | High-danger chances against (if present)            |

---

## Validation guarantees (enforced before any file is written)

1. `teams.team_id` and `team_analytics.team_id` are unique and non-null.
2. Every `games.home_team_id` and `games.visitor_team_id` exists in `teams`.
3. Every `team_analytics.team_id` exists in `teams`.
4. `team_name` is identical between `teams` and `team_analytics` for every `team_id`.
5. Every `games.date` matches `^\d{4}-\d{2}-\d{2}$`.
6. `games` columns exactly equal the documented schema (no drift).

A failure on any check raises `AssertionError` and no CSVs are overwritten.
