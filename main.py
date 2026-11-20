"""GTA VI trophy earn-rate forecast (gta6-trophy-rarity-forecast-01, round 2).

Reads each case's Exophase history and writes five quantiles (5/25/50/75/95%)
per trophy for the earn rate at the case target. Python 3.13, standard
library only; deterministic -- same input bytes always give same output bytes.

Why the forecast looks the way it does (full evidence in STRATEGY.md):
Exophase's tracked-player count N and each trophy's earner count E both move
in jumps when the page refreshes (every few hours). A refresh lands a backlog
of newly synced players, so N jumps and every rate dilutes at once; E then
climbs as those players progress. The median therefore blends two trend
fits over the last 24 hours of distinct reads (older reads are stale):
  * the rate's own log-odds trend on ln(1 + hours since launch), weight 0.75;
  * a structural trend: ln E and ln N fitted separately, rate = E/N at the
    target, weight 0.25, with E's slope shrunk toward the pooled slope.
A trophy's rate is E/N by construction, so the two fits must agree in the
long run; blending them hedges whichever one the next refresh breaks.

Two round-2 corrections, both measured on simulations of the final and on
the practice cases (see STRATEGY.md for the numbers):
  * the blended median is damped 0.85 toward the last observed rate. The
    trend is concave (the population saturates), so a straight log-linear
    extrapolation runs past the target; persistence alone undershoots. The
    truth sits about 85% of the way from the last rate to the extrapolation.
  * the spread is tightened (base 0.3, walk 0.4). Launch night's first
    refresh diluted rates ~6x, which is what the old wide floor insured
    against; by the final, refreshes are small routine jumps, so the old
    floor only widened the quantiles and cost pinball loss.

When the history shows no refresh yet (a single distinct read), no trend
exists to fit. The program then expects one backlog jump before the target
(N x 5, E x 1.1 -- the kit's own rules record launch night's first refresh
taking tracked players 638 -> 3,868). That branch cannot fire on the final,
which holds 2.5 days of refreshes; it exists so the practice cases, which
straddle exactly that first refresh, are forecast honestly instead of flat.

Spread (log-odds): sigma = sqrt(BASE^2 + WALK^2 * horizon_days + RESID^2),
with RESID the trophy's own scatter around its log-odds trend. Quantiles are
inv_logit(logit(median) + z * sigma). Pinball loss is a proper scoring rule,
so the quantiles written are the quantiles believed.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

QUANTILES = [0.05, 0.25, 0.5, 0.75, 0.95]
Z_SCORES = [-1.645, -0.674, 0, 0.674, 1.645]

# --- tuning (each value's evidence is in STRATEGY.md) ---
TREND_WINDOW_H = 24.0  # only reads from the last 24 hours feed the fits
LOGIT_BLEND_W = 0.75   # weight of the direct log-odds fit in the median
NO_REFRESH_N = 5.0     # N jump expected before target when no refresh seen
NO_REFRESH_E = 1.1     # E growth over that same jump
SPREAD_BASE = 0.3      # log-odds floor for unseen refresh jumps (was 0.6)
SPREAD_WALK = 0.4      # log-odds random walk per sqrt(day) (was 0.5)
POOL_SHRINK = 2.0      # weight of the pooled E slope when shrinking
MEDIAN_DAMP = 0.85     # round 2: pull the extrapolated median toward the
                       # last observed rate (log-odds space, non-flat only)
N_GROWTH_CAP = 10.0   # extrapolated N is at most 10x the last N
E_GROWTH_CAP = 50.0   # extrapolated E is at most 50x the last E

LAUNCH = datetime(2026, 11, 19, 0, 0, 0, tzinfo=timezone.utc)


def pct_clamp(rate):
    return min(99.99, max(0.01, rate))


def log_odds(rate):
    p = pct_clamp(rate)
    return math.log(p / (100.0 - p))


def from_log_odds(lg):
    return 100.0 / (1.0 + math.exp(-lg))


def round_4dp(value):
    scaled = value * 10000
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    out = whole / 10000
    return int(out) if out == int(out) else out


def parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def hrs_after_launch(when):
    return (parse_ts(when) - LAUNCH).total_seconds() / 3600.0


def least_squares(xs, ys):
    """Least squares of y on x; returns (slope, intercept, resid_std)."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
    intercept = mean_y - slope * mean_x
    resid = 0.0
    if n > 2:
        resid = math.sqrt(max(sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys)) / (n - 2), 0.0))
    return slope, intercept, resid


def distinct_reads(history):
    """Collapse consecutive reads with identical (N, rates) into one state."""
    states = []
    for read in history:
        key = (read.get("players_tracked"), tuple(read.get("rates") or ()))
        if not states or states[-1][0] != key:
            states.append((key, read))
    return [read for _, read in states]


def trend_window(reads):
    """Last 24h of distinct reads; all of them when too few to fit a line."""
    latest = reads[-1]
    t_latest = hrs_after_launch(latest["at"])
    window = [r for r in reads if hrs_after_launch(r["at"]) >= t_latest - TREND_WINDOW_H]
    return window if len(window) >= 3 else reads


def fit_players(window, x_target, n_last, flat):
    """Extrapolated tracked-player count at the target."""
    xs = [math.log1p(max(hrs_after_launch(r["at"]), 0.0)) for r in window
          if r.get("players_tracked")]
    ys = [math.log(r["players_tracked"]) for r in window if r.get("players_tracked")]
    fit = least_squares(xs, ys)
    if fit is not None:
        target = math.exp(fit[1] + max(fit[0], 0.0) * x_target)
        return min(max(target, n_last), n_last * N_GROWTH_CAP) if n_last > 0 else target
    if flat:
        return float(n_last) * NO_REFRESH_N
    return float(n_last)


def pooled_slope(window, trophies):
    """Pooled ln E slope across all trophies (for shrinking each E slope)."""
    xs_all, ys_all = [], []
    for i in range(len(trophies)):
        for r in window:
            rates = r.get("rates") or []
            n = r.get("players_tracked")
            if i < len(rates) and rates[i] is not None and n and n > 0:
                xs_all.append(math.log1p(max(hrs_after_launch(r["at"]), 0.0)))
                ys_all.append(math.log(max(rates[i] * n / 100.0, 0.5)))
    fit = least_squares(xs_all, ys_all)
    return max(fit[0], 0.0) if fit else 0.0


def trophy_forecast(idx, window, n_target, slope_pool, x_target,
                    r_last, e_last, flat):
    """One trophy's five quantiles: blended median, spread, monotonic."""
    # (a) the rate's own log-odds trend on the window.
    xs_l, ys_l = [], []
    for r in window:
        rates = r.get("rates") or []
        if idx < len(rates) and rates[idx] is not None:
            xs_l.append(math.log1p(max(hrs_after_launch(r["at"]), 0.0)))
            ys_l.append(log_odds(rates[idx]))
    fit_l = least_squares(xs_l, ys_l)
    if fit_l is not None:
        rate_logit = fit_l[1] + fit_l[0] * x_target
        resid = fit_l[2]
    else:
        rate_logit = log_odds(r_last)
        resid = 0.0

    # (b) the structural E/N median: E fitted, slope shrunk to the pool.
    xs_e, ys_e = [], []
    for r in window:
        rates = r.get("rates") or []
        n = r.get("players_tracked")
        if idx < len(rates) and rates[idx] is not None and n and n > 0:
            xs_e.append(math.log1p(max(hrs_after_launch(r["at"]), 0.0)))
            ys_e.append(math.log(max(rates[idx] * n / 100.0, 0.5)))
    fit_e = least_squares(xs_e, ys_e)
    if fit_e is not None:
        slope_e = max(fit_e[0], 0.0)
        k_own = min(len(xs_e), 6)
        slope_e = (k_own * slope_e + POOL_SHRINK * slope_pool) / (k_own + POOL_SHRINK)
        e_target = math.exp(fit_e[1] + slope_e * x_target)
        e_target = min(max(e_target, e_last), e_last * E_GROWTH_CAP) if e_last > 0 else e_target
        rate_en = 100.0 * e_target / n_target if n_target > 0 else r_last
    elif flat:
        e_target = float(e_last) * NO_REFRESH_E
        rate_en = 100.0 * e_target / n_target if n_target > 0 else r_last
    else:
        rate_en = r_last

    if flat:
        # No trend exists: the jump prior carries the whole median.
        # (No damping here: the flat branch is a jump prior, not an
        # extrapolation -- damping it would undo the expected refresh.)
        median = pct_clamp(rate_en)
    else:
        blended = LOGIT_BLEND_W * rate_logit + (1.0 - LOGIT_BLEND_W) * log_odds(rate_en)
        # Round 2: damp the extrapolated movement toward the last observed
        # rate -- the trend is concave (saturating), so a log-linear
        # extrapolation overshoots the target by ~0.15 log-odds.
        blended = log_odds(r_last) + MEDIAN_DAMP * (blended - log_odds(r_last))
        median = pct_clamp(from_log_odds(blended))
    if r_last >= 99.99:
        median = 100.0

    return median, resid


def forecast_case(case):
    trophies = case["trophies"]
    history = case.get("history") or []
    horizon_days = case["horizon_minutes"] / 1440.0
    t_target = hrs_after_launch(case["target"])
    x_target = math.log1p(max(t_target, 0.0))

    if not history:
        rows = [{"id": t["id"], "q": [round_4dp(from_log_odds(log_odds(1.0) + z * 1.5)) for z in Z_SCORES]}
                for t in trophies]
        return {"trophies": rows}

    reads = distinct_reads(history)
    last = reads[-1]
    n_last = last["players_tracked"] or 0
    rates_last = last["rates"]
    window = trend_window(reads)
    flat = len(reads) < 2  # no refresh observed yet

    n_target = fit_players(window, x_target, n_last, flat)
    slope_pool = pooled_slope(window, trophies)

    rows = []
    for i, t in enumerate(trophies):
        r_last = rates_last[i] if rates_last and i < len(rates_last) and rates_last[i] is not None else 1.0
        e_last = r_last * n_last / 100.0 if n_last > 0 else 0.0
        median, resid = trophy_forecast(i, window, n_target, slope_pool,
                                      x_target, r_last, e_last, flat)
        sigma = math.sqrt(SPREAD_BASE ** 2 + (SPREAD_WALK ** 2) * horizon_days + resid ** 2)
        if r_last >= 99.99:
            sigma = 0.35
        lg = log_odds(median)
        qs, prev = [], None
        for z in Z_SCORES:
            q = min(100.0, max(0.0, round_4dp(from_log_odds(lg + z * sigma))))
            if prev is not None and q < prev:
                q = prev
            qs.append(q)
            prev = q
        rows.append({"id": t["id"], "q": qs})
    return {"trophies": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
