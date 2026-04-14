# NHL Prediction Accuracy Tracker — Google Sheet Setup

Two parts:
1. **Local CSV** — `output/accuracy_tracker.csv` is now written automatically every time you run `python nhl_predictor.py`. It backfills winners from `games.csv` and keeps running accuracy up to date.
2. **Google Sheet** — one-time setup to paste or import the CSV, with conditional red/green coloring and a live accuracy cell.

---

## Quick start (manual import, 2 minutes)

This is the easiest path and works forever.

### 1. Create the sheet

Go to [sheets.new](https://sheets.new) (or Google Drive → New → Sheets). Rename the tab **"Tracker"**.

### 2. Set up headers

Paste this into **row 1** (cell A1):

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| date | game | predicted_winner | actual_winner | correct | running_accuracy |

### 3. Add the overall-accuracy summary cell

In cell **H1** paste the label:
```
Overall Accuracy
```

In cell **H2** paste this formula:
```
=IFERROR(COUNTIF(E:E,"YES")/(COUNTIF(E:E,"YES")+COUNTIF(E:E,"NO")),0)
```

Right-click H2 → Format cells → Number → **Percent**. This cell always shows your live win rate.

### 4. Conditional coloring for the "correct" column

Select column **E** (click the letter "E" header). Then:

**Format → Conditional formatting → Add another rule**

Rule 1 — green for correct:
- **Format cells if:** "Text is exactly"
- **Value:** `YES`
- **Formatting style:** green fill

Rule 2 — red for wrong (click "Add another rule"):
- **Format cells if:** "Text is exactly"
- **Value:** `NO`
- **Formatting style:** red fill

Click **Done**.

### 5. (Optional) Color the full row based on the result

If you want the **entire row** to turn red/green instead of just column E:

Select rows 2 down (click row 2 header, then Ctrl/Cmd + Shift + ↓). Format → Conditional formatting → Add rule:

- **Format cells if:** "Custom formula is"
- **Formula for green:** `=$E2="YES"`  → green fill
- Add another rule with formula `=$E2="NO"` → red fill

### 6. Import the CSV

Every time you want to refresh:

**File → Import → Upload** → pick `output/accuracy_tracker.csv` → Import location: **"Replace current sheet"** → Import data.

The conditional formatting and the H2 formula survive the import because they're attached to the cells/column, not the data.

---

## Fully-automated path (advanced, optional)

If you want the sheet to update itself every time you run `python nhl_predictor.py`, use `gspread`. This requires a one-time Google Cloud setup (~15 minutes).

### One-time setup

1. **Create a Google Cloud project and service account** at https://console.cloud.google.com:
   - Create project
   - Enable the **Google Sheets API** and **Google Drive API**
   - Create credentials → Service account → download the JSON key
   - Save the JSON as `gcp_credentials.json` in your project folder
2. **Share your Google Sheet** with the service account's email (it's in the JSON file under `client_email`) — give it Editor access.
3. **Install gspread** in your venv:
   ```bash
   pip install gspread
   ```

### Add this function to `nhl_predictor.py`

Paste below `update_accuracy_tracker`:

```python
def sync_tracker_to_google_sheets(tracker: pd.DataFrame,
                                  sheet_name: str = "NHL Tracker",
                                  creds_path: str = "gcp_credentials.json") -> None:
    """Push the tracker DataFrame to a Google Sheet."""
    try:
        import gspread
    except ImportError:
        log.info("gspread not installed; skipping Google Sheets sync")
        return
    if not Path(creds_path).exists():
        log.info("No %s found; skipping Google Sheets sync", creds_path)
        return
    gc = gspread.service_account(filename=creds_path)
    sh = gc.open(sheet_name)
    ws = sh.sheet1
    ws.clear()
    ws.update([tracker.columns.tolist()] + tracker.astype(str).values.tolist())
    log.info("Synced %d rows to Google Sheet '%s'", len(tracker), sheet_name)
```

Then in `main()`, right after the `update_accuracy_tracker(...)` call:

```python
    tracker_df = update_accuracy_tracker(feats, daily, tracker_path)
    sync_tracker_to_google_sheets(tracker_df)
```

(Capture the return value instead of discarding it.) Create a blank Google Sheet named exactly **"NHL Tracker"**, share it with the service-account email, and every `python nhl_predictor.py` run will now push a fresh copy.

The conditional formatting from the manual setup still applies — gspread only touches the cell values, not the formatting rules.

---

## What the tracker columns mean

| Column | Meaning |
|---|---|
| `date` | Game date (YYYY-MM-DD) |
| `game` | Matchup as `VISITOR @ HOME` |
| `predicted_winner` | Team ID the model picked (pre-game) |
| `actual_winner` | Team ID that actually won — blank until the game plays |
| `correct` | `YES` / `NO` / blank (blank = game not yet played) |
| `running_accuracy` | Running win% across all graded rows, chronological |

Only **graded** rows (where `correct` is YES or NO) affect `running_accuracy` and the `H2` overall cell. Tomorrow's picks show up in the sheet right away but don't count until the games finish and you re-run the pipeline the next morning.
