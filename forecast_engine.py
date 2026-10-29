"""Motor de pronóstico alineado al modelo Excel (ver Forecast_Discrepancy_Assessment.docx)."""
from __future__ import annotations
import csv, io, math, re
from datetime import datetime, date, timedelta

METHODS = {
    "average":     "Historical average",
    "moving":      "Moving average (4 periods)",
    "weighted":    "Weighted moving average (8 periods)",
    "exponential": "Exponential smoothing (α 0.50)",
    "double":      "Double exponential (α 0.2853, β 0.0131)",
    "trend":       "Time series (trend × seasonality)",
    "cma":         "Centered moving average",
}
SMOOTHING = {
    "exponential": {"alpha": 0.5},
    # Valores exactos del Excel — no redondear
    "double": {"alpha": 0.28534814239502798, "beta": 0.013050873170878814},
}
# Excel: MAPE 30 %, WAPE 40 %.  ⚠ El reparto del 30 % restante no está en el docx: confírmalo.
SCORE_WEIGHTS = {"mape": .30, "wape": .40, "variance": .075, "correlation": .075, "stdev": .075, "iqr": .075}

# Parámetros de suavizado editables desde el front-end (defaults = Excel)
DEFAULT_PARAMS = {
    "alpha": 0.5,                     # exponential smoothing
    "dAlpha": 0.28534814239502798,    # double exponential α
    "dBeta": 0.013050873170878814,    # double exponential β
}

def _params(p):
    out = dict(DEFAULT_PARAMS)
    for k in out:
        if p and p.get(k) is not None:
            try:
                out[k] = min(max(float(p[k]), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
    return out

_DATE_CACHE = {}

def _to_date(s):
    d = _DATE_CACHE.get(s)
    if d is None:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        _DATE_CACHE[s] = d
    return d


def method_labels(params=None):
    """Nombres de método con los parámetros actuales (no los defaults)."""
    pr = _params(params)
    labels = dict(METHODS)
    labels["exponential"] = f"Exponential smoothing (α {pr['alpha']:.2f})"
    labels["double"] = f"Double exponential (α {pr['dAlpha']:.4f}, β {pr['dBeta']:.4f})"
    return labels

# ---------- 1) CMA verdadero ----------
def centered_moving_average(values, window=4):
    n = len(values)
    if n < window:
        return []
    ma = [sum(values[i:i + window]) / window for i in range(n - window + 1)]
    return [(ma[i] + ma[i + 1]) / 2 for i in range(len(ma) - 1)]   # cma[k] ↔ values[k + window/2]

# ---------- 2) Índices estacionales ----------
def seasonal_indices(values, cma, window=4, period=52, start_offset=0):
    si = []
    for k, c in enumerate(cma):
        j = k + window // 2
        if j < len(values) and c:
            si.append(values[j] / c)
    buckets = [[] for _ in range(period)]
    for t, s in enumerate(si):
        buckets[(start_offset + t) % period].append(s)
    idx = [sum(b) / len(b) if b else 1.0 for b in buckets]
    mean = sum(idx) / period
    return [x / mean if mean else 1.0 for x in idx]

# ---------- 3) Tendencia lineal ----------
def linear(v):
    n = len(v)
    sx = n * (n - 1) / 2
    sy = sum(v)
    sxx = sum(i * i for i in range(n))
    sxy = sum(i * x for i, x in enumerate(v))
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / (den if den else 1) if n > 1 else 0.0
    return slope, (sy - slope * sx) / max(n, 1)

# ---------- 4) TS = Tendencia × Índice estacional ----------
def ts_forecast(values, horizon, trend, season_idx, start_offset=0, period=52):
    n = len(values)
    slope, intercept = trend
    out = []
    for i in range(horizon):
        t = n + i
        s = season_idx[(start_offset + t) % period] if season_idx else 1.0
        out.append(max(0.0, (intercept + slope * t) * s))
    return out

# ---------- 5) forecast(): métodos planos SIN crecimiento compuesto ----------
def forecast(v, method, h=12, period=52, params=None):
    pr = _params(params)
    a = list(v) if v else [0.0]
    avg = sum(a) / len(a)
    r = a[-4:]; mov = sum(r) / len(r)
    w = a[-8:]; wts = [8, 7, 6, 5, 4, 3, 2, 1][-len(w):]
    wma = sum(x * k for x, k in zip(w, wts)) / sum(wts)
    sa = pr["alpha"]; level = a[0]
    for x in a[1:]:
        level = sa * x + (1 - sa) * level
    da, db = pr["dAlpha"], pr["dBeta"]
    hl, ht = a[0], (a[1] - a[0] if len(a) > 1 else 0.0)
    for x in a[1:]:
        p = hl
        hl = da * x + (1 - da) * (hl + ht)
        ht = db * (hl - p) + (1 - db) * ht
    cma = centered_moving_average(a, 4)
    cma_last = cma[-1] if cma else avg
    ts = ts_forecast(a, h, linear(a), seasonal_indices(a, cma, 4, period, 0), 0, period)
    out = []
    for i in range(h):
        if method == "average":       val = avg
        elif method == "moving":      val = mov
        elif method == "weighted":    val = wma
        elif method == "exponential": val = level
        elif method == "double":      val = max(0.0, hl + ht * (i + 1))
        elif method == "cma":         val = cma_last
        elif method == "trend":       val = ts[i]
        else:                         val = avg
        out.append(val)
    return out
# ---------- 6) Back-test y ranking ----------
def score_methods(v, period=52, params=None):
    items = []
    for method in METHODS:
        e = [(v[i], forecast(v[:i], method, 1, period, params)[0]) for i in range(max(2, len(v) // 3), len(v))]
        if not e:
            last = v[-1] if v else 0
            e = [(last, forecast(v[:-1], method, 1, period, params)[0] or last)]

        ab = [abs(a - f) for a, f in e]
        mape = sum(x / max(e[i][0], 1) for i, x in enumerate(ab)) / len(e) * 100
        wape = sum(ab) / max(sum(a for a, _ in e), 1) * 100
        mean = sum(f for _, f in e) / len(e)
        variance = sum((f - mean) ** 2 for _, f in e) / len(e)
        am = sum(a for a, _ in e) / len(e)
        cov = sum((a - am) * (f - mean) for a, f in e)
        den = math.sqrt(sum((a - am) ** 2 for a, _ in e) * sum((f - mean) ** 2 for _, f in e))
        items.append({"method": method, "mape": mape, "wape": wape, "variance": variance,
                      "correlation": cov / den if den else 0.0, "stdev": math.sqrt(variance),
                      "iqr": max(ab) - min(ab), "score": 0.0})
    def rank(k, asc=True):
        return [x["method"] for x in sorted(items, key=lambda x: x[k], reverse=not asc)]
    r = {k: rank(k, asc=(k != "correlation")) for k in SCORE_WEIGHTS}
    for x in items:
        x["score"] = sum((r[k].index(x["method"]) + 1) * w for k, w in SCORE_WEIGHTS.items())
    return sorted(items, key=lambda x: x["score"])

# ---------- 7) Fechas y series ----------
def date_key(d): return d.strftime("%Y-%m-%d")

def period_label(d, grain):
    if grain == "monthly":
        return d.strftime("%b %y")
    s = f"{d.strftime('%b')} {d.day}"
    return f"Week of {s}" if grain == "weekly" else s

def step(d, k, grain):
    if grain == "daily":  return d + timedelta(days=k)
    if grain == "weekly": return d + timedelta(days=7 * k)
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)

def series_by_grain(rows, channel, form="All forms", business="Overall", grain="monthly"):
    m = {}
    for r in rows:
        if form != "All forms" and r["form"] != form: continue
        if business != "Overall" and r.get("business") != business: continue
        src = r["week"] if grain == "weekly" else (r.get("date") or r["week"])
        d = _to_date(src)
        p = d.replace(day=1) if grain == "monthly" else d
        x = m.setdefault(date_key(p), {"date": p, "value": 0})
        x["value"] += r["total"] if channel == "Total" else r.get(channel, 0)
    return [{"date": date_key(x["date"]), "label": period_label(x["date"], grain), "value": x["value"]}
            for x in sorted(m.values(), key=lambda x: x["date"])]

# ---------- 8) Parser CSV (hoja "Incoming", columnas A–I) ----------
# A Ticket ID · B Created time · C Ticket form · D Ticket channel · E Equinox or Kipu
# F Ticket Skills · G Handle time · H Wait time · I Week
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
            "%m/%d/%Y", "%m/%d/%y %H:%M", "%m/%d/%y", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%b %d, %Y %H:%M", "%b %d, %Y")

def parse_date(s):
    s = (s or "").strip()
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError: pass
    for f in _FORMATS:
        try: return datetime.strptime(s, f)
        except ValueError: continue
    return None

def parse_csv(text):
    text = text.replace("\ufeff", "")
    sample = "\n".join(text.splitlines()[:10])
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("No data rows found in Incoming.")
    
    clean = lambda x: re.sub(r"[^a-z0-9]+", " ", x.replace("\ufeff", "").strip().lower()).strip()
    header_row = 0
    for i, r in enumerate(rows[:10]):
        h = [clean(c) for c in r]
        if any("ticket form" in c for c in h) and any("ticket channel" in c for c in h):
            header_row = i; break
    hdr = [clean(c) for c in rows[header_row]]
    def idx(names):
        for i, h in enumerate(hdr):
            if any(n in h for n in names): return i
        return -1
    fixed = len(hdr) >= 9
    pick = lambda names, fb: idx(names) if idx(names) >= 0 else (fb if fixed else -1)
    created, weeki = idx(["created time", "created at", "created", "timestamp"]), idx(["week"])
    di = created if created >= 0 else (1 if fixed else weeki)
    wi = weeki if weeki >= 0 else (8 if fixed else -1)
    ci = pick(["ticket channel", "channel"], 3)
    fi = pick(["ticket form", "form"], 2)
    bi = pick(["equinox or kipu", "equinox kipu", "team", "business", "brand", "product"], 4)
    ti = idx(["tickets", "volume", "count"])
    if min(di, ci, fi, bi) < 0:
        raise ValueError("Could not map the sheet columns. Received headers: " + " | ".join(hdr))
    agg = {}
    for r in rows[header_row + 1:]:
        get = lambda i: r[i].strip() if 0 <= i < len(r) else ""
        dt = parse_date(get(di)) or (parse_date(get(wi)) if wi >= 0 else None)
        if not dt: continue
        d = dt.date(); monday = d - timedelta(days=d.weekday())
        form = get(fi) or "Unspecified form"
        rb = get(bi).lower()
        business = "Kipu" if "kipu" in rb else "Equinox" if "equinox" in rb else "Unspecified"
        x = agg.setdefault((date_key(d), form, business), {
            "date": date_key(d), "week": date_key(monday), "form": form, "business": business,
            "Email": 0, "Messaging": 0, "Voice": 0, "Unspecified": 0,
            "EmailOff": 0, "MessagingOff": 0, "VoiceOff": 0, "total": 0})
        raw = get(ci).lower()
        ch = ("Voice" if "voice" in raw else "Messaging" if ("messag" in raw or "chat" in raw)
              else "Email" if any(k in raw for k in ("email", "api", "web", "mobile")) else "Unspecified")
        try: n = float(get(ti)) if ti >= 0 and get(ti) else 1
        except ValueError: n = 1
        x[ch] += n
        if ch != "Unspecified" and (dt.weekday() >= 5 or dt.hour < 8):   # fin de semana o antes de 8:00
            x[ch + "Off"] += n
        x["total"] += n
    parsed = sorted(agg.values(), key=lambda x: (x["date"], x["form"], x["business"]))
    if not parsed:
        raise ValueError("No valid dated ticket rows found. Check Created Time (column B) or Week (column I).")
    return parsed

# ---------- 10) Explicación paso a paso de un método ----------
def _r(x, d=2):
    return round(x, d)

def explain(v, method, h=12, period=52, params=None):
    pr = _params(params)
    a = list(v) if v else [0.0]
    n = len(a)
    out = {"method": method, "title": METHODS.get(method, method), "formula": "",
           "steps": [], "columns": [], "rows": [], "result": None, "params": pr}

    if method == "average":
        s = sum(a)
        out["formula"] = "Forecast = (x1 + x2 + … + xn) / n   (flat, no growth)"
        out["steps"] = [f"n = {n} periods", f"Sum of actuals = {_r(s)}",
                        f"Forecast = {_r(s)} / {n} = {_r(s / n)}"]
        out["columns"] = ["t", "Actual"]
        out["rows"] = [[i + 1, _r(x)] for i, x in enumerate(a)]
        out["result"] = s / n

    elif method == "moving":
        w = a[-4:]; s = sum(w)
        out["formula"] = "Forecast = (x(n-3) + x(n-2) + x(n-1) + x(n)) / 4   (Excel window = 4, flat)"
        out["steps"] = [f"Last {len(w)} actuals: {', '.join(str(_r(x)) for x in w)}",
                        f"Sum = {_r(s)}", f"Forecast = {_r(s)} / {len(w)} = {_r(s / len(w))}"]
        out["columns"] = ["t", "Actual", "In window"]
        out["rows"] = [[i + 1, _r(x), "yes" if i >= n - 4 else ""] for i, x in enumerate(a)]
        out["result"] = s / len(w)

    elif method == "weighted":
        w = a[-8:]; wts = [8, 7, 6, 5, 4, 3, 2, 1][-len(w):]
        prods = [x * k for x, k in zip(w, wts)]
        out["formula"] = "Forecast = Σ(x_t × w_t) / Σ(w_t)   with w = 8,7,6,5,4,3,2,1 (most recent = 8)"
        out["steps"] = [f"Σ(x × w) = {_r(sum(prods))}", f"Σ(w) = {sum(wts)}",
                        f"Forecast = {_r(sum(prods))} / {sum(wts)} = {_r(sum(prods) / sum(wts))}"]
        out["columns"] = ["t", "Actual", "Weight", "Actual × Weight"]
        off = n - len(w)
        out["rows"] = [[off + i + 1, _r(x), k, _r(x * k)] for i, (x, k) in enumerate(zip(w, wts))]
        out["result"] = sum(prods) / sum(wts)

    elif method == "exponential":
        al = pr["alpha"]; level = a[0]
        rows = [[1, _r(a[0]), "seed = first actual", _r(level)]]
        for t, x in enumerate(a[1:], start=2):
            prev = level; level = al * x + (1 - al) * prev
            rows.append([t, _r(x), f"{al}×{_r(x)} + {_r(1 - al)}×{_r(prev)}", _r(level)])
        out["formula"] = f"Level_t = α × Actual_t + (1 − α) × Level_(t−1)   with α = {al}  ·  Forecast = last Level (flat)"
        out["steps"] = [f"Seed Level_1 = {_r(a[0])}", f"Recursion over {n - 1} periods", f"Forecast = {_r(level)}"]
        out["columns"] = ["t", "Actual", "Calculation", "Level"]
        out["rows"] = rows; out["result"] = level

    elif method == "double":
        da, db = pr["dAlpha"], pr["dBeta"]
        hl, ht = a[0], (a[1] - a[0] if n > 1 else 0.0)
        rows = [[1, _r(a[0]), _r(hl), _r(ht)]]
        for t, x in enumerate(a[1:], start=2):
            p = hl
            hl = da * x + (1 - da) * (hl + ht)
            ht = db * (hl - p) + (1 - db) * ht
            rows.append([t, _r(x), _r(hl), _r(ht)])
        out["formula"] = (f"Level_t = α×Actual_t + (1−α)×(Level_(t−1) + Trend_(t−1))  ·  "
                          f"Trend_t = β×(Level_t − Level_(t−1)) + (1−β)×Trend_(t−1)  ·  "
                          f"Forecast_(n+k) = Level_n + k×Trend_n   (α = {_r(da, 4)}, β = {_r(db, 4)})")
        out["steps"] = [f"Seed Level = {_r(a[0])}, seed Trend = {_r(rows[0][3])}",
                        f"Final Level = {_r(hl)}, final Trend = {_r(ht)}",
                        "Forecast k periods ahead = " + " · ".join(f"k={k}: {_r(max(0.0, hl + ht * k))}" for k in range(1, min(h, 6) + 1))]
        out["columns"] = ["t", "Actual", "Level", "Trend"]
        out["rows"] = rows; out["result"] = max(0.0, hl + ht)

    elif method == "cma":
        ma = [sum(a[i:i + 4]) / 4 for i in range(n - 3)] if n >= 4 else []
        cma = [(ma[i] + ma[i + 1]) / 2 for i in range(len(ma) - 1)]
        rows = []
        for t in range(n):
            ma_v = _r(ma[t]) if t < len(ma) else ""          # MA starting at t
            cma_v = _r(cma[t - 2]) if 0 <= t - 2 < len(cma) else ""  # CMA centred on t
            rows.append([t + 1, _r(a[t]), ma_v, cma_v])
        out["formula"] = "MA4_i = mean(x_i … x_(i+3))  ·  CMA_k = (MA4_k + MA4_(k+1)) / 2, centred on x_(k+2)  ·  Forecast = last CMA (flat)"
        out["steps"] = [f"{len(ma)} trailing MA(4) values", f"{len(cma)} centred values",
                        f"Forecast = last CMA = {_r(cma[-1]) if cma else 'n/a (need ≥ 5 periods)'}"]
        out["columns"] = ["t", "Actual", "MA(4) starting at t", "CMA centred on t"]
        out["rows"] = rows; out["result"] = cma[-1] if cma else sum(a) / n

    elif method == "trend":
        slope, intercept = linear(a)
        cma = centered_moving_average(a, 4)
        si = seasonal_indices(a, cma, 4, period, 0)
        rows = []
        for i in range(h):
            t = n + i; tv = intercept + slope * t; pos = t % period; s = si[pos] if si else 1.0
            rows.append([f"n+{i + 1}", t, _r(tv), pos, _r(s, 4), _r(max(0.0, tv * s))])
        used = sorted({(t % period) for t in range(n, n + h)})
        out["formula"] = "Trend_t = intercept + slope × t  ·  SI_pos = normalised mean of Actual/CMA per seasonal position  ·  Forecast = Trend_t × SI_pos"
        out["steps"] = [f"Linear regression on t = 0…{n - 1}: slope = {_r(slope, 4)}, intercept = {_r(intercept, 2)}",
                        f"CMA(4) gives {len(cma)} centred values → {sum(1 for k in range(period) if si and si[k] != 1.0)} seasonal positions with data (period = {period})",
                        "Seasonal indices used: " + ", ".join(f"pos {p}: {_r(si[p], 3)}" for p in used[:12]) + (" …" if len(used) > 12 else ""),
                        "Positions without history use SI = 1.0 (pure trend)"]
        out["columns"] = ["Horizon", "t", "Trend", "Seasonal pos", "SI", "Forecast"]
        out["rows"] = rows; out["result"] = rows[0][5] if rows else None

    return out


# ---------- 11) Explorador de datos reales por grano ----------
def table_by_grain(rows, form="All forms", business="Overall", grain="monthly"):
    m = {}
    for r in rows:
        if form != "All forms" and r["form"] != form: continue
        if business != "Overall" and r.get("business") != business: continue
        src = r["week"] if grain == "weekly" else (r.get("date") or r["week"])
        d = _to_date(src)
        p = d.replace(day=1) if grain == "monthly" else d
        x = m.setdefault(date_key(p), {"date": date_key(p), "label": period_label(p, grain),
                                       "Voice": 0, "Messaging": 0, "Email": 0, "Unspecified": 0, "total": 0, "off": 0})
        for k in ("Voice", "Messaging", "Email", "Unspecified", "total"):
            x[k] += r.get(k, 0)
        x["off"] += r.get("VoiceOff", 0) + r.get("MessagingOff", 0) + r.get("EmailOff", 0)
    return sorted(m.values(), key=lambda x: x["date"])

# ---------- 9) Datos de muestra (del proyecto original) ----------
SAMPLE = [
    {"week": "2026-06-29", "form": "EMR Default Form", "Email": 16,  "Messaging": 16,  "Voice": 24,  "Unspecified": 0,  "EmailOff": 16, "MessagingOff": 16, "VoiceOff": 24, "total": 56},
    {"week": "2026-07-06", "form": "EMR Default Form", "Email": 425, "Messaging": 534, "Voice": 606, "Unspecified": 0,  "EmailOff": 41, "MessagingOff": 55, "VoiceOff": 49, "total": 1565},
    {"week": "2026-07-13", "form": "EMR Default Form", "Email": 446, "Messaging": 526, "Voice": 654, "Unspecified": 0,  "EmailOff": 44, "MessagingOff": 44, "VoiceOff": 38, "total": 1626},
    {"week": "2026-07-20", "form": "EMR Default Form", "Email": 390, "Messaging": 414, "Voice": 589, "Unspecified": 0,  "EmailOff": 51, "MessagingOff": 34, "VoiceOff": 53, "total": 1393},
    {"week": "2026-07-27", "form": "EMR Default Form", "Email": 388, "Messaging": 476, "Voice": 595, "Unspecified": 2,  "EmailOff": 34, "MessagingOff": 49, "VoiceOff": 63, "total": 1461},
    {"week": "2026-08-03", "form": "EMR Default Form", "Email": 450, "Messaging": 455, "Voice": 649, "Unspecified": 16, "EmailOff": 53, "MessagingOff": 39, "VoiceOff": 57, "total": 1570},
    {"week": "2026-08-10", "form": "EMR Default Form", "Email": 354, "Messaging": 359, "Voice": 430, "Unspecified": 0,  "EmailOff": 7,  "MessagingOff": 13, "VoiceOff": 5,  "total": 1143},
]


# ---------- 10) Explicación paso a paso de un método ----------
def _r(x, d=2):
    return round(x, d)


def explain(v, method, h=12, period=52, params=None):
    pr = _params(params)
    a = list(v) if v else [0.0]
    n = len(a)
    out = {"method": method, "title": METHODS.get(method, method), "formula": "",
           "steps": [], "columns": [], "rows": [], "result": None, "params": pr}

    if method == "average":
        s = sum(a)
        out["formula"] = "Forecast = (x1 + x2 + ... + xn) / n   (flat, no growth)"
        out["steps"] = [f"n = {n} periods", f"Sum of actuals = {_r(s)}",
                        f"Forecast = {_r(s)} / {n} = {_r(s / n)}"]
        out["columns"] = ["t", "Actual"]
        out["rows"] = [[i + 1, _r(x)] for i, x in enumerate(a)]
        out["result"] = s / n

    elif method == "moving":
        w = a[-4:]
        s = sum(w)
        out["formula"] = "Forecast = (x(n-3) + x(n-2) + x(n-1) + x(n)) / 4   (Excel window = 4, flat)"
        out["steps"] = [f"Last {len(w)} actuals: {', '.join(str(_r(x)) for x in w)}",
                        f"Sum = {_r(s)}", f"Forecast = {_r(s)} / {len(w)} = {_r(s / len(w))}"]
        out["columns"] = ["t", "Actual", "In window"]
        out["rows"] = [[i + 1, _r(x), "yes" if i >= n - 4 else ""] for i, x in enumerate(a)]
        out["result"] = s / len(w)

    elif method == "weighted":
        w = a[-8:]
        wts = [8, 7, 6, 5, 4, 3, 2, 1][-len(w):]
        prods = [x * k for x, k in zip(w, wts)]
        out["formula"] = "Forecast = SUM(x_t * w_t) / SUM(w_t)   with w = 8,7,6,5,4,3,2,1 (most recent = 8)"
        out["steps"] = [f"SUM(x * w) = {_r(sum(prods))}", f"SUM(w) = {sum(wts)}",
                        f"Forecast = {_r(sum(prods))} / {sum(wts)} = {_r(sum(prods) / sum(wts))}"]
        out["columns"] = ["t", "Actual", "Weight", "Actual x Weight"]
        off = n - len(w)
        out["rows"] = [[off + i + 1, _r(x), k, _r(x * k)] for i, (x, k) in enumerate(zip(w, wts))]
        out["result"] = sum(prods) / sum(wts)

    elif method == "exponential":
        al = pr["alpha"]
        level = a[0]
        rows = [[1, _r(a[0]), "seed = first actual", _r(level)]]
        for t, x in enumerate(a[1:], start=2):
            prev = level
            level = al * x + (1 - al) * prev
            rows.append([t, _r(x), f"{al} x {_r(x)} + {_r(1 - al)} x {_r(prev)}", _r(level)])
        out["formula"] = (f"Level_t = alpha * Actual_t + (1 - alpha) * Level_(t-1)   with alpha = {al}"
                          f"  |  Forecast = last Level (flat)")
        out["steps"] = [f"Seed Level_1 = {_r(a[0])}", f"Recursion over {n - 1} periods",
                        f"Forecast = {_r(level)}"]
        out["columns"] = ["t", "Actual", "Calculation", "Level"]
        out["rows"] = rows
        out["result"] = level

    elif method == "double":
        da, db = pr["dAlpha"], pr["dBeta"]
        hl = a[0]
        ht = a[1] - a[0] if n > 1 else 0.0
        rows = [[1, _r(a[0]), _r(hl), _r(ht)]]
        for t, x in enumerate(a[1:], start=2):
            p = hl
            hl = da * x + (1 - da) * (hl + ht)
            ht = db * (hl - p) + (1 - db) * ht
            rows.append([t, _r(x), _r(hl), _r(ht)])
        out["formula"] = (f"Level_t = alpha*Actual_t + (1-alpha)*(Level_(t-1) + Trend_(t-1))  |  "
                          f"Trend_t = beta*(Level_t - Level_(t-1)) + (1-beta)*Trend_(t-1)  |  "
                          f"Forecast_(n+k) = Level_n + k*Trend_n   (alpha = {_r(da, 4)}, beta = {_r(db, 4)})")
        preview = " | ".join(f"k={k}: {_r(max(0.0, hl + ht * k))}" for k in range(1, min(h, 6) + 1))
        out["steps"] = [f"Seed Level = {_r(a[0])}, seed Trend = {_r(rows[0][3])}",
                        f"Final Level = {_r(hl)}, final Trend = {_r(ht)}",
                        "Forecast k periods ahead = " + preview]
        out["columns"] = ["t", "Actual", "Level", "Trend"]
        out["rows"] = rows
        out["result"] = max(0.0, hl + ht)

    elif method == "cma":
        ma = [sum(a[i:i + 4]) / 4 for i in range(n - 3)] if n >= 4 else []
        cma = [(ma[i] + ma[i + 1]) / 2 for i in range(len(ma) - 1)]
        rows = []
        for t in range(n):
            ma_v = _r(ma[t]) if t < len(ma) else ""
            cma_v = _r(cma[t - 2]) if 0 <= t - 2 < len(cma) else ""
            rows.append([t + 1, _r(a[t]), ma_v, cma_v])
        last = _r(cma[-1]) if cma else "n/a (need >= 5 periods)"
        out["formula"] = ("MA4_i = mean(x_i ... x_(i+3))  |  CMA_k = (MA4_k + MA4_(k+1)) / 2, centred on x_(k+2)"
                          "  |  Forecast = last CMA (flat)")
        out["steps"] = [f"{len(ma)} trailing MA(4) values", f"{len(cma)} centred values",
                        f"Forecast = last CMA = {last}"]
        out["columns"] = ["t", "Actual", "MA(4) starting at t", "CMA centred on t"]
        out["rows"] = rows
        out["result"] = cma[-1] if cma else sum(a) / n

    elif method == "trend":
        slope, intercept = linear(a)
        cma = centered_moving_average(a, 4)
        si = seasonal_indices(a, cma, 4, period, 0)
        rows = []
        for i in range(h):
            t = n + i
            tv = intercept + slope * t
            pos = t % period
            s = si[pos] if si else 1.0
            rows.append([f"n+{i + 1}", t, _r(tv), pos, _r(s, 4), _r(max(0.0, tv * s))])
        used = sorted({(t % period) for t in range(n, n + h)})
        with_data = sum(1 for k in range(period) if si and si[k] != 1.0)
        si_text = ", ".join(f"pos {p}: {_r(si[p], 3)}" for p in used[:12]) + (" ..." if len(used) > 12 else "")
        out["formula"] = ("Trend_t = intercept + slope * t  |  SI_pos = normalised mean of Actual/CMA per seasonal position"
                          "  |  Forecast = Trend_t * SI_pos")
        out["steps"] = [f"Linear regression on t = 0..{n - 1}: slope = {_r(slope, 4)}, intercept = {_r(intercept, 2)}",
                        f"CMA(4) gives {len(cma)} centred values -> {with_data} seasonal positions with data (period = {period})",
                        "Seasonal indices used: " + si_text,
                        "Positions without history use SI = 1.0 (pure trend)"]
        out["columns"] = ["Horizon", "t", "Trend", "Seasonal pos", "SI", "Forecast"]
        out["rows"] = rows
        out["result"] = rows[0][5] if rows else None

    return out


# ---------- 11) Explorador de datos reales por grano ----------
def table_by_grain(rows, form="All forms", business="Overall", grain="monthly"):
    m = {}
    for r in rows:
        if form != "All forms" and r["form"] != form:
            continue
        if business != "Overall" and r.get("business") != business:
            continue
        src = r["week"] if grain == "weekly" else (r.get("date") or r["week"])
        d = datetime.strptime(src, "%Y-%m-%d").date()
        p = d.replace(day=1) if grain == "monthly" else d
        x = m.setdefault(date_key(p), {
            "date": date_key(p), "label": period_label(p, grain),
            "Voice": 0, "Messaging": 0, "Email": 0, "Unspecified": 0, "total": 0, "off": 0,
        })
        for k in ("Voice", "Messaging", "Email", "Unspecified", "total"):
            x[k] += r.get(k, 0)
        x["off"] += r.get("VoiceOff", 0) + r.get("MessagingOff", 0) + r.get("EmailOff", 0)
    return sorted(m.values(), key=lambda x: x["date"])