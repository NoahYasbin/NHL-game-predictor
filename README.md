# NHL 2025-2026 Data Pipeline

End-to-end ingestion → transformation → validation → export for Hockey
Reference's 2025-26 NHL season, producing three analytics-ready CSVs:

```
output/
├── games.csv           # (date, visitor_team_id, visitor_goals, home_team_id, home_goals)
├── teams.csv           # (team_id, team_name, ...stats)
└── team_analytics.csv  # (team_id, team_name, ...5v5 analytics)
```

## Install

```bash
pip install pandas lxml beautifulsoup4 requests
```

## Run

```bash
python nhl_pipeline.py
```

Or programmatically:

```python
from nhl_pipeline import NHLPipeline, PipelineConfig

# Default: 2025-26 season, writes to ./output/
NHLPipeline().run()

# Any future season or a custom output location:
NHLPipeline(PipelineConfig(season=2027, output_dir="data/2027")).run()
```

## Architecture

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────┐
│ HockeyReference  │──▶│ parse_games      │──▶│ games.csv    │
│ Fetcher          │   │ parse_team_stats │──▶│ teams.csv    │
│  (retry+backoff) │   │ parse_team_      │──▶│ team_        │
│                  │   │    analytics     │   │ analytics.csv│
└──────────────────┘   └────────┬─────────┘   └──────────────┘
                                │
                                ▼
                         ┌────────────┐
                         │ validate() │  ← hard-fail before any write
                         └────────────┘
```

### Key design decisions

- **`team_id` is the three-letter franchise code from an internal canonical
  registry**, not scraped text. This survives Hockey Reference renaming
  Utah Hockey Club → Utah Mammoth mid-season, handles asterisks on
  playoff-bound teams, accents in "Montréal", etc.
- **Tables hidden inside HTML comments are extracted.** Hockey Reference
  hides `#stats_adv_5on5` and other secondary tables inside `<!-- ... -->`
  blocks; `extract_table_html()` scans comments as a fallback.
- **Postponed / unplayed games are kept with null scores** rather than
  dropped, so downstream can distinguish "not played yet" from "0-0 final".
- **Rank columns are dropped generically** via regex (`Rk`, `_rk`, `_rank`)
  — no hardcoded list to rot as Hockey Reference's schema evolves.
- **Validation is hard-fail and runs before any CSV is written**, so the
  output directory is never left in a broken intermediate state.
- **Rate-limited + retrying HTTP client** with a descriptive User-Agent,
  3-second default sleep between requests. Be a good citizen.

## Scaling to future seasons & additional leagues

- `PipelineConfig(season=2027)` switches seasons — URLs are templated.
- Add new franchises to `CANONICAL_TEAMS` + `TEAM_NAME_ALIASES`.
- The parsers use `id=`-based selectors, not positional, so schema drift
  in unrelated tables won't break extraction.
- To add a second league (AHL, KHL) create a sibling `PipelineConfig`
  subclass overriding `games_url` / `season_url` and a parallel canonical
  team registry. Stage functions are league-agnostic.

## Files

| File | Purpose |
|---|---|
| `nhl_pipeline.py`    | Pipeline module + CLI entrypoint                   |
| `DATA_DICTIONARY.md` | Column-level documentation of every output         |
| `test_pipeline.py`   | Offline smoke test against HTML fixtures           |
| `README.md`          | This file                                          |
