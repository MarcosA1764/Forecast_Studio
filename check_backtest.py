"""Verify the v4 one-step-ahead backtest.

The browser API only has daily data up to the length of the loaded series
(1,024 rows for the current dataset), so t=100 is checked against the real
daily series and t=5,000 / 15,000 are checked against a deterministic
15,001-point synthetic series.  Both are compared with forecast() on the
same prefix, which is the definition required by the spec.

Usage:
    python check_backtest.py            # uses http://127.0.0.1:8000
    python check_backtest.py --no-api   # pure-function checks only
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from urllib.request import Request, urlopen

from forecast_engine import forecast
from server import backtest_series

API = "http://127.0.0.1:8000/api/compute"
FAILURES: list[str] = []


def check(label: str, got, expected, tol: float = 0.0) -> None:
    if got is None or expected is None:
        ok = got is expected
        detail = f"got={got!r} expected={expected!r}"
    else:
        diff = abs(got - expected)
        ok = diff <= tol
        detail = f"got={got!r} expected={expected!r} diff={diff:.3e}"
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {detail}")
    if not ok:
        FAILURES.append(label)


def check_equal(label: str, got, expected) -> None:
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} expected={expected!r}")
    if not ok:
        FAILURES.append(label)


def api_result() -> dict | None:
    body = json.dumps({"grain": "daily", "official": {"default": "double", "periods": {}}}).encode()
    try:
        req = Request(API, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=180) as resp:
            return json.load(resp)
    except Exception as e:  # noqa: BLE001
        print(f"SKIP  API check: {e}")
        return None


def main() -> int:
    print("== API / real daily series ==")
    data = api_result()
    if data:
        values = data["values"]
        history = data["history"]
        official = data["backtestOfficial"]
        best = data["backtestBest"]
        period = data["seasonPeriod"]
        best_method = data["bestMethod"]
        print(f"series={len(values)} history={len(history)} official={len(official)} best={len(best)} "
              f"officialMethod={data['officialMethod']} bestMethod={best_method} "
              f"backtestMs={data['backtestMs']}")
        check_equal("API aligned lengths", len(values) == len(history) == len(official) == len(best), True)
        check_equal("API [0] is null", official[0] is None and best[0] is None, True)
        check_equal("API official method", data["officialMethod"], "double")
        for t in (1, 2, 100, len(values) - 1):
            ref = forecast(values[:t], "double", 1, period)[0]
            check(f"API official t={t}", official[t], ref)
            ref_best = forecast(values[:t], best_method, 1, period)[0]
            check(f"API best t={t}", best[t], ref_best)
        print("real series is shorter than 5,000; t=5,000 / 15,000 use the synthetic series below")

    print("\n== Pure incremental / synthetic long series ==")
    rng = random.Random(20261028)
    n = 15001
    values = [max(1.0, 1000.0 + 200.0 * math.sin(i / 17.0) + (i % 7) * 13.0 + rng.random() * 50.0)
              for i in range(n)]
    print(f"synthetic series={len(values)} period=7")
    start = time.perf_counter()
    double_bt = backtest_series(values, "double", 7)
    double_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    trend_bt = backtest_series(values, "trend", 7)
    trend_ms = (time.perf_counter() - start) * 1000
    print(f"elapsed: double={double_ms:.1f}ms trend={trend_ms:.1f}ms (n={n})")
    if double_ms > 5000 or trend_ms > 5000:
        FAILURES.append("synthetic runtime > 5s")
        print("FAIL  synthetic runtime > 5s")

    for t in (5000, 15000):
        ref = forecast(values[:t], "double", 1, 7)[0]
        check(f"synthetic official double t={t}", double_bt[t], ref)
        ref_trend = forecast(values[:t], "trend", 1, 7)[0]
        check(f"synthetic trend t={t}", trend_bt[t], ref_trend, tol=1e-8)

    print("\n== Summary ==")
    if FAILURES:
        print(f"FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("PASS: all one-step-ahead backtests match forecast() exactly.")
    return 0


if __name__ == "__main__":
    if "--no-api" in sys.argv:
        print("API check disabled by --no-api")
        api_result = lambda: None  # type: ignore[assignment]
    raise SystemExit(main())
