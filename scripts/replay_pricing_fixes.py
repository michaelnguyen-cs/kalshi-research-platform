#!/usr/bin/env python
"""
READ-ONLY replay of the historical fills in kbot_events.db through the pricing
model BEFORE and AFTER the 2026-08 fixes, one fix at a time and then together.

Answers exactly the three questions the fixes have to be judged on:

  (a) DISTRIBUTION.  min / median / max / sd / unique-count of fair_p on the
      historical fills, before, after each fix individually, and after all of
      them. The test is whether fair_p stops being a near-constant -- NOT
      whether it gets wider or narrower, which is not a virtue either way.

  (b) DECISION CHANGE.  On those same fills: how many would the corrected model
      have traded at all, and how many would it have taken the OPPOSITE side
      on. Prettier probabilities that produce identical decisions have changed
      nothing.

  (c) SANITY CASES.  A handful of concrete (spot, strike, time-to-expiry)
      triples with the corrected fair_p, checkable by hand.

It also reports (section 1) how often the vol floor actually BOUND, which is
the measurement defect 1 turns on.

THIS SCRIPT NEVER PLACES AN ORDER and never touches the order-placement path.
Without --fetch-strikes it makes no network call at all. It computes no P&L and
takes no P&L as input: historical paper P&L predates the orderbook fix and is
invalid, so nothing here may be judged by it.

    # on the VM
    python scripts/measure_basis_and_vol.py --rec data/rec_A_usmorning.sqlite \\
        --rec data/rec_B_overnight.sqlite --rec data/rec_C_highvol.sqlite \\
        --out-vol-csv data/measured_15m_vol.csv

    python scripts/replay_pricing_fixes.py \\
        --events kbot_events.db \\
        --rec data/rec_A_usmorning.sqlite --rec data/rec_B_overnight.sqlite \\
        --rec data/rec_C_highvol.sqlite \\
        --fetch-strikes \\
        --vol-csv data/measured_15m_vol.csv

WHAT HAS TO BE RECONSTRUCTED, AND HOW HONEST EACH PIECE IS
----------------------------------------------------------
spot S, sigma, ask, side, time    logged verbatim on the `signal` event. Exact.
strike K                          NOT logged. KXBTC15M's floor_strike is a
                                  dynamic per-window value that never appears
                                  in the ticker. Resolved from the recordings'
                                  kalshi_market table, else from the Kalshi
                                  read-only get_market endpoint (cached to
                                  --strike-cache). Fills whose strike cannot be
                                  resolved are DROPPED and counted, never
                                  guessed -- a guessed strike would silently
                                  manufacture the very result being tested.
sigma if the floor BOUND          UNKNOWABLE from the logs: max(EWMA, 0.35)
                                  destroys the EWMA it discarded. All that is
                                  recoverable is "the true EWMA was below
                                  0.35". This is handled explicitly rather than
                                  papered over -- see SUBSTITUTE SIGMA below --
                                  and section 4 additionally reports the
                                  NON-FLOORED subset on its own, where nothing
                                  needs substituting and the before/after is
                                  assumption-free.

SUBSTITUTE SIGMA
----------------
For a floored fill the replay needs some sigma below the floor. Three modes,
none of which involves choosing a value that makes any result look better:

  --vol-csv PATH    the empirical sub-floor distribution measured off the
                    rec_*.sqlite recordings by measure_basis_and_vol.py, with
                    the SAME estimator and window the live path uses. Reported
                    as a quantile sweep (q10/q25/q50/q75/q90) plus a Monte
                    Carlo band, so the answer comes with its own uncertainty
                    instead of a single confident-looking number.  PREFERRED.
  --sub-sigma X     one fixed value, for sensitivity checks.
  (neither)         floored fills are reported as unresolvable and excluded
                    from the fix-1 arms; only the non-floored subset is used.

CAVEAT, stated once and not forgotten: the recordings and the fills are from
different dates. The sub-floor distribution is therefore an estimate of the
regime, not a per-fill truth. That is why the quantile sweep exists.

THE CALIBRATOR IS PART OF THE ANSWER
------------------------------------
The champion calibrator in kbot_registry.db was FIT on fair_p produced by the
broken model -- a fair_p pinned near 0.5 -- so it learned a steep amplifying
map. Applying it unchanged to a corrected fair_p that is already spread out
double-counts the correction. Section 4 therefore reports decisions BOTH ways:
with the stored calibrator (what deploying the fix without refitting would do)
and with it cleared (the honest model-only comparison). If the two disagree,
the calibrator must be refit before the fix is trusted.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
import random
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scipy.stats import norm  # noqa: E402

from kbot.config import settings as _cfg  # noqa: E402
from kbot.learning.calibration import apply_calibrator  # noqa: E402
from kbot.strategy.btc_hourly import _parse_ticker, effective_t_years  # noqa: E402

_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_FLOOR_EPS = 1e-9


# ── the reconstructed fill ───────────────────────────────────────────────────

@dataclass
class Fill:
    ticker: str
    ts: float
    side: str                 # "yes" | "no" -- the side actually bought
    ask: float
    spot: float
    sigma_logged: float
    strike: float
    direction: str            # "above" | "below" | "range"
    t_years: float
    logged_fair_p: float      # side-specific, POST-calibration, as logged
    floored: bool             # sigma_logged == cfg.vol_floor exactly


@dataclass
class Variant:
    """One pricing configuration to replay the fills through."""
    name: str
    use_new_floor: bool       # substitute a sub-floor sigma where the floor bound
    avg_seconds: float        # settlement averaging window; 0 = point settlement
    basis_dollars: float      # subtract from spot before pricing; 0 = no correction


def raw_fair_p_yes(f: Fill, v: Variant, sub_sigma: float | None) -> float | None:
    """
    The model's YES-side probability for fill `f` under variant `v`.

    Deliberately recomputed FORWARD from (S, K, sigma, T) rather than inverted
    out of the logged fair_p: the logged value is post-calibration, and an
    isotonic map is not invertible on its flat segments. Forward is exact and
    is cross-checked against the logged value in section 3.
    """
    sigma = f.sigma_logged
    if v.use_new_floor and f.floored:
        if sub_sigma is None:
            return None
        sigma = sub_sigma
    spot = f.spot - v.basis_dollars
    t_eff = effective_t_years(f.t_years, v.avg_seconds)
    if t_eff <= 0 or sigma <= 0 or spot <= 0 or f.strike <= 0:
        return None
    d = math.log(spot / f.strike) / (sigma * math.sqrt(t_eff))
    p_above = float(norm.cdf(d))
    return p_above if f.direction == "above" else 1.0 - p_above


def side_p(p_yes: float, side: str) -> float:
    return p_yes if side == "yes" else 1.0 - p_yes


def would_trade(p_side_raw: float, ask: float, calibrator) -> tuple[bool, float, float]:
    """
    Replay _size_side's gates for ONE side. Returns (tradeable, calibrated_p, net_edge).

    Mirrors kbot.strategy.btc_hourly.BTCHourlyStrategy._size_side in order:
    calibrate -> min_side_win_prob -> tradeable band -> fee/slippage/min_net_edge.
    half_spread is NOT logged on the signal event, so it is taken as 0 here --
    which makes this gate slightly PERMISSIVE. It is applied identically to
    every variant, so the before/after DELTA is unaffected; only the absolute
    trade counts are marginally optimistic, equally so on both sides.
    """
    if not (0.0 < ask < 1.0):
        return False, float("nan"), float("nan")
    p = apply_calibrator(calibrator, p_side_raw)
    if p < _cfg.min_side_win_prob:
        return False, p, float("nan")
    if not (_cfg.tradeable_band_low <= ask <= _cfg.tradeable_band_high):
        return False, p, float("nan")
    fee = _cfg.fee_coefficient * ask * (1.0 - ask)
    net_edge = (p - ask) - fee - _cfg.assumed_slippage
    return net_edge >= _cfg.min_net_edge, p, net_edge


@dataclass
class Decision:
    """What the replayed model would have done with one fill under one variant."""
    p_yes: float | None            # None: could not be priced (no substitute sigma)
    side: str | None               # side actually taken, or None for no trade
    band_blocked: bool             # evaluate()'s near_atm_only gate rejected the market
    side_if_unbanded: str | None   # side the per-side gates alone would have chosen

    @property
    def unresolved(self) -> bool:
        return self.p_yes is None


def decide(f: Fill, v: Variant, sub_sigma: float | None, calibrator) -> Decision:
    """
    Replay evaluate()'s market gate and _size_side's per-side gates for one fill.

    `side_if_unbanded` exists because near_atm_only turns would-be FLIPS into
    no-trades: a corrected fair_p that moves far enough to make the other side
    attractive has by then also left the [near_atm_fair_p_min, max] band, so the
    market is skipped before either side is priced. Counting only `side` would
    report "0 opposite-side decisions" and hide the fact that the model's
    DIRECTION reversed. Both are reported.

    The opposite side's ask is taken as 1 - ask, since only the traded side's
    price is logged. Real Kalshi books quote yes_ask + no_ask > 1, so this makes
    the opposite side CHEAPER than it really was -- i.e. it biases the flip count
    UP. A flip count of zero under this assumption is therefore a strong result;
    a positive one is an upper bound.
    """
    p_yes = raw_fair_p_yes(f, v, sub_sigma)
    if p_yes is None:
        return Decision(None, None, False, None)

    best_side, best_edge = None, -math.inf
    for side, ask in (("yes", f.ask if f.side == "yes" else 1.0 - f.ask),
                      ("no", f.ask if f.side == "no" else 1.0 - f.ask)):
        ok, _p, edge = would_trade(side_p(p_yes, side), ask, calibrator)
        if ok and edge > best_edge:
            best_side, best_edge = side, edge

    banded = _cfg.near_atm_only and not (
        _cfg.near_atm_fair_p_min <= p_yes <= _cfg.near_atm_fair_p_max
    )
    return Decision(p_yes, None if banded else best_side, banded, best_side)


# ── loading ──────────────────────────────────────────────────────────────────

def load_strikes_from_chain(events_db: Path, tickers: set[str]) -> dict[str, float]:
    """
    floor_strike recovered from the PRECEDING window's settlement.

    kbot.strategy.btc_hourly's module docstring records this as a property of
    the series: a KXBTC15M window's floor_strike is the BTC reference observed
    at the window's OPEN, which is the settlement value -- the
    `expiration_value` -- of the window that closed at that same instant. So a
    ticker whose predecessor settled while the bot was running has its strike
    already in the log, exactly, with no network call.

    Only fills whose immediate predecessor was itself traded are covered, so
    this is a partial source; it runs first because it is free and offline, and
    the recordings / API fill in the rest. Windows are 15 minutes: the
    predecessor of the window closing at t is the one closing at t - 900s.
    """
    conn = sqlite3.connect(f"file:{events_db}?mode=ro", uri=True)
    try:
        settled: dict[float, float] = {}       # expiry_ts -> expiration_value
        for tk, payload in conn.execute(
            "SELECT ticker, payload FROM events WHERE event_type='trade_outcome'"
        ):
            parsed = _parse_ticker(tk or "")
            if parsed is None:
                continue
            try:
                settled[parsed[1]] = float(json.loads(payload)["expiration_value"])
            except (KeyError, TypeError, ValueError):
                continue
    finally:
        conn.close()

    out: dict[str, float] = {}
    for tk in tickers:
        parsed = _parse_ticker(tk)
        if parsed is None:
            continue
        prev = settled.get(parsed[1] - 900.0)
        if prev is not None and prev > 0:
            out[tk] = prev
    return out


def load_strikes(rec_paths: list[Path], cache_path: Path, fetch: bool,
                 tickers: set[str], events_db: Path | None = None) -> dict[str, float]:
    """floor_strike per ticker: settlement chain, then recordings, then cache, then (opt-in) the API."""
    strikes: dict[str, float] = {}

    if events_db is not None:
        chained = load_strikes_from_chain(events_db, tickers)
        strikes.update(chained)
        print(f"  settlement chain (preceding window's expiration_value): {len(chained)} strike(s)")

    for p in rec_paths:
        if not p.exists():
            print(f"  WARN: recording not found, skipped: {p}")
            continue
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            n = 0
            for tk, fs in conn.execute(
                "SELECT ticker, floor_strike FROM kalshi_market WHERE floor_strike IS NOT NULL"
            ):
                if tk in tickers and tk not in strikes:
                    strikes[tk] = float(fs)
                    n += 1
            print(f"  {p.name}: {n} strike(s) matched a fill")
        except sqlite3.Error as exc:
            print(f"  WARN: {p.name}: {exc}")
        finally:
            conn.close()

    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        before = len(strikes)
        for tk, fs in cached.items():
            if tk in tickers and tk not in strikes and fs is not None:
                strikes[tk] = float(fs)
        print(f"  {cache_path}: +{len(strikes) - before} strike(s)")

    missing = sorted(tickers - set(strikes))
    if missing and fetch:
        print(f"  fetching {len(missing)} strike(s) from the Kalshi read-only get_market endpoint...")
        from kbot.kalshi_client import KalshiClient

        client = KalshiClient()
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        for i, tk in enumerate(missing, 1):
            try:
                m = client.get_market(tk) or {}
                fs = m.get("floor_strike")
                cached[tk] = float(fs) if fs is not None else None
                if fs is not None:
                    strikes[tk] = float(fs)
            except Exception as exc:  # noqa: BLE001
                print(f"    {tk}: {str(exc)[:80]}")
                cached[tk] = None
            if i % 25 == 0:
                print(f"    {i}/{len(missing)}")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cached, indent=0, sort_keys=True))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cached, indent=0, sort_keys=True))
        print(f"  cached -> {cache_path}")
    elif missing:
        print(f"  {len(missing)} ticker(s) still unresolved; re-run with --fetch-strikes to fill them in")

    return strikes


def load_fills(events_db: Path, series_prefix: str) -> tuple[list[dict], int]:
    """
    (raw fill records, count of fills whose `signal` event could not be matched).

    A fill is a `position_open`. Its pricing inputs live on the `signal` event
    that produced it -- position_open carries fair_p only on later rows, but
    signal carries spot/vol/ask/fair_p on all of them. Matched by ticker +
    side + price within a 5s lookback, which either matches exactly or is
    reported as a miss; nothing is matched loosely.
    """
    conn = sqlite3.connect(f"file:{events_db}?mode=ro", uri=True)
    try:
        opens = conn.execute(
            "SELECT ts, ticker, payload FROM events "
            "WHERE event_type='position_open' AND ticker LIKE ? ORDER BY ts",
            (series_prefix + "%",),
        ).fetchall()
        out, unmatched = [], 0
        for ts, ticker, payload in opens:
            po = json.loads(payload)
            row = conn.execute(
                "SELECT payload FROM events WHERE event_type='signal' AND ticker=? "
                "AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                (ticker, ts + 0.001, ts - 5.0),
            ).fetchone()
            if row is None:
                unmatched += 1
                continue
            sg = json.loads(row[0])
            if sg.get("side") != po.get("side") or abs(float(sg.get("ask", -1)) - float(po["price"])) > 1e-6:
                unmatched += 1
                continue
            out.append({"ts": ts, "ticker": ticker, **sg})
        return out, unmatched
    finally:
        conn.close()


def build_fills(raw: list[dict], strikes: dict[str, float]) -> tuple[list[Fill], dict[str, int]]:
    fills: list[Fill] = []
    dropped = {"no_strike": 0, "unparseable": 0, "expired": 0, "bad_inputs": 0}
    for r in raw:
        parsed = _parse_ticker(r["ticker"])
        if parsed is None:
            dropped["unparseable"] += 1
            continue
        _raw_strike, expiry_ts, direction = parsed
        k = strikes.get(r["ticker"])
        if k is None:
            dropped["no_strike"] += 1
            continue
        t_years = (expiry_ts - r["ts"]) / _SECONDS_PER_YEAR
        if t_years <= 0:
            dropped["expired"] += 1
            continue
        spot, sigma, ask = r.get("btc_price"), r.get("vol"), r.get("ask")
        if not spot or not sigma or ask is None or not (0.0 < ask < 1.0):
            dropped["bad_inputs"] += 1
            continue
        fills.append(Fill(
            ticker=r["ticker"], ts=r["ts"], side=r["side"], ask=float(ask),
            spot=float(spot), sigma_logged=float(sigma), strike=float(k),
            direction=direction, t_years=t_years,
            logged_fair_p=float(r["fair_p"]),
            floored=abs(float(sigma) - _cfg.vol_floor) < _FLOOR_EPS,
        ))
    return fills, dropped


def load_champion_calibrator(registry_db: Path):
    if not registry_db.exists():
        print(f"  no registry at {registry_db}; treating the calibrator as absent")
        return None
    conn = sqlite3.connect(f"file:{registry_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT tag, calibrator FROM model_versions WHERE is_champion=1 "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row or row[1] is None:
        print("  no champion calibrator in the registry")
        return None
    print(f"  champion calibrator: {row[0]}")
    return pickle.loads(row[1])


def load_sub_sigmas(vol_csv: Path, floor: float) -> list[float]:
    """Measured sigmas STRICTLY BELOW the old floor -- the only ones a floored fill could have had."""
    out: list[float] = []
    with open(vol_csv, newline="") as f:
        for row in csv.DictReader(f):
            try:
                s = float(row["sigma_ewma_900s"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0.0 < s < floor:
                out.append(s)
    return out


# ── reporting ────────────────────────────────────────────────────────────────

def _pct(v: list[float], q: float) -> float:
    if not v:
        return float("nan")
    s = sorted(v)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[int(pos)] if lo == hi else s[lo] + (s[hi] - s[lo]) * (pos - lo)


def describe(name: str, v: list[float]) -> None:
    if not v:
        print(f"  {name:<34} (no usable fills)")
        return
    print(f"  {name:<34} n={len(v):<5d} min={min(v):.4f} med={_pct(v, 50):.4f} "
          f"max={max(v):.4f} sd={statistics.pstdev(v):.4f} "
          f"uniq(4dp)={len({round(x, 4) for x in v}):<5d} "
          f"iqr={_pct(v, 25):.4f}-{_pct(v, 75):.4f}")


def variants(basis: float) -> list[Variant]:
    """BEFORE, each fix alone, then all together -- so each one's own contribution is visible."""
    vs = [
        Variant("BEFORE (as traded)", False, 0.0, 0.0),
        Variant("FIX1 vol floor only", True, 0.0, 0.0),
        Variant("FIX2 settlement only", False, _cfg.settlement_avg_seconds, 0.0),
    ]
    if basis:
        vs.append(Variant("FIX3 basis only", False, 0.0, basis))
    vs.append(Variant("ALL FIXES", True, _cfg.settlement_avg_seconds, basis))
    return vs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=Path, default=Path("kbot_events.db"))
    ap.add_argument("--registry", type=Path, default=Path("kbot_registry.db"))
    ap.add_argument("--series", default="KXBTC15M", help="ticker series prefix (default: KXBTC15M)")
    ap.add_argument("--rec", action="append", type=Path, default=[],
                    help="rec_*.sqlite to mine floor_strike from; repeat")
    ap.add_argument("--strike-cache", type=Path, default=Path("data/floor_strikes.json"))
    ap.add_argument("--fetch-strikes", action="store_true",
                    help="opt in to the Kalshi READ-ONLY get_market endpoint for missing strikes")
    ap.add_argument("--vol-csv", type=Path, default=None,
                    help="measured sigmas from measure_basis_and_vol.py --out-vol-csv")
    ap.add_argument("--sub-sigma", type=float, default=None,
                    help="single fixed substitute sigma for floored fills (sensitivity check)")
    ap.add_argument("--basis-dollars", type=float, default=0.0,
                    help="Coinbase-minus-index basis to subtract from spot; 0 = no correction "
                         "(leave 0 unless section 3 of measure_basis_and_vol.py says otherwise)")
    ap.add_argument("--mc-draws", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260820)
    args = ap.parse_args()

    if not args.events.exists():
        print(f"ERROR: {args.events} not found (the live copy lives on the VM).")
        return 1

    # ── Section 1: the vol floor's binding rate ─────────────────────────────
    print("=" * 78)
    print("SECTION 1 -- HOW OFTEN DID THE VOL FLOOR ACTUALLY BIND?")
    print("=" * 78)
    print(f"  floor under test: cfg.vol_floor = {_cfg.vol_floor}")
    print("  A logged vol exactly equal to the floor means max(EWMA, floor) returned the")
    print("  FLOOR: sigma was a constant, not an estimate.")
    print()
    conn = sqlite3.connect(f"file:{args.events}?mode=ro", uri=True)
    try:
        for label, where in (
            ("all evaluations", "event_type IN ('signal','evaluation','skip')"),
            ("signals only", "event_type='signal'"),
        ):
            rows = conn.execute(
                f"SELECT payload FROM events WHERE {where} AND ticker LIKE ?",
                (args.series + "%",),
            ).fetchall()
            vols = []
            for (payload,) in rows:
                v = json.loads(payload).get("vol")
                if v is not None:
                    vols.append(float(v))
            if not vols:
                print(f"  {label:<18} (none)")
                continue
            nf = sum(1 for v in vols if abs(v - _cfg.vol_floor) < _FLOOR_EPS)
            nc = sum(1 for v in vols if abs(v - _cfg.vol_sanity_cap) < _FLOOR_EPS)
            print(f"  {label:<18} n={len(vols):<7d} floor bound {nf} ({100.0 * nf / len(vols):.1f}%)  "
                  f"sanity cap hit {nc} ({100.0 * nc / len(vols):.1f}%)")
            print(f"                     vol p5={_pct(vols, 5):.4f} p50={_pct(vols, 50):.4f} "
                  f"p95={_pct(vols, 95):.4f} max={max(vols):.4f}")
    finally:
        conn.close()
    print()
    print("  NOTE the mechanism, not just the rate: KXBTC15M is a single-binary series with")
    print("  no range ladder, so kbot.vol.implied_vol_from_ladder can never fit it and EVERY")
    print("  evaluation lands on the floored fallback. For this series the floor is not a")
    print("  backstop behind a better estimator -- it is the front door.")

    # ── load fills ──────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 2 -- RECONSTRUCTING THE FILLS")
    print("=" * 78)
    raw, unmatched = load_fills(args.events, args.series)
    print(f"  position_open events for {args.series}: {len(raw) + unmatched} "
          f"({unmatched} without a matching signal event -- dropped)")
    strikes = load_strikes(args.rec, args.strike_cache, args.fetch_strikes,
                           {r["ticker"] for r in raw}, events_db=args.events)
    fills, dropped = build_fills(raw, strikes)
    print(f"  usable fills: {len(fills)}   dropped: {dropped}")
    if not fills:
        print("  Nothing to replay. Resolve floor_strike first (--rec / --fetch-strikes).")
        return 1
    n_floored = sum(1 for f in fills if f.floored)
    print(f"  of those, floor bound on {n_floored} ({100.0 * n_floored / len(fills):.1f}%)")

    calib = load_champion_calibrator(args.registry)

    # ── Section 3: reconstruction cross-check ───────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 3 -- RECONSTRUCTION CROSS-CHECK")
    print("=" * 78)
    print("  calibrator(model_p(S,K,sigma,T)) must reproduce the fair_p that was logged at")
    print("  the time. If it does not, the strikes or the timestamps are wrong and NOTHING")
    print("  below this line means anything.")
    before = Variant("BEFORE", False, 0.0, 0.0)
    errs = []
    for f in fills:
        p_yes = raw_fair_p_yes(f, before, None)
        if p_yes is None:
            continue
        errs.append(abs(apply_calibrator(calib, side_p(p_yes, f.side)) - f.logged_fair_p))
    if errs:
        print(f"  |replayed - logged| over {len(errs)} fills: "
              f"med={_pct(errs, 50):.5f} p95={_pct(errs, 95):.5f} max={max(errs):.5f}")
        if _pct(errs, 95) > 0.01:
            print("  *** WARNING: reconstruction does not reproduce the logged fair_p. ***")
            print("  *** Treat sections 4-6 as unvalidated until this is under 0.01.    ***")
        else:
            print("  OK -- the replay reproduces the historical model output.")

    # ── substitute sigma ────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 4 -- SUBSTITUTE SIGMA FOR FLOORED FILLS")
    print("=" * 78)
    sub_pool: list[float] = []
    if args.vol_csv and args.vol_csv.exists():
        sub_pool = load_sub_sigmas(args.vol_csv, _cfg.vol_floor)
        print(f"  {args.vol_csv}: {len(sub_pool)} measured sigma(s) below {_cfg.vol_floor}")
        if sub_pool:
            print(f"    q10={_pct(sub_pool, 10):.4f} q25={_pct(sub_pool, 25):.4f} "
                  f"q50={_pct(sub_pool, 50):.4f} q75={_pct(sub_pool, 75):.4f} "
                  f"q90={_pct(sub_pool, 90):.4f}")
    elif args.sub_sigma:
        sub_pool = [args.sub_sigma]
        print(f"  fixed substitute sigma = {args.sub_sigma}")
    else:
        print("  none supplied -- fix-1 arms will cover only the NON-FLOORED fills.")
    print()
    print("  The floored fills' true EWMA is not recoverable: max(EWMA, floor) threw it away.")
    print("  Every fix-1 number below is therefore conditional on this substitute. The")
    print("  NON-FLOORED subset at the end of section 5 needs no substitute at all and is")
    print("  the assumption-free half of the answer.")

    sweeps: list[tuple[str, float | None]] = []
    if len(sub_pool) > 1:
        sweeps = [(f"sub-sigma q{q}", _pct(sub_pool, q)) for q in (10, 25, 50, 75, 90)]
    elif sub_pool:
        sweeps = [(f"sub-sigma {sub_pool[0]:.4f}", sub_pool[0])]
    else:
        sweeps = [("no substitute", None)]

    # ── Section 5: (a) distributions ────────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 5 -- (a) fair_p DISTRIBUTION ON HISTORICAL FILLS")
    print("=" * 78)
    print("  YES-side RAW model probability (pre-calibration): the quantity the three")
    print("  defects act on. A near-constant here is the defect; spread is not a virtue in")
    print("  itself, only evidence that the model is responding to its inputs again.")

    for sweep_name, sub in sweeps:
        print()
        print(f"  --- {sweep_name} ---")
        for v in variants(args.basis_dollars):
            ps = [p for f in fills if (p := raw_fair_p_yes(f, v, sub)) is not None]
            describe(v.name, ps)

    print()
    print("  NON-FLOORED FILLS ONLY (no substitute sigma needed -- assumption-free):")
    nf_fills = [f for f in fills if not f.floored]
    if nf_fills:
        for v in variants(args.basis_dollars):
            ps = [p for f in nf_fills if (p := raw_fair_p_yes(f, v, None)) is not None]
            describe(v.name, ps)
    else:
        print("    (every fill was floored -- there is no assumption-free subset)")

    # ── Section 6: (b) decision change ──────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 6 -- (b) DECISION CHANGE ON THE SAME FILLS")
    print("=" * 78)
    print("  Replays evaluate()'s near-ATM gate and _size_side's calibrate ->")
    print("  min_side_win_prob -> tradeable-band -> fee/slippage/min_net_edge chain.")
    print("  half_spread is not logged and is taken as 0 (equally, in every arm).")
    print()

    for calib_name, cal in (("stored champion calibrator", calib), ("calibrator CLEARED", None)):
        print(f"  === {calib_name} ===")
        for sweep_name, sub in sweeps:
            print(f"    --- {sweep_name} ---")
            for v in variants(args.basis_dollars):
                traded = same = opposite = banded = gated = unresolved = 0
                flip_unbanded = 0
                for f in fills:
                    dec = decide(f, v, sub, cal)
                    if dec.unresolved:
                        unresolved += 1
                        continue
                    if dec.side_if_unbanded is not None and dec.side_if_unbanded != f.side:
                        flip_unbanded += 1
                    if dec.side is None:
                        if dec.band_blocked:
                            banded += 1
                        else:
                            gated += 1
                    elif dec.side == f.side:
                        traded += 1
                        same += 1
                    else:
                        traded += 1
                        opposite += 1
                base = len(fills) - unresolved
                pc = (lambda n: f"{100.0 * n / base:5.1f}%" if base else "  n/a")
                print(f"      {v.name:<24} trade={traded:<4d}({pc(traded)})  "
                      f"same={same:<4d}  OPPOSITE={opposite:<4d}({pc(opposite)})  "
                      f"skip_band={banded:<4d}  skip_gates={gated:<4d}  "
                      f"[flip_if_band_off={flip_unbanded}]"
                      + (f"  unresolved={unresolved}" if unresolved else ""))
        print()

    # Monte Carlo over the empirical sub-floor distribution.
    if len(sub_pool) > 1 and args.mc_draws > 0:
        print("  === Monte Carlo over the measured sub-floor distribution ===")
        print(f"  {args.mc_draws} draws, seed {args.seed}; each floored fill gets an independent")
        print("  sigma drawn from the measured pool. Reports the 5-50-95 band, because a")
        print("  single point estimate would overstate what the logs can support.")
        rng = random.Random(args.seed)
        all_v = variants(args.basis_dollars)
        for cal_name, cal in (("champion", calib), ("cleared", None)):
            acc = {v.name: {"trade": [], "opposite": [], "flip": []} for v in all_v}
            for _ in range(args.mc_draws):
                draws = {id(f): rng.choice(sub_pool) for f in fills if f.floored}
                for v in all_v:
                    t = o = fl = 0
                    for f in fills:
                        dec = decide(f, v, draws.get(id(f)), cal)
                        if dec.side_if_unbanded is not None and dec.side_if_unbanded != f.side:
                            fl += 1
                        if dec.side is not None:
                            t += 1
                            if dec.side != f.side:
                                o += 1
                    acc[v.name]["trade"].append(t)
                    acc[v.name]["opposite"].append(o)
                    acc[v.name]["flip"].append(fl)
            print(f"    calibrator: {cal_name}")
            for v in all_v:
                a = acc[v.name]
                band = lambda k: f"{_pct(a[k], 5):.0f}/{_pct(a[k], 50):.0f}/{_pct(a[k], 95):.0f}"  # noqa: E731
                print(f"      {v.name:<24} of {len(fills)} fills, p5/p50/p95:  "
                      f"trade {band('trade')}   OPPOSITE {band('opposite')}   "
                      f"flip_if_band_off {band('flip')}")

    # ── Section 7: (c) sanity cases ─────────────────────────────────────────
    print()
    print("=" * 78)
    print("SECTION 7 -- (c) CONCRETE SANITY CASES")
    print("=" * 78)
    sub_mid = _pct(sub_pool, 50) if sub_pool else None
    # Spread the picks across |ln(S/K)| / (sigma*sqrt(T)) -- the quantity fair_p
    # is actually a function of -- not across raw dollar gaps, which conflate a
    # $100 move at 13 minutes with a $100 move at 6.
    def _z(f: Fill) -> float:
        return abs(math.log(f.spot / f.strike)) / (f.sigma_logged * math.sqrt(f.t_years))
    picked = sorted(fills, key=_z)
    idx = [0, len(picked) // 4, len(picked) // 2, (3 * len(picked)) // 4, len(picked) - 1]
    show = [picked[i] for i in sorted(set(idx))]
    all_v = variants(args.basis_dollars)
    for f in show:
        mins = f.t_years * _SECONDS_PER_YEAR / 60.0
        gap = f.spot - f.strike
        print()
        print(f"  {f.ticker}  spot=${f.spot:,.2f}  strike=${f.strike:,.2f}  "
              f"gap=${gap:+,.2f}  T={mins:.2f}min  sigma_logged={f.sigma_logged:.4f}"
              + ("  [FLOORED]" if f.floored else ""))
        for v in all_v:
            p = raw_fair_p_yes(f, v, sub_mid)
            if p is None:
                print(f"      {v.name:<24} (needs a substitute sigma)")
                continue
            sig = sub_mid if (v.use_new_floor and f.floored and sub_mid) else f.sigma_logged
            t_eff = effective_t_years(f.t_years, v.avg_seconds)
            scale = sig * math.sqrt(t_eff) * f.spot
            print(f"      {v.name:<24} fair_p(YES)={p:.4f}   "
                  f"1sd move=${scale:,.0f}   gap={gap / scale:+.2f}sd")
        print("      one-line check: fair_p(YES) = Phi(gap / 1sd-move); a gap of one sd")
        print("      prices near 0.84, half an sd near 0.69, zero at 0.50.")

    print()
    print("  NOTE the range above. cfg.near_atm_only restricts entries to fair_p(YES) in")
    print(f"  [{_cfg.near_atm_fair_p_min}, {_cfg.near_atm_fair_p_max}], which is +/-"
          f"{norm.ppf(_cfg.near_atm_fair_p_max):.2f} sd -- so no FILL can ever sit further out")
    print("  than that, whatever the model says. The synthetic cases below exercise the")
    print("  parts of the curve real fills structurally cannot reach.")

    print()
    print("  SYNTHETIC CASES -- hand-checkable, independent of any logged data.")
    print("  Spot $60,000, 15 minutes to close, 60s settlement average, sigma as shown.")
    print("  1sd = sigma * sqrt(T_eff) * spot with T_eff = 860s.")
    t15 = 900.0 / _SECONDS_PER_YEAR
    for sigma in (0.35, 0.20, 0.10):
        t_eff = effective_t_years(t15, _cfg.settlement_avg_seconds)
        sd_dollars = sigma * math.sqrt(t_eff) * 60_000.0
        print(f"    sigma={sigma:.2f}  1sd=${sd_dollars:,.1f}")
        for gap in (0.0, 25.0, 50.0, 100.0, 200.0):
            p_old = float(norm.cdf(gap / (sigma * math.sqrt(t15) * 60_000.0)))
            p_new = float(norm.cdf(gap / sd_dollars))
            print(f"      strike ${60_000 - gap:>9,.0f} (spot {gap:+6.0f})  "
                  f"point-settlement {p_old:.4f} -> averaged {p_new:.4f}   "
                  f"({gap / sd_dollars:+.2f} sd)")
    print("    Read: at the OLD floored sigma=0.35 a $50 gap is a quarter of a sigma and")
    print("    prices at ~0.60; at a measured sigma=0.10 the same $50 gap is nearly a full")
    print("    sigma and prices near 0.85. Same market, same spot, same strike -- the vol")
    print("    input alone is what was flattening every contract toward 0.50.")

    print()
    print("=" * 78)
    print("No parameter in this script was chosen to improve any return, and no P&L was")
    print("read. Historical paper P&L predates the orderbook fix and is invalid.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
