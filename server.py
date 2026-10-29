"""Servidor local — solo librería estándar. Ejecuta:  python server.py"""
import csv
import hashlib
import json
import os
import shutil
import sys
import webbrowser
from collections import deque
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
import re
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from forecast_engine import (DEFAULT_PARAMS, METHODS, SAMPLE, explain, forecast, method_labels, parse_csv,
                             period_label, score_methods, series_by_grain, step, table_by_grain)
from staffing import DEFAULT_ASSUMPTIONS, PAID_BASELINES, plan_for

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    # PyInstaller --onefile extracts bundled files here; it is read-only and temporary.
    BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    USER_DATA = Path(os.environ.get("FORECASTSTUDIO_DATA") or
                     (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ForecastStudio" / "data"))
else:
    BASE = Path(__file__).resolve().parent
    USER_DATA = BASE / "data"

USER_DATA.mkdir(parents=True, exist_ok=True)

DATA = USER_DATA / "incoming.txt"
STATE = {"rows": SAMPLE, "status": "Sample data · 7 weeks (built-in)", "version": 0,
         "origin": "sample", "synced": ""}
_CACHE = {}
_BACKTEST_CACHE = {}
META = USER_DATA / "meta.json"

ORIGIN_LABEL = {"sample": "Sample data", "sheet": "Google Sheet", "file": "Uploaded file", "paste": "Pasted data"}

SOURCE = USER_DATA / "source.json"
OFFICIAL = USER_DATA / "official.json"
OFFICIAL_HISTORY = USER_DATA / "official.json.history"
FORECAST_HISTORY_CSV = USER_DATA / "forecast_history.csv"
BACKTEST_DIR = USER_DATA / "backtest"
CSV_HEADER = ["saved_at", "snapshot_id", "default", "period_index", "period_label", "method",
              "manual", "official_value", "best_method", "best_value", "actual_value"]
GOOGLE_HOST = "docs.google.com"


def prepare_user_data():
    """Create the writable data directory and install first-run seeds.

    In a PyInstaller one-file build, BASE/_MEIPASS is temporary and read-only.
    The seed files are copied once to USER_DATA; later runs preserve user data.
    """
    USER_DATA.mkdir(parents=True, exist_ok=True)
    if not FROZEN:
        return

    seed_dir = BASE / "data"
    for filename, seed_name in (("official.json", "official.seed.json"),
                                ("incoming.txt", "incoming.txt")):
        target = USER_DATA / filename
        seed = seed_dir / seed_name
        if not target.exists() and seed.exists():
            shutil.copyfile(seed, target)

    if not OFFICIAL.exists():
        OFFICIAL.write_text(json.dumps({"default": "auto", "periods": {}}, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def clean_official(payload):
    """Keep only the persisted official state (strip request-only context)."""
    if not isinstance(payload, dict):
        return {"default": "auto", "periods": {}}
    default = payload.get("default")
    if not isinstance(default, str) or not default:
        default = "auto"
    periods = payload.get("periods")
    if not isinstance(periods, dict):
        periods = {}
    return {"default": default, "periods": periods}


def load_official():
    try:
        official = json.loads(OFFICIAL.read_text(encoding="utf-8-sig")) if OFFICIAL.exists() else {}
        return clean_official(official)
    except Exception:  # noqa: BLE001
        return {"default": "auto", "periods": {}}


def save_official(official):
    OFFICIAL.parent.mkdir(exist_ok=True)
    OFFICIAL.write_text(json.dumps(clean_official(official), ensure_ascii=False, indent=2), encoding="utf-8")


def load_history():
    try:
        data = json.loads(OFFICIAL_HISTORY.read_text(encoding="utf-8-sig")) if OFFICIAL_HISTORY.exists() else []
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def write_history(items):
    OFFICIAL_HISTORY.parent.mkdir(exist_ok=True)
    OFFICIAL_HISTORY.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_id(snapshot, existing):
    raw = json.dumps({k: snapshot[k] for k in ("savedAt", "default", "periods")},
                     sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    sid = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    salt = 0
    while sid in existing:
        salt += 1
        sid = hashlib.sha1((raw + "#" + str(salt)).encode("utf-8")).hexdigest()[:8]
    return sid


def append_history(official, saved_at):
    items = load_history()
    snapshot = {"savedAt": saved_at, "default": official["default"],
                "periods": official["periods"]}
    snapshot["snapshot_id"] = _snapshot_id(snapshot, {s.get("snapshot_id") for s in items if isinstance(s, dict)})
    items.append(snapshot)
    write_history(items[-20:])  # FIFO: keep the latest 20 snapshots
    return snapshot


def load_source():
    try:
        return json.loads(SOURCE.read_text(encoding="utf-8-sig")) if SOURCE.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def save_source(url, tab):
    SOURCE.parent.mkdir(exist_ok=True)
    SOURCE.write_text(json.dumps({"url": url, "tab": tab}), encoding="utf-8")


def google_csv_url(source, sheet="Incoming"):
    u = urlparse(source.strip())
    if u.scheme != "https" or u.netloc != GOOGLE_HOST or not u.path.startswith("/spreadsheets/"):
        raise ValueError("Only Google Sheets links from " + GOOGLE_HOST + " are supported.")
    base = "https" + "://" + GOOGLE_HOST
           # Formato 'Publicar en la web' (…/d/e/XXXX/pub?gid=0&single=true&output=csv)
    if "/spreadsheets/d/e/" in u.path and u.path.rstrip("/").endswith("/pub"):
        q = parse_qs(u.query)
        q["output"] = ["csv"]
        query = "&".join(f"{k}={v[0]}" for k, v in q.items())
        return base + u.path + "?" + query
    # Formato normal de edicion (…/d/ID/edit?gid=0)
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", u.path)
    if not m:
        raise ValueError("This Google Sheets link format is not recognized.")
    sid = m.group(1)
    gid = parse_qs(u.query).get("gid", [None])[0]
    if gid is None and u.fragment.startswith("gid="):
        gid = u.fragment[4:]
    if gid is not None:
        return base + "/spreadsheets/d/" + sid + "/export?format=csv&gid=" + gid
    return base + "/spreadsheets/d/" + sid + "/gviz/tq?tqx=out:csv&sheet=" + quote(sheet)


def fetch_google_csv(source, sheet="Incoming"):
    if not source.strip():
        raise ValueError("Enter a Google Sheet URL.")
    req = Request(google_csv_url(source, sheet), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8-sig")
    except HTTPError as e:
        raise ValueError(f"Google Sheets denied access (HTTP {e.code}). Share the sheet as 'Anyone with the link - Viewer'.")
    except URLError as e:
        raise ValueError(f"The Google Sheet could not be reached ({e.reason}). Check internet/proxy access.")
    if not text.strip() or text.lstrip().startswith("<"):
        raise ValueError("Google returned a web page instead of CSV. Confirm sharing or use the 'Publish to web' CSV link.")
    return text

def load_rows(text, origin):
    STATE["rows"] = parse_csv(text)
    STATE["version"] += 1
    _CACHE.clear()
    _BACKTEST_CACHE.clear()
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(text, encoding="utf-8")
    STATE["origin"] = origin
    STATE["synced"] = datetime.now().strftime("%d-%m-%Y %H:%M")
    STATE["status"] = f"{ORIGIN_LABEL.get(origin, origin)} · {len(STATE['rows'])} dated periods"
    META.write_text(json.dumps({"origin": origin, "synced": STATE["synced"]}), encoding="utf-8")


CHANNELS = ("Voice", "Messaging", "Email")


# ---------------------------------------------------------------- datos
def state_payload():
    rows = STATE["rows"]
    businesses = sorted({r.get("business") for r in rows
                         if r.get("business") and r.get("business") not in ("Unspecified", "Overall")})
    return {
        "status": STATE["status"],
        "forms": ["All forms"] + sorted({r["form"] for r in rows}),
        "businesses": ["Overall"] + businesses,
        "channels": ["Total", "Voice", "Messaging", "Email"],
        "latestWeek": max((r["week"] for r in rows), default=""),
        "defaults": DEFAULT_ASSUMPTIONS,
        "baselines": PAID_BASELINES,
        "methods": METHODS,
        "params": DEFAULT_PARAMS,
        "source": load_source(),
        "official": load_official(),
        "origin": STATE["origin"],
        "synced": STATE["synced"],
    }


# ---------------------------------------------------------------- backtest
def _smoothing(params):
    """Same normalization as forecast_engine._params (without importing a private name)."""
    p = dict(DEFAULT_PARAMS)
    for k in p:
        if params and params.get(k) is not None:
            try:
                p[k] = min(max(float(params[k]), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
    return p


def backtest_series(values, method, period=52, params=None):
    """One-step-ahead backtest in O(n) per method (O(n·period) for trend).

    out[t] = forecast(values[:t], method, 1, period, params)[0] for t = 1..n-1;
    out[0] = None.  The states below reproduce forecast() incrementally instead of
    calling forecast() once per t (which would be O(n²)).
    """
    n = len(values)
    out = [None] * n
    if n == 0:
        return out
    pr = _smoothing(params)

    if method == "average":
        total = values[0]
        for t in range(1, n):
            out[t] = total / t
            total += values[t]

    elif method == "moving":
        win = deque([values[0]])
        total = values[0]
        for t in range(1, n):
            out[t] = total / len(win)
            win.append(values[t])
            total += values[t]
            if len(win) > 4:
                total -= win.popleft()

    elif method == "weighted":
        weights = [8, 7, 6, 5, 4, 3, 2, 1]
        win = deque([values[0]])
        for t in range(1, n):
            wts = weights[-len(win):]
            out[t] = sum(x * w for x, w in zip(win, wts)) / sum(wts)
            win.append(values[t])
            if len(win) > 8:
                win.popleft()

    elif method == "exponential":
        alpha = pr["alpha"]
        level = values[0]
        for t in range(1, n):
            out[t] = level
            level = alpha * values[t] + (1 - alpha) * level

    elif method == "double":
        alpha, beta = pr["dAlpha"], pr["dBeta"]
        level = values[0]
        trend = None
        for t in range(1, n):
            out[t] = max(0.0, level + (trend or 0.0))
            x = values[t]
            if trend is None:               # first update: forecast([v0, v1])
                trend = x - level
            previous = level
            level = alpha * x + (1 - alpha) * (level + trend)
            trend = beta * (level - previous) + (1 - beta) * trend

    elif method == "cma":
        total = values[0]
        ma = []
        for t in range(1, n):
            if t >= 4:
                ma.append(sum(values[t - 4:t]) / 4)
            if not ma:                      # fewer than 4 points: fall back to the average
                out[t] = total / t
            elif len(ma) == 1:              # 4 points: one MA, no centered pair yet
                out[t] = ma[-1]
            else:
                out[t] = (ma[-2] + ma[-1]) / 2
            total += values[t]

    elif method == "trend":
        # Exact incremental replication of linear() + centered_moving_average(4) +
        # seasonal_indices(..., period, 0) + ts_forecast(..., h=1).
        total = values[0]
        sxy = 0.0
        sxx = 0.0
        ma = []
        buckets = [[] for _ in range(period)]
        for t in range(1, n):
            if t > 1:
                x = values[t - 1]
                total += x
                sxy += (t - 1) * x
                sxx += (t - 1) * (t - 1)
                if t >= 4:
                    ma.append(sum(values[t - 4:t]) / 4)
                if t >= 5:
                    center = (ma[-2] + ma[-1]) / 2
                    if center:
                        buckets[(t - 5) % period].append(values[t - 3] / center)
            idx = [sum(b) / len(b) if b else 1.0 for b in buckets]
            mean = sum(idx) / period
            idx = [x / mean if mean else 1.0 for x in idx]
            sx = t * (t - 1) / 2
            den = t * sxx - sx * sx
            slope = (t * sxy - sx * total) / (den if den else 1) if t > 1 else 0.0
            intercept = (total - slope * sx) / max(t, 1)
            out[t] = max(0.0, (intercept + slope * t) * idx[t % period])

    else:
        return backtest_series(values, "average", period, params)
    return out


def _safe_name(value):
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
    return name[:48] or "all"


def _backtest_path(method, grain, form, business, channel):
    form_hash = hashlib.sha1(str(form).encode("utf-8")).hexdigest()[:10]
    return BACKTEST_DIR / (f"{_safe_name(method)}_{_safe_name(grain)}_{form_hash}_"
                           f"{_safe_name(business)}_{_safe_name(channel)}.json")


def _backtest(values, method, grain, form, business, channel, period, params):
    """Return (series, elapsed_ms), using memory then disk cache."""
    params = params or {}
    params_hash = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    key = (method, grain, form, business, channel, params_hash, STATE["version"])
    cached = _BACKTEST_CACHE.get(key)
    if cached is not None:
        return cached, 0.0

    started = perf_counter()
    path = _backtest_path(method, grain, form, business, channel)
    values_hash = hashlib.sha256(json.dumps({"values": values, "params": params, "period": period},
                                            sort_keys=True, default=str).encode("utf-8")).hexdigest()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            series = saved.get("series")
            if saved.get("values_hash") == values_hash and isinstance(series, list) and len(series) == len(values):
                _BACKTEST_CACHE[key] = series
                return series, (perf_counter() - started) * 1000
        except Exception:  # noqa: BLE001
            pass

    series = backtest_series(values, method, period, params)
    _BACKTEST_CACHE[key] = series
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"values_hash": values_hash, "series": series}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError as e:
        print(f"Backtest cache write failed: {e}", file=sys.stderr)
    return series, (perf_counter() - started) * 1000


# ---------------------------------------------------------------- cálculo
def _compute_core(rows, channel, form, business, grain, override, params, a, official):
    period = {"daily": 7, "weekly": 52, "monthly": 12}[grain]
    horizon = 30 if grain == "daily" else 12
    period_days = 1 if grain == "daily" else 5 if grain == "weekly" else a["days"]
    period_name = {"daily": "Day", "weekly": "Week", "monthly": "Month"}[grain]

    history = series_by_grain(rows, channel, form, business, grain)
    values = [s["value"] for s in history]
    scores = score_methods(values, period, params)
    best = scores[0]["method"] if scores else "weighted"

    last = datetime.strptime(history[-1]["date"], "%Y-%m-%d").date() if history else date.today()
    labels = [period_label(step(last, i + 1, grain), grain) for i in range(horizon)]
    comparison = {m: [round(x) for x in forecast(values, m, horizon, period, params)] for m in METHODS}
    channel_comparison = {}
    for ch in CHANNELS:
        vals = [s["value"] for s in series_by_grain(rows, ch, form, business, grain)]
        channel_comparison[ch] = {m: [round(x) for x in forecast(vals, m, horizon, period, params)]
                                  for m in METHODS}

    # Totales históricos por canal (reparto regular / fin de semana), agregados una sola vez.
    # Se pasan a plan_for como un único dict en vez de recorrer las filas en cada llamada.
    hist = {k: 0 for ch in CHANNELS for k in (ch, ch + "Off")}
    for r in rows:
        if business != "Overall" and r.get("business") != business:
            continue
        for ch in CHANNELS:
            hist[ch] += r.get(ch, 0)
            hist[ch + "Off"] += r.get(ch + "Off", 0)

    # ---- Método por defecto: official.default > override (página Methods) > statistical best
    default_m = official.get("default") or "auto"
    if default_m == "auto":
        default_m = best if override == "auto" else override
    if default_m not in METHODS:
        default_m = best

    # ---- Backtest one-step-ahead: only the effective official default + the best.
    # Historical backtest uses the default method; per-period overrides/manuals are future-only.
    bt_started = perf_counter()
    backtest_official, _ = _backtest(values, default_m, grain, form, business, channel, period, params)
    if best == default_m:
        backtest_best = backtest_official
    else:
        backtest_best, _ = _backtest(values, best, grain, form, business, channel, period, params)
    backtest_ms = round((perf_counter() - bt_started) * 1000, 1)

    # ---- Pronóstico oficial período a período (default + override + manual)
    per = official.get("periods") or {}
    if not isinstance(per, dict):
        per = {}
    resolved, future = [], []
    chvalues = {ch: [] for ch in CHANNELS}
    for i in range(horizon):
        o = per.get(str(i)) or {}
        if not isinstance(o, dict):
            o = {}
        m = o.get("method") or default_m
        if m not in METHODS:
            m = default_m
        manual = o.get("manual")
        try:
            manual = float(manual) if manual not in (None, "") else None
        except (TypeError, ValueError):
            manual = None
        base_ch = {ch: channel_comparison[ch][m][i] for ch in CHANNELS}
        if manual is not None:
            total = round(manual)
            s = sum(base_ch.values())
            scale = manual / s if s else 0
            for ch in CHANNELS:
                chvalues[ch].append(round(base_ch[ch] * scale))
        else:
            total = comparison[m][i]
            for ch in CHANNELS:
                chvalues[ch].append(base_ch[ch])
        future.append(total)
        resolved.append({"method": m, "manual": manual, "value": total,
                         "overridden": bool(per.get(str(i)))})

    def plan(ch, vol):
        return plan_for(ch, vol, hist, a, period_days)

    ledger, staff = [], []
    for i, label in enumerate(labels):
        chans = list(CHANNELS) if channel == "Total" else [channel]
        plans = [plan(ch, chvalues[ch][i] if channel == "Total" else future[i]) for ch in chans]
        value = sum(x["volume"] for x in plans)
        required = sum(x["required"] for x in plans)
        available = sum(x["available"] for x in plans)
        capacity = sum(x["capacity"] for x in plans)
        staff.append({"period": label, "value": value, "required": required, "available": available,
                      "gap": available - required, "capacity": capacity,
                      "occupancy": value / capacity * 100 if capacity else 0})
        for ch in CHANNELS:
            row = plan(ch, chvalues[ch][i])
            row["period"] = label
            row["method"] = resolved[i]["method"]
            ledger.append(row)

    channel_needs = []
    for ch in CHANNELS:
        row = plan(ch, chvalues[ch][0])
        row["method"] = resolved[0]["method"]
        channel_needs.append(row)

    coverage = []
    for n in channel_needs:
        if n["channel"] == "Email":
            continue
        name = "Chat" if n["channel"] == "Messaging" else n["channel"]
        for seg, vol, req, paid in (("Regular shifts", n["regularVolume"], n["regularRequired"], n["regularPaid"]),
                                    ("Weekend / midnight", n["offVolume"], n["offRequired"], n["offPaid"])):
            coverage.append({"channel": name, "segment": seg, "volume": vol,
                             "share": vol / max(n["volume"], 1) * 100,
                             "required": req, "paid": paid, "gap": paid - req})

    return {
        "periodName": period_name, "horizon": horizon, "periodDays": period_days, "seasonPeriod": period,
        "method": default_m, "officialDefault": default_m, "official": resolved, "best": best,
        "bestMethod": best, "officialMethod": default_m,
        "backtestOfficial": backtest_official, "backtestBest": backtest_best, "backtestMs": backtest_ms,
        "scores": scores, "values": values,
        "history": history, "futureLabels": labels, "future": future, "comparison": comparison,
        "channelComparison": channel_comparison, "hist": hist,
        "channelNeeds": channel_needs, "coverage": coverage, "ledger": ledger, "staff": staff,
        "explorer": {g: table_by_grain(rows, form, business, g) for g in ("daily", "weekly", "monthly")},
    }


def compute(rows, p):
    channel = p.get("channel", "Total")
    form = p.get("form", "All forms")
    business = p.get("business", "Overall")
    grain = p.get("grain", "monthly")
    override = p.get("override", "auto")
    inspect = p.get("inspect") or "auto"
    params = p.get("params") or {}
    official = p.get("official")
    if not isinstance(official, dict):
        official = load_official()
    a = dict(DEFAULT_ASSUMPTIONS)
    for k, v in (p.get("assumptions") or {}).items():
        a[k] = float(v)

    key = json.dumps([STATE["version"], channel, form, business, grain, override,
                      sorted(params.items()), sorted(a.items()), official],
                     sort_keys=True, default=str)
    core = _CACHE.get(key)
    fresh = core is None
    if fresh:
        core = _compute_core(rows, channel, form, business, grain, override, params, a, official)
        _CACHE[key] = core

    labels = method_labels(params)
    target = core["method"] if inspect == "auto" else inspect
    exp = explain(core["values"], target, core["horizon"], core["seasonPeriod"], params)
    exp["title"] = labels.get(target, exp["title"])

    periods = official.get("periods")
    official_saved = (isinstance(periods, dict) and bool(periods)) or (official.get("default") or "auto") != "auto"

    out = dict(core)
    for internal in ("channelComparison", "hist", "best"):
        out.pop(internal, None)
    out.update({
        "methods": labels, "params": params, "assumptions": a, "officialSaved": official_saved,
        "inspect": target, "explanation": exp, "status": STATE["status"],
        "backtestMs": core["backtestMs"] if fresh else 0.0,
    })
    return out

# ---------------------------------------------------------------- history files
def _rotate_history_csv():
    """Rotate once the current file has more than 5 000 data rows."""
    if not FORECAST_HISTORY_CSV.exists():
        return
    try:
        with FORECAST_HISTORY_CSV.open(encoding="utf-8") as f:
            lines = sum(1 for _ in f)
    except OSError:
        return
    if lines <= 5001:                       # header + <= 5000 data rows
        return
    stamp = datetime.now().strftime("%Y-%m")
    target = USER_DATA / f"forecast_history_{stamp}.csv"
    n = 1
    while target.exists():
        n += 1
        target = USER_DATA / f"forecast_history_{stamp}_{n}.csv"
    try:
        FORECAST_HISTORY_CSV.replace(target)
    except OSError as e:
        print(f"Forecast history rotation failed: {e}", file=sys.stderr)


def append_history_csv(official, snapshot, context):
    """Append one row per future period. Returns the number of rows written."""
    ctx = context if isinstance(context, dict) else {}
    grain = ctx.get("grain")
    if grain not in ("daily", "weekly", "monthly"):
        grain = "monthly"
    params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
    assumptions = ctx.get("assumptions") if isinstance(ctx.get("assumptions"), dict) else {}
    result = compute(STATE["rows"], {
        "channel": ctx.get("channel") or "Total",
        "form": ctx.get("form") or "All forms",
        "business": ctx.get("business") or "Overall",
        "grain": grain,
        "params": params,
        "assumptions": assumptions,
        "official": official,
    })
    best_method = result.get("bestMethod") or result.get("best")
    best_values = (result.get("comparison") or {}).get(best_method) or []
    rows = []
    for i, label in enumerate(result.get("futureLabels") or []):
        resolved = (result.get("official") or [])[i]
        rows.append([
            snapshot["savedAt"], snapshot["snapshot_id"], official["default"], i, label,
            resolved["method"], "" if resolved["manual"] is None else resolved["manual"],
            resolved["value"], best_method, best_values[i] if i < len(best_values) else "",
            "",                              # actual_value: future periods have no actual yet
        ])
    if not rows:
        return 0
    _rotate_history_csv()
    new_file = not FORECAST_HISTORY_CSV.exists() or FORECAST_HISTORY_CSV.stat().st_size == 0
    with FORECAST_HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        if new_file:
            writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    return len(rows)


def persist_official(payload):
    """Save current state + snapshot, then append CSV rows. CSV errors never block the Save."""
    official = clean_official(payload)
    save_official(official)
    saved_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    snapshot = append_history(official, saved_at)
    state = state_payload()
    try:
        append_history_csv(official, snapshot, payload.get("context") if isinstance(payload, dict) else None)
    except Exception as e:  # noqa: BLE001
        print(f"Forecast history CSV append failed: {e}", file=sys.stderr)
        state["csvError"] = str(e)
    return state


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8-sig") if n else ""

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (BASE / "index.html").read_bytes(), "text/html")
        elif path == "/api/state":
            self._send(200, state_payload())
        elif path == "/api/official":
            self._send(200, load_official())
        elif path == "/api/official/history":
            history = sorted(load_history(), key=lambda s: s.get("savedAt", ""), reverse=True)
            self._send(200, history)
        elif path == "/api/official/history/csv":
            if FORECAST_HISTORY_CSV.exists():
                data = FORECAST_HISTORY_CSV.read_bytes()
            else:
                data = ("\n".join(CSV_HEADER) + "\n").encode("utf-8")
            self._send(200, data, "text/csv",
                       {"Content-Disposition": 'attachment; filename="forecast_history.csv"'})
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/compute":
                self._send(200, compute(STATE["rows"], json.loads(self._body() or "{}")))
            elif path == "/api/official/reset":
                save_official({"default": "auto", "periods": {}})
                self._send(200, state_payload())
            elif path == "/api/official":
                payload = json.loads(self._body() or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("Official forecast payload must be a JSON object.")
                self._send(200, persist_official(payload))
            elif path.startswith("/api/official/load/"):
                snapshot_id = unquote(path[len("/api/official/load/"):])
                snapshot = next((s for s in load_history() if s.get("snapshot_id") == snapshot_id), None)
                if not snapshot:
                    self._send(404, {"error": "Snapshot not found"})
                    return
                save_official(snapshot)          # load only; do not append a snapshot or CSV rows
                self._send(200, state_payload())
            elif path == "/api/upload":
                load_rows(self._body(), "file")
                self._send(200, state_payload())
            elif path == "/api/paste":
                p = json.loads(self._body() or "{}")
                load_rows(p.get("text", ""), "paste")
                self._send(200, state_payload())
            elif path == "/api/sheet":
                p = json.loads(self._body() or "{}")
                saved = load_source()
                url = (p.get("url") or saved.get("url") or "").strip()
                tab = (p.get("tab") or saved.get("tab") or "Incoming").strip()
                load_rows(fetch_google_csv(url, tab), "sheet")
                save_source(url, tab)
                self._send(200, state_payload())
            else:
                self._send(404, {"error": "Not found"})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})

    def log_message(self, *_):
        pass

if __name__ == "__main__":
    prepare_user_data()

    if DATA.exists():
        try:
            meta = json.loads(META.read_text(encoding="utf-8-sig")) if META.exists() else {}
            load_rows(DATA.read_text(encoding="utf-8-sig"), meta.get("origin", "file"))
            if meta.get("synced"):
                STATE["synced"] = meta["synced"]
                META.write_text(json.dumps(meta), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            STATE["status"] = f"Saved data could not be parsed: {e}"
    port = 8000
    host = "127.0.0.1"
    url = "http" + "://" + host + ":" + str(port)

    print(flush=True)
    print("=" * 60, flush=True)
    print(f"  Your App is running on the Local Server : {url}", flush=True)
    print("=" * 60, flush=True)
    print("  Close this window to stop the server.", flush=True)
    print(flush=True)

    import threading
    import time

    def _open_browser():
        time.sleep(1.0)
        webbrowser.open(url)

    if not os.environ.get("FORECASTSTUDIO_NO_BROWSER"):
        threading.Thread(target=_open_browser, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()