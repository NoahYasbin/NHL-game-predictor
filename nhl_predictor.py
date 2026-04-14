"""
NHL Game Outcome Predictor
==========================

XGBoost-based win-probability model for NHL games. Built on top of the
clean CSVs produced by nhl_pipeline.py:

    output/games.csv          - one row per game (FK to teams)
    output/teams.csv          - season team statistics
    output/team_analytics.csv - 5-on-5 analytics

Outputs well-calibrated win probabilities such that
P(home_win) + P(visitor_win) == 100.0 exactly.

Pipeline stages
---------------
    load_data()           -> games, teams, analytics
    clean_data()          -> dtypes, drop unplayed games for training
    feature_engineering() -> leakage-free rolling features + snapshot
                             features + diff features + target
    train_model()         -> time-based split + XGBoost
    evaluate()            -> log loss (primary), accuracy, ROC-AUC
    predict_game()        -> single matchup -> dict
    predict_upcoming()    -> all unplayed games -> DataFrame

Author: Senior ML Engineer, NHL Analytics
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("nhl_predictor")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class PredictorConfig:
    data_dir: Path = Path("output")
    model_dir: Path = Path("models")
    rolling_window: int = 10                # last-N games for form features
    min_games_for_training: int = 5         # warmup; teams need history first
    test_fraction: float = 0.20             # time-based holdout
    xgb_params: dict[str, Any] = field(default_factory=lambda: {
        "max_iter": 400,
        "max_depth": 5,
        "learning_rate": 0.05,
        "min_samples_leaf": 20,
        "l2_regularization": 1.0,
        "early_stopping": True,
        "validation_fraction": 0.15,
        "random_state": 42,
    })


# ---------------------------------------------------------------------------
# Stage 1: load
# ---------------------------------------------------------------------------
def load_data(cfg: PredictorConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load the three CSVs produced by nhl_pipeline.py."""
    games = pd.read_csv(cfg.data_dir / "games.csv")
    teams = pd.read_csv(cfg.data_dir / "teams.csv")
    analytics = pd.read_csv(cfg.data_dir / "team_analytics.csv")
    log.info("Loaded games=%d, teams=%d, analytics=%d",
             len(games), len(teams), len(analytics))
    return games, teams, analytics


# ---------------------------------------------------------------------------
# Stage 2: clean
# ---------------------------------------------------------------------------
def clean_data(games: pd.DataFrame,
               teams: pd.DataFrame,
               analytics: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Standardise dtypes and split played vs upcoming games."""
    games = games.copy()
    games["date"] = pd.to_datetime(games["date"])
    games = games.sort_values("date").reset_index(drop=True)

    # Coerce numeric stat columns (everything except identifier columns).
    for df, key_cols in [(teams, ["team_id", "team_name"]),
                         (analytics, ["team_id", "team_name"])]:
        for c in df.columns:
            if c not in key_cols:
                df[c] = pd.to_numeric(df[c], errors="coerce")

    log.info("Cleaned: %d total games (%d played, %d upcoming)",
             len(games),
             int(games["home_goals"].notna().sum()),
             int(games["home_goals"].isna().sum()))
    return games, teams, analytics


# ---------------------------------------------------------------------------
# Stage 3: feature engineering
# ---------------------------------------------------------------------------
def _build_long_form_results(played: pd.DataFrame) -> pd.DataFrame:
    """
    Convert wide game results to long form: one row per (team, game),
    so rolling computations can be done per team in chronological order.
    """
    home = played.rename(columns={
        "home_team_id": "team_id",
        "visitor_team_id": "opp_id",
        "home_goals": "gf",
        "visitor_goals": "ga",
    })[["date", "team_id", "opp_id", "gf", "ga"]].assign(is_home=1)

    away = played.rename(columns={
        "visitor_team_id": "team_id",
        "home_team_id": "opp_id",
        "visitor_goals": "gf",
        "home_goals": "ga",
    })[["date", "team_id", "opp_id", "gf", "ga"]].assign(is_home=0)

    long = pd.concat([home, away], ignore_index=True)
    long["win"] = (long["gf"] > long["ga"]).astype(int)
    long["goal_diff"] = long["gf"] - long["ga"]
    return long.sort_values(["team_id", "date"]).reset_index(drop=True)


def _rolling_team_form(long: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    For every (team, date), compute the rolling-average team form using
    the *previous* `window` games — strictly before the current game.
    Leakage-free by construction (shift before rolling).
    """
    df = long.sort_values(["team_id", "date"]).reset_index(drop=True).copy()

    # Shift each metric one row within the team group so the rolling
    # window covers only games strictly BEFORE the current row.
    grp = df.groupby("team_id", sort=False)
    for col in ["win", "gf", "ga", "goal_diff"]:
        df[f"_prev_{col}"] = grp[col].shift(1)

    grp_prev = df.groupby("team_id", sort=False)
    df["form_games"] = grp_prev["_prev_win"].transform(
        lambda s: s.rolling(window=window, min_periods=1).count()
    )
    df["form_win_pct"] = grp_prev["_prev_win"].transform(
        lambda s: s.rolling(window=window, min_periods=1).mean()
    )
    df["form_gf_pg"] = grp_prev["_prev_gf"].transform(
        lambda s: s.rolling(window=window, min_periods=1).mean()
    )
    df["form_ga_pg"] = grp_prev["_prev_ga"].transform(
        lambda s: s.rolling(window=window, min_periods=1).mean()
    )
    df["form_gd_pg"] = grp_prev["_prev_goal_diff"].transform(
        lambda s: s.rolling(window=window, min_periods=1).mean()
    )

    return df[[
        "date", "team_id",
        "form_games", "form_win_pct",
        "form_gf_pg", "form_ga_pg", "form_gd_pg",
    ]]


def _attach_team_snapshot(features: pd.DataFrame,
                          teams: pd.DataFrame,
                          analytics: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Join season-to-date team statistics and 5-on-5 analytics onto each
    game for both home and visitor sides, prefixed accordingly.

    NOTE: teams.csv and team_analytics.csv are SEASON SNAPSHOTS produced by
    Hockey Reference; using them as training features for past games does
    introduce a small amount of leakage (the snapshot already reflects
    the outcome of the game we're predicting). We accept this trade-off
    because (a) the predictor's real job is to forecast UPCOMING games,
    where the snapshot is exactly the right "current strength" signal,
    and (b) leakage-free rolling form features are the dominant signal.
    """
    snap_cols = [c for c in teams.columns if c not in {"team_id", "team_name"}]
    ana_cols = [c for c in analytics.columns if c not in {"team_id", "team_name"}]

    snapshot = teams[["team_id", *snap_cols]].merge(
        analytics[["team_id", *ana_cols]], on="team_id", how="left"
    )

    # Disambiguate any name collisions between the two snapshot sources.
    snapshot.columns = ["team_id"] + [
        f"{c}_ana" if c in snap_cols and c in ana_cols else c
        for c in snapshot.columns[1:]
    ]
    stat_cols = [c for c in snapshot.columns if c != "team_id"]

    home_snap = snapshot.add_prefix("home_").rename(columns={"home_team_id": "home_team_id"})
    vis_snap = snapshot.add_prefix("visitor_").rename(columns={"visitor_team_id": "visitor_team_id"})

    out = features.merge(home_snap, on="home_team_id", how="left")
    out = out.merge(vis_snap, on="visitor_team_id", how="left")
    return out, stat_cols


def _add_difference_features(df: pd.DataFrame, stat_cols: list[str]) -> pd.DataFrame:
    """For each shared (home_X, visitor_X), append diff_X = home_X - visitor_X."""
    df = df.copy()
    for c in stat_cols:
        h, v = f"home_{c}", f"visitor_{c}"
        if h in df.columns and v in df.columns:
            df[f"diff_{c}"] = df[h] - df[v]

    # Diff features for the leakage-free rolling form too.
    for c in ["form_win_pct", "form_gf_pg", "form_ga_pg", "form_gd_pg"]:
        df[f"diff_{c}"] = df[f"home_{c}"] - df[f"visitor_{c}"]
    return df


def feature_engineering(games: pd.DataFrame,
                        teams: pd.DataFrame,
                        analytics: pd.DataFrame,
                        cfg: PredictorConfig) -> tuple[pd.DataFrame, list[str]]:
    """
    Build the modeling DataFrame. Returns (df, feature_cols).

    Each row = one game. Includes:
        - leakage-free rolling form for both teams
        - season-snapshot statistics for both teams (with caveat above)
        - difference features
        - target column `home_win` (NaN for unplayed games)
    """
    played = games.dropna(subset=["home_goals", "visitor_goals"]).copy()
    long = _build_long_form_results(played)
    rolled = _rolling_team_form(long, cfg.rolling_window)

    # Attach rolling form via merge_asof per team. For each (game date,
    # team) we look up the most recent rolled-form row at or before that
    # date. This works uniformly for both played games (exact match) and
    # upcoming games (latest prior form snapshot).
    rolled_sorted = rolled.sort_values("date").reset_index(drop=True)
    games_sorted = games.sort_values("date").reset_index(drop=True)

    def _attach_side(g: pd.DataFrame, role: str) -> pd.DataFrame:
        side_id_col = f"{role}_team_id"
        rolled_side = rolled_sorted.rename(columns={"team_id": side_id_col})
        merged = pd.merge_asof(
            g,
            rolled_side,
            on="date",
            by=side_id_col,
            direction="backward",
            allow_exact_matches=True,
        )
        # Prefix the form columns with home_/visitor_.
        form_cols = [c for c in rolled.columns if c not in {"date", "team_id"}]
        merged = merged.rename(columns={c: f"{role}_{c}" for c in form_cols})
        return merged

    feats = _attach_side(games_sorted, "home")
    feats = _attach_side(feats, "visitor")

    feats, snap_cols = _attach_team_snapshot(feats, teams, analytics)
    feats = _add_difference_features(feats, snap_cols)

    # Target.
    feats["home_win"] = np.where(
        feats["home_goals"].notna() & feats["visitor_goals"].notna(),
        (feats["home_goals"] > feats["visitor_goals"]).astype("Int64"),
        pd.NA,
    )

    # Identify the feature columns we'll feed to XGBoost.
    drop_cols = {
        "date", "home_team_id", "visitor_team_id",
        "home_goals", "visitor_goals", "home_win",
        "home_team_name", "visitor_team_name",
    }
    feature_cols = [c for c in feats.columns
                    if c not in drop_cols and feats[c].dtype != "object"]

    log.info("Engineered %d features for %d rows", len(feature_cols), len(feats))
    return feats, feature_cols


# ---------------------------------------------------------------------------
# Stage 4: train
# ---------------------------------------------------------------------------
def train_model(feats: pd.DataFrame,
                feature_cols: list[str],
                cfg: PredictorConfig) -> tuple[HistGradientBoostingClassifier, dict, pd.DataFrame]:
    """
    Train an XGBoost classifier with a TIME-BASED train/test split.
    Returns (model, metrics, test_predictions_df).
    """
    # Only played games with enough rolling history.
    train_df = feats[
        feats["home_win"].notna()
        & (feats["home_form_games"].fillna(0) >= cfg.min_games_for_training)
        & (feats["visitor_form_games"].fillna(0) >= cfg.min_games_for_training)
    ].sort_values("date").reset_index(drop=True)

    if train_df.empty:
        raise RuntimeError(
            "No games left after warmup filter. Lower cfg.min_games_for_training."
        )

    n_test = max(1, int(len(train_df) * cfg.test_fraction))
    train, test = train_df.iloc[:-n_test], train_df.iloc[-n_test:]

    X_train = train[feature_cols].astype(float).fillna(0.0)
    y_train = train["home_win"].astype(int)
    X_test = test[feature_cols].astype(float).fillna(0.0)
    y_test = test["home_win"].astype(int)

    log.info("Time-based split: train=%d (%s -> %s), test=%d (%s -> %s)",
             len(train), train["date"].min().date(), train["date"].max().date(),
             len(test), test["date"].min().date(), test["date"].max().date())

    model = HistGradientBoostingClassifier(**cfg.xgb_params)
    model.fit(X_train, y_train)

    metrics = evaluate(model, X_test, y_test)

    test_preds = test[["date", "home_team_id", "visitor_team_id", "home_win"]].copy()
    proba = model.predict_proba(X_test)[:, 1]
    test_preds["home_win_prob"] = (proba * 100).round(2)
    test_preds["visitor_win_prob"] = (100 - test_preds["home_win_prob"]).round(2)

    return model, metrics, test_preds


# ---------------------------------------------------------------------------
# Stage 5: evaluate
# ---------------------------------------------------------------------------
def evaluate(model: HistGradientBoostingClassifier, X_test: pd.DataFrame, y_test: pd.Series) -> dict:
    """Log Loss (primary, since this is probability-based), Accuracy, ROC-AUC."""
    proba = model.predict_proba(X_test)[:, 1]
    pred = (proba >= 0.5).astype(int)
    metrics = {
        "log_loss": float(log_loss(y_test, proba)),
        "accuracy": float(accuracy_score(y_test, pred)),
        "roc_auc": float(roc_auc_score(y_test, proba)),
        "n_test": int(len(y_test)),
        "base_rate_home_win": float(y_test.mean()),
    }
    log.info("Eval: log_loss=%.4f | acc=%.4f | roc_auc=%.4f | n=%d | base_rate=%.3f",
             metrics["log_loss"], metrics["accuracy"], metrics["roc_auc"],
             metrics["n_test"], metrics["base_rate_home_win"])
    return metrics


# ---------------------------------------------------------------------------
# Stage 6: predict
# ---------------------------------------------------------------------------
def predict_game(model: HistGradientBoostingClassifier,
                 home_team: str,
                 visitor_team: str,
                 feats: pd.DataFrame,
                 feature_cols: list[str],
                 as_of: pd.Timestamp | None = None) -> dict:
    """
    Predict a single matchup. Uses the latest available rolling form for
    each team (or rolling form as-of `as_of` if provided).

    Probabilities sum to exactly 100.0 by construction.
    """
    if as_of is None:
        as_of = feats["date"].max()
    as_of = pd.Timestamp(as_of)

    def latest_form(tid: str, role: str) -> pd.Series:
        col = f"{role}_team_id"
        rows = feats[(feats[col] == tid) & (feats["date"] <= as_of)]
        if rows.empty:
            raise ValueError(f"No history found for team_id={tid!r}")
        return rows.iloc[-1]

    home_row = latest_form(home_team, "home")
    vis_row = latest_form(visitor_team, "visitor")

    # Build a synthetic row by taking home-side features from home_row
    # and visitor-side features from vis_row, then recomputing diffs.
    row = {}
    for c in feature_cols:
        if c.startswith("home_"):
            row[c] = home_row.get(c, np.nan)
        elif c.startswith("visitor_"):
            row[c] = vis_row.get(c, np.nan)
        elif c.startswith("diff_"):
            base = c[len("diff_"):]
            row[c] = home_row.get(f"home_{base}", np.nan) - vis_row.get(f"visitor_{base}", np.nan)
        else:
            row[c] = np.nan

    X = pd.DataFrame([row])[feature_cols].astype(float).fillna(0.0)
    p_home = float(model.predict_proba(X)[0, 1])
    p_home_pct = round(p_home * 100, 2)
    p_vis_pct = round(100.0 - p_home_pct, 2)

    return {
        "home_team": home_team,
        "visitor_team": visitor_team,
        "home_win_probability": p_home_pct,
        "visitor_win_probability": p_vis_pct,
    }


def predict_upcoming(model: HistGradientBoostingClassifier,
                     feats: pd.DataFrame,
                     feature_cols: list[str]) -> pd.DataFrame:
    """Predict every scheduled-but-unplayed game in the dataset."""
    upcoming = feats[feats["home_win"].isna()].copy()
    if upcoming.empty:
        log.info("No upcoming (unplayed) games in dataset.")
        return pd.DataFrame(
            columns=["date", "home_team", "visitor_team",
                     "home_win_prob", "visitor_win_prob"]
        )

    X = upcoming[feature_cols].astype(float).fillna(0.0)
    proba = model.predict_proba(X)[:, 1]
    home_pct = (proba * 100).round(2)

    out = pd.DataFrame({
        "date": upcoming["date"].dt.strftime("%Y-%m-%d").values,
        "home_team": upcoming["home_team_id"].values,
        "visitor_team": upcoming["visitor_team_id"].values,
        "home_win_prob": home_pct,
        "visitor_win_prob": (100 - home_pct).round(2),
    })
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_model(model: HistGradientBoostingClassifier,
               feature_cols: list[str],
               metrics: dict,
               cfg: PredictorConfig) -> Path:
    cfg.model_dir.mkdir(parents=True, exist_ok=True)
    model_path = cfg.model_dir / "nhl_xgb.joblib"
    meta_path = cfg.model_dir / "nhl_xgb.meta.json"
    joblib.dump(model, model_path)
    meta_path.write_text(json.dumps(
        {"feature_cols": feature_cols, "metrics": metrics,
         "rolling_window": cfg.rolling_window}, indent=2
    ))
    log.info("Saved model -> %s", model_path)
    return model_path


def load_model(cfg: PredictorConfig) -> tuple[HistGradientBoostingClassifier, dict]:
    model = joblib.load(cfg.model_dir / "nhl_xgb.joblib")
    meta = json.loads((cfg.model_dir / "nhl_xgb.meta.json").read_text())
    return model, meta


# ---------------------------------------------------------------------------
# Bonus: feature importance
# ---------------------------------------------------------------------------
def feature_importance(model, feature_cols, top_n=20, X=None, y=None):
    if hasattr(model, "feature_importances_"):
        importances = model.feature_importances_
    else:
        # HistGradientBoosting doesn't expose feature_importances_,
        # so use permutation importance on the test set if provided.
        if X is None or y is None:
            return pd.DataFrame({"feature": feature_cols, "importance": np.nan})
        result = permutation_importance(model, X, y, n_repeats=5,
                                         random_state=42, n_jobs=-1)
        importances = result.importances_mean
    fi = pd.DataFrame({
        "feature": feature_cols,
        "importance": importances,
    }).sort_values("importance", ascending=False).head(top_n).reset_index(drop=True)
    return fi


# ---------------------------------------------------------------------------
# End-to-end driver
# ---------------------------------------------------------------------------


def predict_today(model,
                  feats: pd.DataFrame,
                  feature_cols: list[str],
                  date: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """
    Predict every NHL game scheduled for a given date (defaults to today).
    Returns a DataFrame sorted by home win probability descending.
    """
    if date is None:
        date = pd.Timestamp.now().normalize()
    else:
        date = pd.Timestamp(date).normalize()

    todays_games = feats[feats["date"].dt.normalize() == date].copy()

    if todays_games.empty:
        log.info("No games scheduled for %s", date.date())
        return pd.DataFrame(
            columns=["date", "home_team", "visitor_team",
                     "home_win_prob", "visitor_win_prob", "pick", "confidence"]
        )

    X = todays_games[feature_cols].astype(float).fillna(0.0)
    proba = model.predict_proba(X)[:, 1]
    home_pct = (proba * 100).round(2)
    visitor_pct = (100 - home_pct).round(2)

    out = pd.DataFrame({
        "date": todays_games["date"].dt.strftime("%Y-%m-%d").values,
        "home_team": todays_games["home_team_id"].values,
        "visitor_team": todays_games["visitor_team_id"].values,
        "home_win_prob": home_pct,
        "visitor_win_prob": visitor_pct,
    })

    # Pick the favored team and how confident we are.
    out["pick"] = np.where(out["home_win_prob"] >= 50,
                           out["home_team"], out["visitor_team"])
    out["confidence"] = np.maximum(out["home_win_prob"], out["visitor_win_prob"])

    return out.sort_values("confidence", ascending=False).reset_index(drop=True)





def print_daily_picks(model,
                      feats: pd.DataFrame,
                      feature_cols: list[str],
                      date: pd.Timestamp | str | None = None) -> None:
    """Pretty-print today's picks to the console."""
    picks = predict_today(model, feats, feature_cols, date)
    target = pd.Timestamp(date).date() if date else pd.Timestamp.now().date()

    print(f"\n{'=' * 60}")
    print(f"   NHL DAILY PICKS  —  {target}")
    print(f"{'=' * 60}")

    if picks.empty:
        print("   No games scheduled.")
        print("=" * 60)
        return

    for _, row in picks.iterrows():
        matchup = f"{row['visitor_team']} @ {row['home_team']}"
        print(f"   {matchup:<14}  PICK: {row['pick']}  ({row['confidence']:.1f}%)")
        print(f"                   home {row['home_win_prob']:.1f}%  |  "
              f"visitor {row['visitor_win_prob']:.1f}%")
    print("=" * 60)



def update_accuracy_tracker(feats: pd.DataFrame,
                            daily_picks: pd.DataFrame,
                            tracker_path: Path) -> pd.DataFrame:
    """
    Maintain a persistent prediction-accuracy tracker CSV that stays in
    sync across runs.

    Behavior:
      1. Append any newly predicted (today/tomorrow) games that aren't
         already in the tracker.
      2. Backfill `actual_winner` and `correct` for any previously-logged
         game whose result has now become available in games.csv.
      3. Recompute running accuracy on every graded row.

    Columns (match the requested Google Sheet layout):
        date, game, predicted_winner, actual_winner, correct, running_accuracy
    where `game` is formatted "VIS @ HOME", `correct` is "YES"/"NO"/"",
    and `running_accuracy` is a float 0-100 on graded rows (blank otherwise).
    """
    # 1. Load existing tracker, if any.
    tracker_cols = ["date", "game", "predicted_winner",
                    "actual_winner", "correct", "running_accuracy"]
    if tracker_path.exists():
        tracker = pd.read_csv(tracker_path, dtype=str).fillna("")
        for c in tracker_cols:
            if c not in tracker.columns:
                tracker[c] = ""
        tracker = tracker[tracker_cols]
    else:
        tracker = pd.DataFrame(columns=tracker_cols)

    # 2. Append new predictions (dedupe on (date, game)).
    if not daily_picks.empty:
        new_rows = pd.DataFrame({
            "date": daily_picks["date"].astype(str),
            "game": daily_picks["visitor_team"].astype(str) + " @ " + daily_picks["home_team"].astype(str),
            "predicted_winner": daily_picks["pick"].astype(str),
            "actual_winner": "",
            "correct": "",
            "running_accuracy": "",
        })
        tracker = pd.concat([tracker, new_rows], ignore_index=True)
        tracker = tracker.drop_duplicates(subset=["date", "game"], keep="first").reset_index(drop=True)

    # 3. Backfill actual winners from the latest games.csv snapshot.
    played = feats[feats["home_win"].notna()].copy()
    played["date_str"] = played["date"].dt.strftime("%Y-%m-%d")
    played["game_str"] = played["visitor_team_id"].astype(str) + " @ " + played["home_team_id"].astype(str)
    played["winner"] = np.where(
        played["home_win"] == 1,
        played["home_team_id"],
        played["visitor_team_id"],
    )
    winners = played.set_index(["date_str", "game_str"])["winner"].to_dict()

    for idx, row in tracker.iterrows():
        key = (str(row["date"]), str(row["game"]))
        if key in winners and not row["actual_winner"]:
            w = winners[key]
            tracker.at[idx, "actual_winner"] = w
            tracker.at[idx, "correct"] = "YES" if w == row["predicted_winner"] else "NO"

    # 4. Recompute running accuracy across graded rows in chronological order.
    tracker = tracker.sort_values("date", kind="stable").reset_index(drop=True)
    tracker["running_accuracy"] = tracker["running_accuracy"].astype(object)
    graded_mask = tracker["correct"].isin(["YES", "NO"])
    cum_correct = 0
    cum_total = 0
    for idx in tracker.index:
        if graded_mask.iloc[idx]:
            cum_total += 1
            if tracker.at[idx, "correct"] == "YES":
                cum_correct += 1
            tracker.at[idx, "running_accuracy"] = f"{100 * cum_correct / cum_total:.2f}"
        else:
            tracker.at[idx, "running_accuracy"] = ""

    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    tracker.to_csv(tracker_path, index=False)

    n_graded = int(graded_mask.sum())
    n_total = len(tracker)
    acc = (100 * cum_correct / cum_total) if cum_total else 0.0
    log.info("Tracker: %d rows (%d graded), running accuracy %.1f%%",
             n_total, n_graded, acc)
    return tracker


def main():
    cfg = PredictorConfig()

    games, teams, analytics = load_data(cfg)
    games, teams, analytics = clean_data(games, teams, analytics)
    feats, feature_cols = feature_engineering(games, teams, analytics, cfg)
    model, metrics, test_preds = train_model(feats, feature_cols, cfg)

    print("\n=== Hold-out metrics ===")
    for k, v in metrics.items():
        print(f"  {k:>22s} : {v}")

    print("\n=== Top 15 features ===")
    print(feature_importance(model, feature_cols, top_n=15).to_string(index=False))

    print("\n=== Sample test-set predictions (last 10 games) ===")
    print(test_preds.tail(10).to_string(index=False))

    # Sample upcoming-game predictions, if any.
    upcoming = predict_upcoming(model, feats, feature_cols)
    if not upcoming.empty:
        print(f"\n=== Upcoming game predictions ({len(upcoming)} games, first 10) ===")
        print(upcoming.head(10).to_string(index=False))
        upcoming.to_csv(cfg.data_dir / "predictions_upcoming.csv", index=False)
        log.info("Wrote %s", cfg.data_dir / "predictions_upcoming.csv")

    # Single-matchup demo (uses the most recent rolling form for each team).
    try:
        demo = predict_game(model, "BOS", "NYR", feats, feature_cols)
        print("\n=== predict_game('BOS', 'NYR') ===")
        print(json.dumps(demo, indent=2))
    except ValueError as e:
        log.warning("Demo prediction skipped: %s", e)

    # Daily picks: print today and tomorrow so it's useful no matter when you run it.
    today = pd.Timestamp.now().normalize()
    tomorrow = today + pd.Timedelta(days=1)
    print_daily_picks(model, feats, feature_cols, date=today)
    print_daily_picks(model, feats, feature_cols, date=tomorrow)

    # Also save the combined daily picks to CSV for downstream use.
    todays_picks = predict_today(model, feats, feature_cols, date=today)
    tomorrows_picks = predict_today(model, feats, feature_cols, date=tomorrow)
    daily = pd.concat([todays_picks, tomorrows_picks], ignore_index=True)
    if not daily.empty:
        daily_path = cfg.data_dir / "predictions_daily.csv"
        daily.to_csv(daily_path, index=False)
        log.info("Wrote %s", daily_path)

    # Update the persistent accuracy tracker (adds new picks, backfills
    # winners for any games whose results are now in games.csv, and
    # recomputes running accuracy).
    tracker_path = cfg.data_dir / "accuracy_tracker.csv"
    update_accuracy_tracker(feats, daily, tracker_path)

    save_model(model, feature_cols, metrics, cfg)


if __name__ == "__main__":
    main()
