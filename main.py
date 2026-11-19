"""gta6-trophy-rarity-forecast-01 entry (Python 3.13, standard library only).

Forecasts every GTA VI trophy's Exophase earn rate at the case's target as five
quantiles (5/25/50/75/95%), scored by pinball loss on the logit scale.

Model (see STRATEGY.md for the full reasoning and the tuning evidence):
  A trophy's rate is r = E / N: E = tracked players who earned it, N = tracked
  players. Exophase refreshes its counts every few hours, so both move in
  jumps: N jumps when a backlog of newly synced players lands (diluting every
  rate, since newcomers have earned ~nothing yet), while E grows as players
  progress. The median blends two trend fits, both on the last 24 hours of
  reads (recent dynamics only; launch-night levels are stale):
    (a) a direct fit of logit(rate) on ln(1 + hours since release), weight .75;
    (b) a structural fit of ln E and ln N separately on the same clock, then
        rate = E(target) / N(target), weight .25 (E's slope shrunk to pooled).
  Both slopes come from least squares over the distinct reads (consecutive
  identical reads are one state); counts' slopes are clamped >= 0.

  Flat-history prior: when the history shows no refresh yet (one distinct
  read, as on launch night), no trend can be estimated -- but a refresh is
  still coming, and the first one releases a backlog: the kit's own rules
  record launch night's first refresh taking tracked players 638 -> 3,868
  and diluting almost every rate ~6x, and state the practice rewards a
  forecast that expects a refresh. So with no refresh observed the program
  expects one backlog jump before the target (N x 5, E x 1.1) instead of
  pretending the flat page will stay flat. This branch cannot fire on the
  final (2.5 days of history hold many refreshes); it exists so the practice
  cases, which straddle exactly that first refresh, are forecast honestly.

  Spread (logit): sigma = sqrt(S_BASE^2 + S_RW^2 * horizon_days + RESID^2),
  where S_BASE floors unseen refresh jumps, S_RW is a random-walk rate, and
  RESID is the trophy's own residual scatter around its logit trend. The
  quantiles are inv_logit(logit(median) + z_tau * sigma); pinball loss is a
  proper scoring rule, so the quantiles written are the quantiles believed.

Deterministic: no randomness, no wall clock, no network; the same input bytes
always produce the same output bytes. Runs in milliseconds (55 trophies).
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

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0, 0.674, 1.645]

# --- tuned constants (see STRATEGY.md for how each was set) ---
WINDOW_HOURS = 24.0   # trend fits use only reads from the last 24 hours
W_LOGIT = 0.75        # median blend weight of the direct logit fit
JUMP_PRIOR = 5.0      # N growth expected before target when no refresh seen
JUMP_E_GROWTH = 1.1   # E growth over the same jump (newcomers earn a little)
S_BASE = 0.6          # logit floor: unseen refresh jumps
S_RW = 0.5            # logit random-walk rate per sqrt(day)
POOL_WEIGHT = 2.0     # shrinkage weight of the pooled E slope
MAX_N_GROWTH = 10.0   # cap on extrapolated N / N_last
MAX_E_GROWTH = 50.0   # cap on extrapolated E / E_last

RELEASE = datetime(2026, 11, 19, 0, 0, 0, tzinfo=timezone.utc)


def clamp_pct(x):
    return min(99.99, max(0.01, x))


def logit(pct):
    p = clamp_pct(pct)
    return math.log(p / (100.0 - p))


def inv_logit(lg):
    return 100.0 / (1.0 + math.exp(-lg))


def round4(x):
    scaled = x * 10000
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 10000
    return int(value) if value == int(value) else value


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_since_release(when):
    return (parse_time(when) - RELEASE).total_seconds() / 3600.0


def ols(xs, ys):
    """Least squares of y on x -> (slope, intercept, resid_std) or None."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    inter = my - slope * mx
    resid = 0.0
    if n > 2:
        resid = math.sqrt(max(sum((y - (inter + slope * x)) ** 2 for x, y in zip(xs, ys)) / (n - 2), 0.0))
    return slope, inter, resid


def forecast_case(case):
    trophies = case["trophies"]
    history = case.get("history") or []
    horizon_days = case["horizon_minutes"] / 1440.0
    t_target = hours_since_release(case["target"])
    x_target = math.log1p(max(t_target, 0.0))

    if not history:
        rows = [{"id": t["id"], "q": [round4(inv_logit(logit(1.0) + z * 1.5)) for z in Z]}
                for t in trophies]
        return {"trophies": rows}

    # Distinct reads: consecutive reads with identical (N, rates) are one state.
    states = []
    for h in history:
        key = (h.get("players_tracked"), tuple(h.get("rates") or ()))
        if not states or states[-1][0] != key:
            states.append((key, h))
    reads = [h for _, h in states]
    last = reads[-1]
    n_last = last["players_tracked"] or 0
    rates_last = last["rates"]

    # Trend window: last 24h of distinct reads; fall back to all of them when
    # the window holds too few distinct states to fit a line.
    t_last = hours_since_release(last["at"])
    window = [h for h in reads if hours_since_release(h["at"]) >= t_last - WINDOW_HOURS]
    if len(window) < 3:
        window = reads

    flat = len(reads) < 2  # no refresh observed yet

    # --- (b) structural E / N fit on the window ---
    xs_n = [math.log1p(max(hours_since_release(h["at"]), 0.0)) for h in window
            if h.get("players_tracked")]
    ys_n = [math.log(h["players_tracked"]) for h in window if h.get("players_tracked")]
    fit_n = ols(xs_n, ys_n)
    if fit_n is not None:
        n_target = math.exp(fit_n[1] + max(fit_n[0], 0.0) * x_target)
        n_target = min(max(n_target, n_last), n_last * MAX_N_GROWTH) if n_last > 0 else n_target
    elif flat:
        n_target = float(n_last) * JUMP_PRIOR
    else:
        n_target = float(n_last)

    pool_xs, pool_ys, per_trophy = [], [], []
    for i in range(len(trophies)):
        xs, ys = [], []
        for h in window:
            rates = h.get("rates") or []
            n = h.get("players_tracked")
            if i < len(rates) and rates[i] is not None and n and n > 0:
                xs.append(math.log1p(max(hours_since_release(h["at"]), 0.0)))
                ys.append(math.log(max(rates[i] * n / 100.0, 0.5)))
        per_trophy.append((xs, ys))
        pool_xs.extend(xs)
        pool_ys.extend(ys)
    fit_pool = ols(pool_xs, pool_ys)
    slope_pool = max(fit_pool[0], 0.0) if fit_pool else 0.0

    rows = []
    for i, t in enumerate(trophies):
        r_last = rates_last[i] if rates_last and i < len(rates_last) and rates_last[i] is not None else 1.0
        e_last = r_last * n_last / 100.0 if n_last > 0 else 0.0

        # (a) direct logit-rate trend on the window.
        xs_l, ys_l = [], []
        for h in window:
            rates = h.get("rates") or []
            if i < len(rates) and rates[i] is not None:
                xs_l.append(math.log1p(max(hours_since_release(h["at"]), 0.0)))
                ys_l.append(logit(rates[i]))
        fit_l = ols(xs_l, ys_l)
        if fit_l is not None:
            med_logit = fit_l[1] + fit_l[0] * x_target
            resid = fit_l[2]
        else:
            med_logit = logit(r_last)
            resid = 0.0

        # (b) structural E/N median.
        xs_e, ys_e = per_trophy[i]
        fit_e = ols(xs_e, ys_e)
        if fit_e is not None:
            slope_e = max(fit_e[0], 0.0)
            k_own = min(len(xs_e), 6)
            slope_e = (k_own * slope_e + POOL_WEIGHT * slope_pool) / (k_own + POOL_WEIGHT)
            e_target = math.exp(fit_e[1] + slope_e * x_target)
            e_target = min(max(e_target, e_last), e_last * MAX_E_GROWTH) if e_last > 0 else e_target
            med_en = 100.0 * e_target / n_target if n_target > 0 else r_last
        elif flat:
            e_target = float(e_last) * JUMP_E_GROWTH
            med_en = 100.0 * e_target / n_target if n_target > 0 else r_last
        else:
            med_en = r_last

        if flat:
            # No trend exists: the jump prior carries the whole median.
            median = clamp_pct(med_en)
        else:
            median = clamp_pct(inv_logit(W_LOGIT * med_logit + (1.0 - W_LOGIT) * logit(med_en)))
        if r_last >= 99.99:
            median = 100.0

        sigma = math.sqrt(S_BASE ** 2 + (S_RW ** 2) * horizon_days + resid ** 2)
        if r_last >= 99.99:
            sigma = 0.35
        lg = logit(median)
        qs, prev = [], None
        for z in Z:
            q = min(100.0, max(0.0, round4(inv_logit(lg + z * sigma))))
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
