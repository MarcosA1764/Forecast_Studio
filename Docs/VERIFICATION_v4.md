# Verification — forecast v4 (2026-10-28)

Environment: Windows, Python 3.10+, local server `http://127.0.0.1:8000`, dataset
`data/incoming.txt` (27.5 MB, 15,401 parsed rows). The longest real series is daily = 1,024
points (weekly = 148, monthly = 34). All checks below passed; no git repository was created and
no commit/push was made.

## E1 — visible official + best

`POST /api/compute` and the UI were checked with three official defaults:

| official default | officialMethod | bestMethod | backtests equal | chart legend |
| --- | --- | --- | --- | --- |
| `auto` | `exponential` | `exponential` | yes | `Actual / Official (exponential) / Best (exponential)` |
| `trend` | `trend` | `exponential` | no | `Actual / Official (trend) / Best (exponential)` |
| `double` | `double` | `exponential` | no | `Actual / Official (double) / Best (exponential)` |

The historical part of the Official line is the one-step backtest; the future part is the
official future. The Best line is dotted and uses `bestMethod`. When both methods agree the
server computes the backtest once and returns the same values under both keys.

## E2 — one-step exactness

Command: `python check_backtest.py` → `PASS` (full log: `%TEMP%\fs_v4_check.txt`).

- Real daily API series, official `double`: `t = 1, 2, 100, 1023`; every value equals
  `forecast(values[:t], "double", 1, period)[0]` with diff `0.000e+00`.
- Real daily API series, best `weighted`: same four points; diff `0.000e+00`.
- Deterministic synthetic series (15,001 points), `double`:
  `t = 5,000` and `t = 15,000`; diff `0.000e+00` both.
- Synthetic `trend`: diff `1.364e-12` at `t = 5,000` and `2.728e-12` at `t = 15,000`.
- Arrays are aligned with `history` (same length; `[0] = null`).
- The 5,000/15,000 cases use the synthetic series because the loaded daily series has only
  1,024 points; the real dataset still has 15,401 parsed rows.

## E3 — cold/warm backtest

Measured with the production handler and data, with automatic browser opening disabled to
avoid a second browser request contending with the measurement. Request:
`POST /api/compute {"grain":"daily"}` against the full dataset.

| Run | wall time | `backtestMs` |
| --- | --- | --- |
| cold | **7.295848 s** | **10.3** |
| warm 1 | 0.014942 s | 0.0 |
| warm 2 | 0.013352 s | 0.0 |
| warm 3 | 0.014299 s | 0.0 |

The cold wall time includes the pre-existing scoring of all methods; the incremental backtest
itself is 10.3 ms. This is below the 10 s gate. One earlier run with the browser auto-open
running contended and took 13.48 s; it was repeated isolated as above without changing the
handler or data. The current source is not modified for this measurement.

## E4 — two distinct Saves (controlled test)

The controlled test produced exactly two snapshots and 24 CSV data rows:

| snapshot_id | savedAt | default | overrides |
| --- | --- | --- | --- |
| `07c17ec3` | `2026-10-28T18:37:02.428525-06:00` | `auto` | 1 (period 0, trend) |
| `dcae914e` | `2026-10-28T18:37:02.494827-06:00` | `double` | 0 |

After the acceptance run, the open Brave window made two additional Saves:
`39e01437` (`2026-10-28T18:43:45.963820-06:00`, default `trend`, 1 override) and
`4c51a985` (`2026-10-28T18:44:22.605091-06:00`, default `trend`, 4 overrides). Those are
preserved as user work; the working tree therefore now has **4 snapshots / 48 CSV rows**.
The controlled 2-save behavior and the E6 no-append behavior were verified before those two
later Saves.

The CSV has exactly `header + 24` rows:

```text
saved_at,snapshot_id,default,period_index,period_label,method,manual,official_value,best_method,best_value,actual_value
2026-10-28T18:37:02.428525-06:00,07c17ec3,auto,0,Nov 26,trend,,13567,exponential,10453,
...
2026-10-28T18:37:02.494827-06:00,dcae914e,double,11,Oct 27,double,,178,exponential,10453,
```

`actual_value` is blank in all 24 rows. `data/official.json` contains only
`{"default": ..., "periods": {...}}`; the request `context` was stripped. Full state log:
`%TEMP%\fs_v4_state.txt`.

## E5 — history and CSV endpoints

At the time of the controlled test, `GET /api/official/history` returned the two snapshots
newest first (`dcae914e`, then `07c17ec3`), each with `savedAt`, `default`, `periods` and
`snapshot_id`. The sidecar file itself is append-ordered; the endpoint sorts it. After the two
later user Saves it returns four snapshots newest first (`4c51a985`, `39e01437`, `dcae914e`,
`07c17ec3`). `GET /api/official/history/csv` returns `Content-Type: text/csv; charset=utf-8`
and `Content-Disposition: attachment; filename="forecast_history.csv"`; the controlled file had
the 24 rows above, and the current file has 48 rows.

## E6 — load a saved version

The UI's previous version (`07c17ec3`) was loaded with `POST /api/official/load/07c17ec3`.
Result: default `auto`, future period 0 method `trend`, `overridden: true`; `officialMethod`
remains the resolved default (`exponential`) for the historical backtest, as required by
ADR-021. At that moment history stayed at 2 snapshots and the CSV hash/content did not change.
The official state file was rewritten; no snapshot and no CSV row were added. Screenshot:
`%TEMP%\fs_shots\v4-e6-loaded.png`. (The two later user Saves described in E4 happened after
this check.)

## E7 — CSV download

At the time of the E7 run, the UI download produced filename `forecast_history.csv` with 25
lines (header + 24 rows), the exact header above, and blank `actual_value` on every row. The
server file hash was unchanged by the download. The current file has 48 rows after the later
user Saves. Screenshot evidence is next to the other captures; the downloaded copy is
`%TEMP%\fs_shots\forecast_history.csv`.

## E8 — backtest panel ranges and MAPE

Monthly view:

| range | rows | MAPE official | MAPE best |
| --- | --- | --- | --- |
| `12` | 12 | 28.1% | 13.1% |
| `24` | 24 | 34.9% | 10.2% |
| `52` | 34 (history is shorter) | 36.7% | 10.4% |
| `all` | 34 | 36.7% | 10.4% |

The first row (`t = 0`) shows `—` for both forecasts. Headers are
`PERÍODO / ACTUAL / OFFICIAL (DOUBLE) / BEST (EXPONENTIAL) / ERROR OFFICIAL / ERROR BEST`.
Screenshot: `%TEMP%\fs_shots\v4-e8-backtest12.png`.

## E9 — editable and historical labels

The forecast view shows:

```text
Editable: Nov 26 → Oct 27 (12 months)
Histórico: Oct 23 → Oct 26 (solo proyección, no editable)
```

The historical backtest rows are read-only; the editable table covers future periods only.

## E10 — overview has only three forecast series

Overview legend: `Actual / Official (double) / Best (exponential)`. It has no saved-versions
panel and no backtest panel; the comparison machinery remains in `Forecast methods`.
Screenshot: `%TEMP%\fs_shots\v4-e10-overview.png`.

## E11 — curl timing

`curl.exe -X POST /api/compute --data-binary "@daily.json"`:

- first/cold: **7.295848 s** (`backtestMs = 10.3`), below 10 s;
- warm: **0.014942 s**, **0.013352 s**, **0.014299 s**, all below 500 ms, `backtestMs = 0.0`.

A later request after the working official state changed recomputed the other method once in
7.36 s and then served the same request in 10–242 ms; still within the gates. The point is the
same: the expensive part is the pre-existing all-method scoring, not the incremental backtest.

## UI acceptance run

`node ui_v4_accept.js` (full log: `%TEMP%\fs_v4_ui_accept.txt`) reported
`ACCEPTANCE PASS`; the page had zero page/console errors. It covered E1, E6–E10, including the
three series, load-without-append, CSV download, ranges 12/24/52/all, labels and the overview.
Screenshots:

- `%TEMP%\fs_shots\v4-e1-auto.png`
- `%TEMP%\fs_shots\v4-e1-trend.png`
- `%TEMP%\fs_shots\v4-e6-loaded.png`
- `%TEMP%\fs_shots\v4-e8-backtest12.png`
- `%TEMP%\fs_shots\v4-e10-overview.png`

## Static checks and diff

- `python -m py_compile server.py staffing.py forecast_engine.py check_backtest.py` → OK.
- Inline `index.html` script extracted and `node --check` → OK.
- No `localStorage` or `sessionStorage` in `index.html`.
- `forecast_engine.py` mtime is 17:34, before the v4 edits; it was not modified.
- `git diff --no-index --stat` against `%TEMP%\fs_v3_backup`:
  - `server.py`: 344 insertions, 9 deletions
  - `index.html`: 114 insertions, 18 deletions
  - `README.md`: 156 insertions, 27 deletions
  - `docs/DECISIONS.md`: 135 insertions, 6 deletions
  - `check_backtest.py`: new file
- `docs/DECISIONS.md` gained ADR-019, ADR-020 and ADR-021 and the updated 3-series ADR-012.
  ADR-022 was not created because the trend path is exact.
- No `.git` directory; no init/commit/push.

## Known limitations

- The loaded daily series has 1,024 points; 5,000/15,000 exactness is validated on a
  deterministic synthetic 15,001-point series.
- CSV rotation at >5,000 data rows and the 20-snapshot cap are implemented and documented; the
  workspace currently exercises 48 rows and 4 snapshots (the controlled E4 check used 24 rows
  and 2 snapshots), not the caps.
- The cold timing is the full `/api/compute` wall time (including pre-existing scoring); the
  backtest metric alone is 10.3 ms. Automatic browser opening was disabled for the isolated
  measurement; the API handler and data were unchanged.
