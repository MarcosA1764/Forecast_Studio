# Forecast & Staffing Planner

Local workforce-forecasting and staffing tool. A Python standard-library HTTP server
(`server.py`) serves `index.html` and exposes a small JSON API; the numeric engine lives in
`forecast_engine.py` and the headcount model in `staffing.py`.

Requirements: Python 3.10+ (standard library only). No external packages.

## Run

```powershell
python -m py_compile server.py staffing.py forecast_engine.py check_backtest.py
python server.py
```

The browser opens at <http://127.0.0.1:8000>. If `data/incoming.txt` exists it is loaded on
startup; otherwise the built-in sample (7 weeks) is used. The current 27.5 MB dataset parses to
15,401 rows. Its longest series is **daily = 1,024 points** (weekly = 148, monthly = 34), so a
5,000/15,000-point check is done on a deterministic synthetic series (see
`check_backtest.py`).

## Data sources

| Mode | Endpoint | Notes |
| --- | --- | --- |
| Google Sheets | `POST /api/sheet` | Shared link or "Publish to web" CSV link |
| Upload CSV/TSV | `POST /api/upload` | Replaces the active dataset |
| Paste | `POST /api/paste` | Replaces the active dataset |
| Built-in sample | — | Used when there is no saved data file |

Every successful load goes through a single `load_rows()`, which increments the state
version, clears the compute/backtest caches, persists `data/incoming.txt`, and records
origin/sync time in `data/meta.json`. A dataset change therefore takes effect without
restarting the server.

## Views

- **Headcount overview** — the three-series volume outlook (Actual, Official backtest, Best),
  staffing need, gaps and 24/7 coverage.
- **Data explorer** — actual volume by day/week/month.
- **Forecast methods** — seven-method back-test, ranking, smoothing parameters and the
  step-by-step method inspector. The `Automatic selection` dropdown here is the fallback used
  only when the official forecast default is "statistical best".
- **Official forecast** — the rolling per-period builder described below, the one-step-ahead
  historical backtest panel, saved versions, the calculation ledger and the assumptions in use.
- **Staffing planner** — planning assumptions and the required-versus-paid plan.

## Rolling official forecast

The `Official forecast` view decides the forecast that the whole app uses. For the selected
grain (daily / weekly / monthly) it lists one row per forecast period. Each period gets **one
method** — not a comparison table — plus an optional manual volume:

- **Default method** — the `Default:` selector. `Default: statistical best` resolves each
  future period with the back-test winner; choosing an explicit method applies it to every
  future period that has no override.
- **Per-period method** — the row's `Method` select. `Auto` means "use the default"; any of the
  seven methods overrides the default **for that future period only**. The row is tagged
  `edited`.
- **Manual volume** (optional) — type a number in the row's `Manual` input. The official value
  for that period becomes the manual number and the three channel forecasts are scaled
  proportionally to the method resolved for that period (the rounded channel values sum to the
  manual total, ±1 per channel from rounding). The method is still recorded in `resolved`.

### Chart: Actual + Official + Best

The `Official forecast` chart and the `Headcount overview` chart show **three series**:

1. **Actual** — the historical volume.
2. **Official (`officialMethod`)** — the one-step-ahead official backtest over the historical
   range, followed by the official future forecast.
3. **Best (`bestMethod`)** — the statistical-best backtest over the historical range, followed
   by that method's future forecast, drawn as a thin dotted line.

If a backtest cannot be produced, the chart falls back to Actual + the official future and
shows `Backtest no disponible`; the rest of the screen remains usable. The seven-method
comparison remains in `Forecast methods`, where it belongs.

### Backtest panel

Below the main chart, **Backtest vs. actual** shows one row per historical period:

`Período | Actual | Official (method) | Best (method) | Error Official | Error Best`

The range selector offers **12 / 24 / 52 / All** historical periods. The MAPE figures above the
table are recalculated for the selected range. The backtest is defined as:

```
backtest[t] = forecast(values[:t], method, 1, period, params)[0]   for t = 1..n-1
backtest[0] = null
```

The historical range is **read-only**: the table above it only edits future periods. The
official backtest always uses the **effective default method** (`officialMethod`); future
per-period overrides and manual volumes are deliberately not applied to history (ADR-021).
If the official default and the statistical best resolve to the same method, the server
computes the backtest once and reuses it for both series.

### Save, reset, history and CSV

Changes are previewed immediately: the chart, the table, the overview and the staffing planner
recalculate with the current selection. They are **not** persisted until you press **Save**.
`Save` is disabled while there is nothing new to save. The badge shows `Saved ✓` when the
server state is stored, or `Unsaved changes` while the preview differs from it. **Reset** clears
the default and all overrides, persists that empty state immediately, and returns the chart to
the statistical best.

Each **Save** writes three things:

| File | Contents |
| --- | --- |
| `data/official.json` | Clean state only: `{"default": ..., "periods": {...}}` |
| `data/official.json.history` | JSON list of the latest **20** snapshots (FIFO) |
| `data/forecast_history.csv` | Append-only history: one row per future period per Save |

A snapshot is:

```json
{
  "savedAt": "2026-10-28T18:37:02.494827-06:00",
  "default": "double",
  "periods": {},
  "snapshot_id": "dcae914e"
}
```

`snapshot_id` is the first 8 characters of a SHA-1 of the snapshot (collision-checked).
`savedAt` is ISO-8601 with microseconds and timezone. The history file is append-ordered; the
API returns newest first. Loading a version only rewrites `data/official.json`: it does **not**
create a history snapshot and does **not** write CSV rows.

The CSV header is exactly:

```text
saved_at,snapshot_id,default,period_index,period_label,method,manual,official_value,best_method,best_value,actual_value
```

`actual_value` is empty because these are future periods. Values of `manual` that are not set
are empty. CSV rows are appended for the Save's `grain`/filters; a Save with 12 future months
writes 12 rows, so two monthly saves write 24 data rows. When the current file would exceed
5,000 data rows, it is rotated to `data/forecast_history_YYYY-MM.csv` (with a numeric suffix on
collision) before the next append. The rotation is implemented; the controlled E4 check wrote
24 rows, and this working tree currently has 48 rows from four Saves.

The **Download forecast history (CSV)** button next to Save/Reset downloads the current
append-only file. **Saved versions** is a collapsible panel with the snapshot time, default,
override count and a **Load** button for each version.

The official forecast lives on the server in **`data/official.json`** (created on first save):

```json
{
  "default": "auto",
  "periods": {
    "0": {"method": "trend"},
    "3": {"method": "double"},
    "7": {"manual": 9500}
  }
}
```

All browsers/profiles that point at this server read the same files, and they survive restarts.
There is **no `localStorage`** involved. `data/official.json` is runtime state; the working
copy reflects the last Save/load. Press `Reset` for the empty state
(`{"default": "auto", "periods": {}}`). If you prefer to keep
local preferences out of version control, add `data/official.json`,
`data/official.json.history`, `data/forecast_history*.csv` and `data/backtest/` to `.gitignore`.
It is not a data source: `data/incoming.txt` and `data/meta.json` are.

### Backtest cache

The one-step backtest is incremental, not a call to `forecast()` once per period. It is cached
in memory and on disk:

```text
data/backtest/{method}_{grain}_{form_hash10}_{business}_{channel}.json
```

The disk entry stores a SHA-256 of the values, params and period together with the series. A
data load increments the state version and clears the in-memory caches; a stale disk entry is
detected by the hash and recomputed. The directory is cache-only and can be deleted safely.

### Grain changes and period indices (known limitation)

Overrides are keyed by **forecast-period index**, not by date. Changing the grain moves the
overrides: three monthly overrides become overrides for the first three weeks when switching
to weekly, or the first three days in daily view. This is intentional (ADR-015) because a
period has no stable identity across grains. Press `Reset` after changing grain if the pattern
is not what you want. A date-keyed override model would be a different feature and is out of
scope.

## API

`GET /api/state` returns data-source metadata, defaults and the persisted `official` object.
`POST /api/compute` accepts:

```json
{
  "channel": "Total", "form": "All forms", "business": "Overall", "grain": "monthly",
  "override": "auto", "inspect": "auto", "params": {}, "assumptions": {},
  "official": {"default": "auto", "periods": {}}
}
```

If `official` is omitted or is not an object, the server uses the persisted
`data/official.json`. The response keeps the existing fields (`future`, `comparison`, `staff`,
`ledger`, `channelNeeds`, `coverage`, `explorer`, …) and adds:

- `official` — resolved list, one entry per future period:
  `{method, manual, value, overridden}`.
- `officialDefault` — the effective default method key.
- `officialSaved` — `true` when the request carries a non-auto default or at least one period
  override.
- `officialMethod` — the method actually used for the official historical backtest and future
  default (resolved from `auto`).
- `bestMethod` — the statistical-best method key.
- `backtestOfficial` — array aligned with `history`, `[0] = null`; one-step official backtest.
- `backtestBest` — same shape for `bestMethod`; identical to `backtestOfficial` when the
  methods are equal.
- `backtestMs` — backtest calculation time in milliseconds for this request; `0.0` when the
  whole compute result was a memory-cache hit.

`method` remains for UI compatibility and now equals `officialDefault`.

Official-forecast endpoints:

| Method | Path | Body | Result |
| --- | --- | --- | --- |
| GET | `/api/official` | — | Persisted official object (clean state only) |
| POST | `/api/official` | `{"default": ..., "periods": {...}, "context": {...}}` | Saves state, appends one snapshot and the CSV rows; returns the updated state payload (plus `csvError` if only the CSV failed) |
| POST | `/api/official/reset` | — | Saves `{"default":"auto","periods":{}}` and returns the updated state payload; no snapshot/CSV write |
| GET | `/api/official/history` | — | Latest 20 snapshots, newest first |
| GET | `/api/official/history/csv` | — | `text/csv` attachment `forecast_history.csv` |
| POST | `/api/official/load/{snapshot_id}` | — | Writes that snapshot to `data/official.json` only; returns the state payload |

The optional Save `context` is request-only and is stripped before `data/official.json` is
written. It supplies the CSV's `channel`, `form`, `business`, `grain`, `params` and
`assumptions`; the server recomputes the official/ best values for that context.

## Verification

```powershell
python -m py_compile server.py staffing.py forecast_engine.py check_backtest.py
python check_backtest.py
```

`check_backtest.py` compares the API's real daily backtest at `t = 1, 2, 100, 1023` with
`forecast(values[:t], ...)[0]` (all exact), then checks a deterministic 15,001-point synthetic
series at `t = 5,000` and `t = 15,000` for `double` (official) and `trend`; the trend differences
are at the 1e-12 level. The synthetic checks exist because the loaded daily series maxes out at
1,024 points.

Measured on the delivered dataset (15,401 rows): a cold `/api/compute` daily request took
**7.30 s** wall time and reported `backtestMs = 10.3`; three warm calls took **13–15 ms** and
reported `backtestMs = 0.0`. The cold wall time includes the existing all-method scoring; the
backtest itself is far below the 10 s gate. The measurement was taken with the server's
automatic browser-opening disabled so that a second browser request could not contend with the
first one; the API handler and data are the same.

## Documentation

- `docs/DECISIONS.md` — architecture decision records (ADR-019/020/021 cover this version).
- `docs/VERIFICATION_v4.md` — E1–E11 evidence, commands, timings and screenshots.
- `Docs/Forecast_Staffing_Planner_Source_v19.txt` — source brief.
- `Docs/Forecast_Discrepancy_Assessment.docx` — Excel model comparison.
