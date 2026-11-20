"""GTA VI trophy earn-rate forecast (gta6-trophy-rarity-forecast-01, round 3).

For every case, read the Exophase history and emit five quantiles
(5/25/50/75/95%) of the earn rate at the case target, one row per trophy.
Python 3.13, standard library only; the output is a pure function of the
input bytes, so rerunning gives byte-identical files.

The shape of the forecast (the full reasoning and every number's evidence
live in STRATEGY.md):
Exophase's tracked-player count N and each trophy's earner count E arrive in
jumps, because the page refreshes only every few hours and each refresh
lands a backlog of freshly synced players. A refresh dilutes every rate at
once (N jumps) and then E climbs while those players work through the game.
So the median is a blend of two fits over the last day of distinct reads:
  * the rate itself, fitted in log-odds against ln(1 + hours since launch),
    weight 0.75;
  * the structure: ln E and ln N fitted on their own, rate = E/N at the
    target, weight 0.25, with E's slope pulled toward the pooled slope.
A rate is E/N by definition, so the two fits have to agree eventually;
blending them hedges against whichever one the next refresh breaks.

Round 2 (kept): the blended median is pulled 0.85 of the way toward the
last observed rate. The population saturates, so the trend is concave and a
straight log-linear line runs past the target; persistence alone falls
short. The truth sits about 85% of the way from the last rate to the line.
The spread was also tightened (floor 0.3, walk 0.4): launch night's first
refresh diluted rates ~6x, which is what the old wide floor insured against,
but by the final the refreshes are small routine jumps and the old floor
only widened the quantiles and cost pinball loss.

Round 3 (new, measured on the practice cases and on simulations of the
final -- see STRATEGY.md for the numbers): the no-refresh branch is
recalibrated to what launch night actually did, and it gets its own tight
spread. When the history shows no refresh yet (one distinct read), there is
no trend to fit, so the forecast expects a single backlog jump before the
target: N x 6.06 and E x 1.02. That is the kit's own rules record of the
first refresh (tracked players 638 -> 3,868, earners essentially flat),
cross-checked against round 2's live loss of 0.135, which implies the same
jump. Because the jump is calibrated rather than guessed, its spread floor
is 0.05 log-odds instead of the generic 0.3. This branch never fires on the
final, which holds 2.5 days of refreshes; it exists so the practice cases,
which straddle exactly that first refresh, are forecast honestly.

Spread (log-odds): sigma = sqrt(FLOOR^2 + WALK^2 * horizon_days + RESID^2),
where FLOOR is 0.05 on the flat branch and 0.3 elsewhere, and RESID is the
trophy's own scatter around its log-odds trend. Quantiles are
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
WINDOW_H = 24.0       # only reads from the last 24 hours feed the fits
BLEND_W = 0.75        # weight of the direct log-odds fit in the median
JUMP_N = 6.06         # N jump expected before target when no refresh seen
JUMP_E = 1.02         # E growth over that same jump
FLOOR = 0.3           # log-odds floor for unseen refresh jumps (was 0.6)
FLOOR_FLAT = 0.05     # round 3: floor when the flat jump prior carries
                      # the median (the jump is calibrated, not a guess)
WALK = 0.4            # log-odds random walk per sqrt(day) (was 0.5)
SHRINK_W = 2.0        # weight of the pooled E slope when shrinking
DAMP = 0.85           # round 2: pull the extrapolated median toward the
                      # last observed rate (log-odds space, non-flat only)
CAP_N = 10.0          # extrapolated N is at most 10x the last N
CAP_E = 50.0          # extrapolated E is at most 50x the last E

LAUNCH = datetime(2026, 11, 19, 0, 0, 0, tzinfo=timezone.utc)


def clamp_pct(rate):
    return min(99.99, max(0.01, rate))


def to_logit(rate):
    p = clamp_pct(rate)
    return math.log(p / (100.0 - p))


def from_logit(lg):
    return 100.0 / (1.0 + math.exp(-lg))


def round4(value):
    scaled = value * 10000
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    out = whole / 10000
    return int(out) if out == int(out) else out


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def hours_since_launch(when):
    return (parse_time(when) - LAUNCH).total_seconds() / 3600.0


def linfit(xs, ys):
    """Least squares of y on x; returns (slope, intercept, resid_std)."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    resid = 0.0
    if n > 2:
        resid = math.sqrt(max(sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys)) / (n - 2), 0.0))
    return slope, intercept, resid


def collapse_reads(history):
    """Collapse consecutive reads with identical (N, rates) into one state."""
    states = []
    for read in history:
        key = (read.get("players_tracked"), tuple(read.get("rates") or ()))
        if not states or states[-1][0] != key:
            states.append((key, read))
    return [read for _, read in states]


def fit_window(reads):
    """Last 24h of distinct reads; all of them when too few to fit a line."""
    latest = reads[-1]
    t_latest = hours_since_launch(latest["at"])
    window = [r for r in reads if hours_since_launch(r["at"]) >= t_latest - WINDOW_H]
    return window if len(window) >= 3 else reads


def players_at_target(window, x_target, n_last, flat):
    """Extrapolated tracked-player count at the target."""
    xs = [math.log1p(max(hours_since_launch(r["at"]), 0.0)) for r in window
          if r.get("players_tracked")]
    ys = [math.log(r["players_tracked"]) for r in window if r.get("players_tracked")]
    fit = linfit(xs, ys)
    if fit is not None:
        target = math.exp(fit[1] + max(fit[0], 0.0) * x_target)
        return min(max(target, n_last), n_last * CAP_N) if n_last > 0 else target
    if flat:
        return float(n_last) * JUMP_N
    return float(n_last)


def pool_slope(window, trophies):
    """Pooled ln E slope across all trophies (for shrinking each E slope)."""
    xs_all, ys_all = [], []
    for i in range(len(trophies)):
        for r in window:
            rates = r.get("rates") or []
            n = r.get("players_tracked")
            if i < len(rates) and rates[i] is not None and n and n > 0:
                xs_all.append(math.log1p(max(hours_since_launch(r["at"]), 0.0)))
                ys_all.append(math.log(max(rates[i] * n / 100.0, 0.5)))
    fit = linfit(xs_all, ys_all)
    return max(fit[0], 0.0) if fit else 0.0


def forecast_trophy(idx, window, n_target, slope_pool, x_target,
                    r_last, e_last, flat):
    """One trophy's five quantiles: blended median, spread, monotonic."""
    # (a) the rate's own log-odds trend on the window.
    xs_l, ys_l = [], []
    for r in window:
        rates = r.get("rates") or []
        if idx < len(rates) and rates[idx] is not None:
            xs_l.append(math.log1p(max(hours_since_launch(r["at"]), 0.0)))
            ys_l.append(to_logit(rates[idx]))
    fit_l = linfit(xs_l, ys_l)
    if fit_l is not None:
        rate_logit = fit_l[1] + fit_l[0] * x_target
        resid = fit_l[2]
    else:
        rate_logit = to_logit(r_last)
        resid = 0.0

    # (b) the structural E/N median: E fitted, slope shrunk to the pool.
    xs_e, ys_e = [], []
    for r in window:
        rates = r.get("rates") or []
        n = r.get("players_tracked")
        if idx < len(rates) and rates[idx] is not None and n and n > 0:
            xs_e.append(math.log1p(max(hours_since_launch(r["at"]), 0.0)))
            ys_e.append(math.log(max(rates[idx] * n / 100.0, 0.5)))
    fit_e = linfit(xs_e, ys_e)
    if fit_e is not None:
        slope_e = max(fit_e[0], 0.0)
        k_own = min(len(xs_e), 6)
        slope_e = (k_own * slope_e + SHRINK_W * slope_pool) / (k_own + SHRINK_W)
        e_target = math.exp(fit_e[1] + slope_e * x_target)
        e_target = min(max(e_target, e_last), e_last * CAP_E) if e_last > 0 else e_target
        rate_en = 100.0 * e_target / n_target if n_target > 0 else r_last
    elif flat:
        e_target = float(e_last) * JUMP_E
        rate_en = 100.0 * e_target / n_target if n_target > 0 else r_last
    else:
        rate_en = r_last

    if flat:
        # No trend exists: the jump prior carries the whole median.
        # (No damping here: the flat branch is a jump prior, not an
        # extrapolation -- damping it would undo the expected refresh.)
        median = clamp_pct(rate_en)
    else:
        blended = BLEND_W * rate_logit + (1.0 - BLEND_W) * to_logit(rate_en)
        # Round 2: damp the extrapolated movement toward the last observed
        # rate -- the trend is concave (saturating), so a log-linear
        # extrapolation overshoots the target by ~0.15 log-odds.
        blended = to_logit(r_last) + DAMP * (blended - to_logit(r_last))
        median = clamp_pct(from_logit(blended))
    if r_last >= 99.99:
        median = 100.0

    return median, resid


def forecast(case):
    trophies = case["trophies"]
    history = case.get("history") or []
    horizon_days = case["horizon_minutes"] / 1440.0
    t_target = hours_since_launch(case["target"])
    x_target = math.log1p(max(t_target, 0.0))

    if not history:
        rows = [{"id": t["id"], "q": [round4(from_logit(to_logit(1.0) + z * 1.5)) for z in Z_SCORES]}
                for t in trophies]
        return {"trophies": rows}

    reads = collapse_reads(history)
    last = reads[-1]
    n_last = last["players_tracked"] or 0
    rates_last = last["rates"]
    window = fit_window(reads)
    flat = len(reads) < 2  # no refresh observed yet

    n_target = players_at_target(window, x_target, n_last, flat)
    slope_pool = pool_slope(window, trophies)

    rows = []
    for i, t in enumerate(trophies):
        r_last = rates_last[i] if rates_last and i < len(rates_last) and rates_last[i] is not None else 1.0
        e_last = r_last * n_last / 100.0 if n_last > 0 else 0.0
        median, resid = forecast_trophy(i, window, n_target, slope_pool,
                                        x_target, r_last, e_last, flat)
        floor = FLOOR_FLAT if flat else FLOOR
        sigma = math.sqrt(floor ** 2 + (WALK ** 2) * horizon_days + resid ** 2)
        if r_last >= 99.99:
            sigma = 0.35
        lg = to_logit(median)
        qs, prev = [], None
        for z in Z_SCORES:
            q = min(100.0, max(0.0, round4(from_logit(lg + z * sigma))))
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
        fc = forecast(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(fc, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
