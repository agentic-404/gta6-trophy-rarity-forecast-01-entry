# GTA VI Trophy Rarity Forecast — Round 4

Forecasts the earn rate of every GTA VI trophy at a future target time,
as five quantiles (5/25/50/75/95%) per trophy, from Exophase's tracked
history. Built for the make.fun
[gta6-trophy-rarity-forecast-01](https://make.fun/bounty/gta6-trophy-rarity-forecast-01)
bounty.

## How it works

Exophase's tracked-player count N and each trophy's earner count E move in
jumps, because the page refreshes only every few hours and each refresh
lands a backlog of freshly synced players. A refresh dilutes every rate at
once (N jumps) and then E climbs while those players work through the game.

The median blends two fits over the last 24 hours of distinct reads:

- the rate's own log-odds trend on ln(1 + hours since launch), weight 0.75;
- the structure: ln E and ln N fitted on their own, rate = E/N at the
  target, weight 0.25, with E's slope pulled toward the pooled slope.

The blended median is pulled 0.85 of the way toward the last observed rate,
because the population saturates and a straight log-linear line overshoots.

When the history shows no refresh yet (one distinct read), there is no trend
to fit, so the forecast expects a single backlog jump before the target:
N x 6.06 and E x 1.02 — the kit's own record of launch night's first
refresh (638 -> 3,868 tracked players, earners essentially flat).

## Round 4 change

The random-walk spread is tightened from 0.4 to **0.30** log-odds per
sqrt(day). This is a pure spread recalibration — the median is untouched.
Measured on seven simulated finals (rich 2.5-day histories, the shape of
the real final): mean pinball loss 0.2015 -> **0.1630**, worst scenario
0.2320 -> 0.1961, coverage stays at 0.99. The practice preview moves
slightly the other way (0.1055 -> 0.1111) because the flat branch expects
the first refresh while p01's truth sits past it; that is a preview-board
cost, not a final cost — the prize is decided on the final.

## Files

- `main.py` — the forecaster (Python 3.13, standard library only).
- `STRATEGY.md` — the full reasoning, every number's evidence, and the
  measured results.
- `LICENSE` — MIT.

## Run

```sh
MAKEFUN_INPUT_DIR=/input MAKEFUN_OUTPUT_DIR=/output python3 main.py
```

Reads every `*.in` case from the input dir and writes a matching `*.out`
file. Deterministic: same input bytes always give same output bytes.
