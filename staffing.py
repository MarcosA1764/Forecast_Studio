import math

PAID_BASELINES = {
    "Overall": {"availableVoice": 23, "availableMessaging": 7, "availableEmail": 4},
    "Kipu":    {"availableVoice": 10, "availableMessaging": 0, "availableEmail": 0},
    "Equinox": {"availableVoice": 13, "availableMessaging": 7, "availableEmail": 4},
}
# El texto original indica: "Equinox retains 5 Voice and 1 Chat agents for weekend/midnight coverage"
DEFAULT_ASSUMPTIONS = {
    "aht": 20, "util": 70, "shrinkVoice": 15, "shrinkMessaging": 15, "shrinkEmail": 15,
    "hours": 8, "days": 22, "concurrency": 2, **PAID_BASELINES["Overall"],
    "offVoice": 5, "offMessaging": 1,
}

def _hist_sum(hist, key):
    """Suma una columna del histórico.

    Acepta el histórico pre-agregado (``{canal: total, canal+"Off": total}``), la lista de
    filas original (compatibilidad) o una lista con un único dict agregado.
    """
    if isinstance(hist, dict):
        return float(hist.get(key, 0) or 0)
    return sum((r.get(key, 0) or 0) for r in hist)

def plan_for(ch, volume, hist, a, period_days):
    shrink = a["shrinkVoice"] if ch == "Voice" else a["shrinkMessaging"] if ch == "Messaging" else a["shrinkEmail"]
    conc = a["concurrency"] if ch == "Messaging" else 1
    available = a["availableVoice"] if ch == "Voice" else a["availableMessaging"] if ch == "Messaging" else a["availableEmail"]
    gross = period_days * a["hours"] * 60
    productive = gross * (a["util"] / 100) * conc * (1 - shrink / 100)

    hist_total = _hist_sum(hist, ch)
    hist_off = _hist_sum(hist, ch + "Off")
    off_share = hist_off / hist_total if hist_total else 0.0
    off_vol = round(volume * off_share)
    reg_vol = volume - off_vol

    off_paid = a["offVoice"] if ch == "Voice" else a["offMessaging"] if ch == "Messaging" else 0
    reg_paid = max(available - off_paid, 0)
    req = lambda v: math.ceil(v * a["aht"] / productive) if productive else 0
    reg_calc, off_calc = req(reg_vol), req(off_vol)
    protect = ch in ("Voice", "Messaging")
    reg_req = max(reg_calc, reg_paid) if protect else reg_calc
    off_req = max(off_calc, off_paid) if protect else off_calc
    required = reg_req + off_req
    capacity = math.floor(available * productive / a["aht"]) if a["aht"] else 0
    return {
        "channel": ch, "volume": volume, "concurrency": conc, "shrinkage": shrink,
        "grossMinutes": gross, "productiveMinutes": round(productive), "available": available,
        "offShare": off_share * 100, "offVolume": off_vol, "regularVolume": reg_vol,
        "offPaid": off_paid, "regularPaid": reg_paid, "regularCalc": reg_calc, "offCalc": off_calc,
        "regularRequired": reg_req, "offRequired": off_req, "required": required,
        "capacity": capacity, "gap": available - required,
    }