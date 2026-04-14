"""
NHL 2025-2026 Season Data Pipeline
==================================

Production-grade ingestion / transformation pipeline that scrapes Hockey Reference,
cleans and normalizes the data, and emits three analytics-ready CSVs:

    - games.csv          : (date, visitor_team_id, visitor_goals, home_team_id, home_goals)
    - teams.csv          : (team_id, team_name, <team stats ...>)
    - team_analytics.csv : (team_id, team_name, <5-on-5 analytics ...>)

Design goals
------------
* Modular: each stage (fetch -> parse -> clean -> validate -> export) is isolated.
* Reusable: `NHLPipeline(season=2026)` - future seasons / leagues drop in trivially.
* Robust: handles Hockey Reference's HTML-commented tables, postponed games,
  rank columns, inconsistent team naming, and missing values.
* Validated: referential integrity, uniqueness, and schema checks before export.

Author: Senior Data Engineer, NHL Analytics
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup, Comment

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("nhl_pipeline")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PipelineConfig:
    season: int = 2026                       # 2025-26 season -> 2026
    base_url: str = "https://www.hockey-reference.com"
    output_dir: Path = Path("output")
    request_timeout: int = 30
    request_sleep: float = 3.0                # be polite between requests
    user_agent: str = (
        "Mozilla/5.0 (compatible; NHL-Analytics-Pipeline/1.0; "
        "+https://example.org/contact)"
    )

    @property
    def games_url(self) -> str:
        return f"{self.base_url}/leagues/NHL_{self.season}_games.html"

    @property
    def season_url(self) -> str:
        return f"{self.base_url}/leagues/NHL_{self.season}.html"


# ---------------------------------------------------------------------------
# Canonical team registry
# ---------------------------------------------------------------------------
# team_id is the official Hockey Reference / NHL three-letter franchise code.
# This is the single source of truth for joining across datasets and guarantees
# consistent naming even if Hockey Reference renames a franchise mid-season
# (e.g. Utah Hockey Club -> Utah Mammoth for 2025-26).
CANONICAL_TEAMS: dict[str, str] = {
    "ANA": "Anaheim Ducks",
    "ARI": "Arizona Coyotes",        # legacy, defunct 2024
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CAR": "Carolina Hurricanes",
    "CBJ": "Columbus Blue Jackets",
    "CGY": "Calgary Flames",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NJD": "New Jersey Devils",
    "NSH": "Nashville Predators",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SEA": "Seattle Kraken",
    "SJS": "San Jose Sharks",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",           # 2025-26 rename from Utah Hockey Club
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets",
    "WSH": "Washington Capitals",
}

# Reverse lookup: accept any variant (including legacy + alias) -> team_id
TEAM_NAME_ALIASES: dict[str, str] = {
    # canonical forms
    **{name.lower(): tid for tid, name in CANONICAL_TEAMS.items()},
    # common variants / legacy names
    "utah hockey club": "UTA",
    "utah mammoth": "UTA",
    "montréal canadiens": "MTL",
    "st louis blues": "STL",
    "st. louis blues": "STL",
    "phoenix coyotes": "ARI",
}


def normalize_team_name(name: str) -> str | None:
    """Map any team name variant to its canonical three-letter team_id."""
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    # Strip trailing asterisks Hockey Reference uses to mark playoff teams.
    key = key.rstrip("*").strip()
    return TEAM_NAME_ALIASES.get(key)


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------
class HockeyReferenceFetcher:
    """Thin HTTP client with retry + backoff, respects rate limits."""

    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent})

    def get(self, url: str, retries: int = 3) -> str:
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                log.info("GET %s (attempt %d)", url, attempt)
                resp = self.session.get(url, timeout=self.cfg.request_timeout)
                resp.raise_for_status()
                time.sleep(self.cfg.request_sleep)
                return resp.text
            except requests.RequestException as e:
                last_err = e
                log.warning("Fetch failed (%s); backing off...", e)
                time.sleep(self.cfg.request_sleep * attempt * 2)
        raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------
def extract_table_html(page_html: str, table_id: str) -> str | None:
    """
    Hockey Reference hides many secondary tables inside HTML comments to
    defeat naive scrapers. This helper returns the raw <table> HTML whether
    it lives in the DOM or in a comment.
    """
    soup = BeautifulSoup(page_html, "lxml")

    # First: look in the live DOM.
    tbl = soup.find("table", id=table_id)
    if tbl is not None:
        return str(tbl)

    # Fallback: scan every HTML comment for the table.
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if f'id="{table_id}"' in c:
            inner = BeautifulSoup(c, "lxml")
            tbl = inner.find("table", id=table_id)
            if tbl is not None:
                return str(tbl)
    return None


def read_single_table(html_fragment: str) -> pd.DataFrame:
    """Parse a single <table> HTML string into a clean DataFrame."""
    dfs = pd.read_html(StringIO(html_fragment))
    if not dfs:
        raise ValueError("No table parsed from HTML fragment")
    df = dfs[0]
    # Flatten multi-index headers (hockey-reference uses over-headers).
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            c[1] if not str(c[0]).startswith("Unnamed") else c[1]
            for c in df.columns
        ]
    return df


_snake_re1 = re.compile(r"[^0-9a-zA-Z]+")
_snake_re2 = re.compile(r"_+")


def safe_to_numeric(s: pd.Series) -> pd.Series:
    """pandas 2.x removed errors='ignore'; emulate it."""
    converted = pd.to_numeric(s, errors="coerce")
    # If everything became NaN but the original had non-null strings, keep original.
    if converted.isna().all() and s.notna().any():
        return s
    return converted


def to_snake_case(name: str) -> str:
    s = _snake_re1.sub("_", str(name).strip())
    s = _snake_re2.sub("_", s).strip("_").lower()
    return s or "col"


def dedupe_columns(cols: Iterable[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for c in cols:
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Stage: games.csv
# ---------------------------------------------------------------------------
def parse_games(page_html: str) -> pd.DataFrame:
    """Extract, clean, and normalize the season schedule & results table."""
    html_tbl = extract_table_html(page_html, "games")
    if html_tbl is None:
        raise RuntimeError("Could not locate #games table on schedule page")
    df = read_single_table(html_tbl)

    # Hockey Reference's #games table has two columns literally titled 'G'
    # (visitor goals, then home goals). pandas suffixes the dup -> 'G.1'.
    rename_map = {
        "Date": "date",
        "Visitor": "visitor_team_name",
        "G": "visitor_goals",
        "Home": "home_team_name",
        "G.1": "home_goals",
    }
    df = df.rename(columns=rename_map)

    required = ["date", "visitor_team_name", "visitor_goals",
                "home_team_name", "home_goals"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"Games table missing expected columns: {missing}. "
            f"Got: {df.columns.tolist()}"
        )

    df = df[required].copy()

    # Drop intra-table header repeats ("Date" appearing as a value).
    df = df[df["date"].astype(str).str.lower() != "date"]

    # Standardize dates (ISO 8601).
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    # Map team names -> canonical IDs.
    df["visitor_team_id"] = df["visitor_team_name"].map(normalize_team_name)
    df["home_team_id"] = df["home_team_name"].map(normalize_team_name)

    # Report any unmapped teams loudly - silent data loss is unacceptable.
    unmapped = pd.concat([
        df.loc[df["visitor_team_id"].isna(), "visitor_team_name"],
        df.loc[df["home_team_id"].isna(), "home_team_name"],
    ]).dropna().unique()
    if len(unmapped):
        log.warning("Unmapped team names encountered: %s", list(unmapped))

    # Goals: numeric; NaN for postponed / unplayed games (kept, not dropped,
    # so downstream can filter via date).
    df["visitor_goals"] = pd.to_numeric(df["visitor_goals"], errors="coerce").astype("Int64")
    df["home_goals"] = pd.to_numeric(df["home_goals"], errors="coerce").astype("Int64")

    out = df[[
        "date",
        "visitor_team_id", "visitor_goals",
        "home_team_id", "home_goals",
    ]].copy()

    # Remove rows with no valid date (header junk, etc).
    out = out.dropna(subset=["date"])

    # Drop exact duplicates (defensive).
    out = out.drop_duplicates()

    # Stable chronological sort for reproducibility.
    out = out.sort_values(["date", "visitor_team_id", "home_team_id"]).reset_index(drop=True)

    log.info("Parsed %d games (%d played, %d scheduled/postponed)",
             len(out),
             int(out["home_goals"].notna().sum()),
             int(out["home_goals"].isna().sum()))
    return out


# ---------------------------------------------------------------------------
# Stage: teams.csv and team_analytics.csv
# ---------------------------------------------------------------------------
# Columns whose semantics are "rank within league" and are excluded per spec.
_RANK_COL_PATTERNS = (
    re.compile(r"^rk$", re.I),
    re.compile(r"^rank$", re.I),
    re.compile(r"_rk$", re.I),
    re.compile(r"_rank$", re.I),
)


def _drop_rank_columns(df: pd.DataFrame) -> pd.DataFrame:
    drop = [c for c in df.columns
            if any(p.search(c) for p in _RANK_COL_PATTERNS)]
    if drop:
        log.info("Dropping rank columns: %s", drop)
    return df.drop(columns=drop)


def _normalize_team_stats_frame(df: pd.DataFrame, team_col: str) -> pd.DataFrame:
    """Shared cleaning for both team stats tables."""
    # Remove footer "League Average" / "Avg" rows.
    df = df[~df[team_col].astype(str).str.contains(
        r"league average|avg|^\s*$", case=False, regex=True, na=True
    )].copy()

    # Map names -> canonical team_id.
    df["team_id"] = df[team_col].map(normalize_team_name)
    unmapped = df.loc[df["team_id"].isna(), team_col].unique()
    if len(unmapped):
        log.warning("Unmapped team names in stats table: %s", list(unmapped))
        df = df.dropna(subset=["team_id"])

    # Canonical team_name pulled from the registry so it is guaranteed
    # identical across all three output files.
    df["team_name"] = df["team_id"].map(CANONICAL_TEAMS)

    # Reorder: team_id, team_name, ... all other stats (minus the original name).
    other_cols = [c for c in df.columns
                  if c not in {team_col, "team_id", "team_name"}]
    return df[["team_id", "team_name", *other_cols]].reset_index(drop=True)


def parse_team_stats(page_html: str) -> pd.DataFrame:
    """Extract and clean the 'Team Statistics' table (#stats)."""
    html_tbl = extract_table_html(page_html, "stats")
    if html_tbl is None:
        raise RuntimeError("Could not locate #stats table on season page")
    df = read_single_table(html_tbl)

    # The Team column header is usually "Team" but can be anonymous.
    team_col = "Team" if "Team" in df.columns else df.columns[1]

    df = _drop_rank_columns(df)

    # Snake case everything except the team column (handled separately).
    df.columns = dedupe_columns([
        c if c == team_col else to_snake_case(c) for c in df.columns
    ])

    df = _normalize_team_stats_frame(df, team_col=team_col)

    # Coerce every non-identifier column to numeric where possible.
    for c in df.columns:
        if c in {"team_id", "team_name"}:
            continue
        df[c] = safe_to_numeric(df[c])

    log.info("Parsed team stats: %d rows x %d cols", *df.shape)
    return df


def parse_team_analytics(page_html: str, teams_df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract 'Team Analytics (5-on-5)' (#stats_adv_5on5), join to teams for
    canonical (team_id, team_name) keys.
    """
    html_tbl = extract_table_html(page_html, "stats_adv")
    if html_tbl is None:
        raise RuntimeError("Could not locate #stats_adv table on season page")
    df = read_single_table(html_tbl)

    team_col = "Team" if "Team" in df.columns else df.columns[1]

    df = _drop_rank_columns(df)

    df.columns = dedupe_columns([
        c if c == team_col else to_snake_case(c) for c in df.columns
    ])

    df = _normalize_team_stats_frame(df, team_col=team_col)

    # Join against teams.csv to enforce primary-key integrity and to reuse
    # the canonical team_name (not the analytics-table spelling).
    analytics_cols = [c for c in df.columns if c not in {"team_id", "team_name"}]
    df = (
        teams_df[["team_id", "team_name"]]
        .merge(df[["team_id", *analytics_cols]], on="team_id", how="inner")
    )

    for c in analytics_cols:
        df[c] = safe_to_numeric(df[c])

    log.info("Parsed team analytics: %d rows x %d cols", *df.shape)
    return df


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(games: pd.DataFrame,
             teams: pd.DataFrame,
             analytics: pd.DataFrame) -> None:
    """Hard-fail data validation before any file is written."""
    log.info("Running validation checks...")

    # 1. teams primary key uniqueness
    assert teams["team_id"].is_unique, "teams.team_id not unique"
    assert teams["team_id"].notna().all(), "teams.team_id has NULLs"

    # 2. analytics primary key uniqueness
    assert analytics["team_id"].is_unique, "team_analytics.team_id not unique"

    # 3. Referential integrity: analytics -> teams
    orphan_analytics = set(analytics["team_id"]) - set(teams["team_id"])
    assert not orphan_analytics, f"analytics rows without team: {orphan_analytics}"

    # 4. Referential integrity: games -> teams
    team_ids = set(teams["team_id"])
    bad_home = set(games["home_team_id"].dropna()) - team_ids
    bad_vis = set(games["visitor_team_id"].dropna()) - team_ids
    assert not bad_home, f"games.home_team_id references unknown teams: {bad_home}"
    assert not bad_vis, f"games.visitor_team_id references unknown teams: {bad_vis}"

    # 5. Games schema
    assert list(games.columns) == [
        "date", "visitor_team_id", "visitor_goals",
        "home_team_id", "home_goals",
    ], f"games schema drift: {games.columns.tolist()}"

    # 6. Consistent naming across teams + analytics
    merged = teams[["team_id", "team_name"]].merge(
        analytics[["team_id", "team_name"]],
        on="team_id", suffixes=("_t", "_a"),
    )
    mismatch = merged[merged["team_name_t"] != merged["team_name_a"]]
    assert mismatch.empty, f"team_name mismatch:\n{mismatch}"

    # 7. Date format
    bad_dates = games.loc[
        ~games["date"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$"), "date"
    ]
    assert bad_dates.empty, f"non-ISO dates: {bad_dates.head().tolist()}"

    log.info("All validation checks passed ✔")


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------
class NHLPipeline:
    def __init__(self, cfg: PipelineConfig | None = None):
        self.cfg = cfg or PipelineConfig()
        self.fetcher = HockeyReferenceFetcher(self.cfg)

    def run(self) -> dict[str, Path]:
        cfg = self.cfg
        cfg.output_dir.mkdir(parents=True, exist_ok=True)

        games_html = self.fetcher.get(cfg.games_url)
        season_html = self.fetcher.get(cfg.season_url)

        games = parse_games(games_html)
        teams = parse_team_stats(season_html)
        analytics = parse_team_analytics(season_html, teams)

        validate(games, teams, analytics)

        paths = {
            "games": cfg.output_dir / "games.csv",
            "teams": cfg.output_dir / "teams.csv",
            "team_analytics": cfg.output_dir / "team_analytics.csv",
        }
        games.to_csv(paths["games"], index=False)
        teams.to_csv(paths["teams"], index=False)
        analytics.to_csv(paths["team_analytics"], index=False)

        log.info("Wrote: %s", {k: str(v) for k, v in paths.items()})
        return paths


if __name__ == "__main__":
    NHLPipeline().run()
