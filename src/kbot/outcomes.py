"""
Trade-outcome recording: turn a settled Kalshi market + the positions we
held on it into `trade_outcome` telemetry events (won/lost, fee, net P&L).

Those events are what `telemetry.resolved_paper_trades` / the paper gate
(`kbot.gates.check_paper_promotion`), RULE R3 kill-fast, and
`kbot learn --evaluate` all read -- without them the whole learning/gate
layer sees "0 resolved trades".

Two deliberate corrections vs. a naive settlement model (2026-07):

  * Resolution comes from the market's `result` field ("yes"/"no"), which
    is Kalshi's authoritative settlement call, falling back to
    `expiration_value` compared against the strike. `expiration_value` is
    the measured settlement PRICE (e.g. BTC ~$63,962), NOT a $1/$0 payout --
    so "expiration_value == 1.0 means YES" is wrong; a KXBTC15M market's
    expiration_value is never 1.0.

  * P&L matches the BACKTEST convention (kbot.backtest.run_backtest):
        cost = contracts * fill_price
        fee  = fee_coefficient * contracts * fill_price * (1 - fill_price)
        won  -> net = contracts*1.0 - cost - fee
        lost -> net = -cost - fee
    i.e. it SUBTRACTS the entry cost. A formula that omits `cost` would
    overstate P&L and be incomparable to the backtest distribution that the
    paper gate and kill-fast check live results against.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

import structlog

from kbot.config import Settings, settings as _default_settings
from kbot.quantity import units_to_contracts

log = structlog.get_logger(__name__)


# ── Settlement resolution ────────────────────────────────────────────────────

def _resolve_against_strike(
    value: float,
    strike_type: str | None,
    floor_strike: float | None,
    cap_strike: float | None,
) -> bool | None:
    """
    Resolve a market to YES/NO from its settlement `value` vs its strike(s).
    Returns None when the field(s) the strike_type needs are missing (never
    guesses a False from a missing bound).
    """
    if strike_type == "less":
        return None if cap_strike is None else value < float(cap_strike)
    if strike_type == "less_or_equal":
        return None if cap_strike is None else value <= float(cap_strike)
    if strike_type == "greater":
        return None if floor_strike is None else value > float(floor_strike)
    if strike_type == "greater_or_equal":
        return None if floor_strike is None else value >= float(floor_strike)
    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            return None
        return float(floor_strike) <= value <= float(cap_strike)
    return None


def resolved_yes(market: dict) -> bool | None:
    """
    True/False if `market` has settled YES/NO, else None (not settled or
    undeterminable). Prefers the authoritative `result` field; falls back to
    the measured `expiration_value` compared against the strike.
    """
    result = str(market.get("result") or "").lower()
    if result in ("yes", "no"):
        return result == "yes"

    exp_raw = market.get("expiration_value")
    if exp_raw not in (None, ""):
        try:
            exp_val = float(exp_raw)
        except (TypeError, ValueError):
            exp_val = None
        if exp_val is not None:
            return _resolve_against_strike(
                exp_val,
                market.get("strike_type"),
                market.get("floor_strike"),
                market.get("cap_strike"),
            )
    return None


# ── P&L ──────────────────────────────────────────────────────────────────────

def compute_outcome(
    side: str,
    contracts: int,
    fill_price: float,
    is_yes_resolved: bool,
    cfg: Settings | None = None,
) -> dict:
    """
    (won, gross_pnl, fee, net_pnl) for one position on a settled market.

    net_pnl matches kbot.backtest: payout minus entry cost minus fee, so live
    outcomes are directly comparable to the backtest P&L distribution the gate
    and kill-fast compare against. `fee` uses cfg.fee_coefficient (default
    0.07), same formula as the backtest -- no cent-rounding, for that same
    comparability.
    """
    cfg = cfg or _default_settings
    side_val = (side.value if hasattr(side, "value") else str(side)).lower()
    won = (side_val == "yes" and is_yes_resolved) or (side_val == "no" and not is_yes_resolved)

    cost = contracts * fill_price
    fee = cfg.fee_coefficient * contracts * fill_price * (1.0 - fill_price)
    gross_pnl = contracts * 1.0 if won else 0.0
    net_pnl = (gross_pnl - cost - fee) if won else (-cost - fee)

    return {"won": won, "gross_pnl": gross_pnl, "fee": fee, "net_pnl": net_pnl}


def build_trade_outcome(
    ticker: str,
    side: str,
    contracts: int,
    fill_price: float,
    market: dict,
    strategy: str | None,
    cfg: Settings | None = None,
    fair_p: float | None = None,
    net_edge_dollars: float | None = None,
    raw_fair_p: float | None = None,
    is_live: bool = False,
) -> dict | None:
    """
    trade_outcome payload for one settled position, or None if the market
    isn't resolvable yet (so the caller skips rather than records a guess).
    `pnl_dollars` == net_pnl is the field the gate/kill-fast/learn all read.

    `fair_p` (the model's predicted win probability at fill time), when given,
    is carried into the payload as both `fair_p` and `model_prob` -- the
    latter is what kbot.learning.evaluate reads to bucket outcomes by
    predicted probability for the calibration report. Omitted when None
    (older positions predating fair_p on position_open events).

    `net_edge_dollars` (the strategy's predicted net edge at fill time,
    stored as `net_edge` on the position_open event) is carried in under the
    key kbot.learning.evaluate and retrain._score_window actually read --
    without it every trade reported a $0.00 predicted edge. Omitted when
    None, same convention as fair_p.

    `raw_fair_p` is the PRE-calibration model probability. Retrain fits
    calibrators on this (fair_p/model_prob is the calibrated output --
    fitting on it while applying the result to raw probs compounds a domain
    mismatch every retrain generation). Omitted when None.

    `is_live` is the real-money flag stamped on the POSITION at order-placement
    time (kbot.execution.Executor.execute -> PositionTracker.open_position),
    carried onto the outcome so the Live P&L accounting can select real trades
    without consulting any mutable current state. Unlike the fields above it is
    ALWAYS written, defaulting to False: a missing flag must read as paper, so
    an outcome recorded through a path that doesn't know can never leak into
    real-money P&L. See kbot.live_pnl.
    """
    cfg = cfg or _default_settings
    ry = resolved_yes(market)
    if ry is None:
        return None
    oc = compute_outcome(side, contracts, fill_price, ry, cfg)
    payload = {
        "ticker": ticker,
        "side": (side.value if hasattr(side, "value") else str(side)).lower(),
        "contracts": contracts,
        "fill_price": fill_price,
        "expiration_value": market.get("expiration_value"),
        "result": (str(market.get("result")).lower() if market.get("result") else None),
        "resolved_yes": ry,
        "won": oc["won"],
        "gross_pnl": round(oc["gross_pnl"], 6),
        "fee": round(oc["fee"], 6),
        "net_pnl": round(oc["net_pnl"], 6),
        "pnl_dollars": round(oc["net_pnl"], 6),  # gate/kill-fast/learn read this
        "strategy": strategy,
        "is_live": bool(is_live),  # real money at PLACEMENT time -- see docstring
        # Provenance of the P&L figures above. "local_model" means they were
        # COMPUTED from the settled market plus the modelled fee, not read off
        # the exchange. Correct and intended for paper; for real money see
        # build_live_trade_outcome, which overwrites this with the exchange's
        # own accounting. Recorded on every outcome so an estimate can never be
        # mistaken for a fact after the fact.
        "pnl_source": "local_model",
    }
    if fair_p is not None:
        payload["fair_p"] = fair_p
        payload["model_prob"] = fair_p  # kbot.learning.evaluate buckets on this
    if net_edge_dollars is not None:
        payload["net_edge_dollars"] = net_edge_dollars
    if raw_fair_p is not None:
        payload["raw_fair_p"] = raw_fair_p
    return payload


def build_live_trade_outcome(
    client: Any,
    ticker: str,
    side: str,
    contracts: int,
    fill_price: float,
    market: dict,
    strategy: str | None,
    cfg: Settings | None = None,
    **kwargs: Any,
) -> dict | None:
    """
    trade_outcome payload for a REAL-MONEY position, reconciled against Kalshi.

    The settled result and the realized P&L come from the exchange's own
    position record (GET /portfolio/positions via kbot.live_settlement), not
    from the local model: `pnl_dollars` is what Kalshi actually credited, net
    of the fee it actually charged. That is what makes the Live P&L tab
    reconcile against a real account balance instead of approximating it.

    The locally computed figures are retained alongside as
    `local_pnl_estimate` / `local_fee_estimate` so the two can be compared
    (a persistent divergence means the fee model needs revisiting).

    Falls back to the local computation, tagged `pnl_source="local_estimate"`,
    when the exchange record is unreachable or the market has not settled
    there yet. Returns None when neither source can resolve the market, so the
    caller retries later rather than recording a guess.

    `is_live` is forced True here: this function is only reached for a position
    whose placement-time stamp already said real money.
    """
    from kbot.live_settlement import fetch_settled_position

    kwargs.pop("is_live", None)
    local = build_trade_outcome(
        ticker, side, contracts, fill_price, market, strategy, cfg,
        is_live=True, **kwargs,
    )

    reconciled = fetch_settled_position(
        client, ticker, side=side, contracts=contracts, fill_price=fill_price,
    )

    if reconciled is None:
        if local is not None:
            local["pnl_source"] = "local_estimate"
            log.warning(
                "outcomes.live_reconcile_unavailable",
                ticker=ticker, side=str(side), fallback="local_estimate",
            )
        return local

    # The exchange settled it. Build on the local payload when there is one
    # (it carries strategy/fair_p/net_edge and the model comparison), else from
    # the identity fields alone -- a market Kalshi has settled must record an
    # outcome even if our own get_market call came back unresolvable.
    if local is not None:
        payload = dict(local)
        payload["local_pnl_estimate"] = local.get("net_pnl")
        payload["local_fee_estimate"] = local.get("fee")
    else:
        payload = {
            "ticker": ticker,
            "side": (side.value if hasattr(side, "value") else str(side)).lower(),
            "contracts": contracts,
            "fill_price": fill_price,
            "expiration_value": market.get("expiration_value") if market else None,
            "strategy": strategy,
            "is_live": True,
        }
        for key in ("fair_p", "net_edge_dollars", "raw_fair_p"):
            value = kwargs.get(key if key != "net_edge_dollars" else "net_edge_dollars")
            if value is not None:
                payload[key] = value
        if kwargs.get("fair_p") is not None:
            payload["model_prob"] = kwargs["fair_p"]

    payload.update(reconciled)
    payload["is_live"] = True
    return payload


def strategy_for_ticker(ticker: str) -> str | None:
    """Best-effort strategy name from a ticker prefix, for records missing it."""
    if ticker.startswith("KXBTC15M-"):
        return "kxbtc15m"
    if ticker.startswith("KXETH15M-"):
        return "kxeth15m"
    if ticker.startswith("KXBTC-"):
        return "btc_hourly"
    return None


def record_ticker_outcomes(
    ticker: str,
    positions: list[dict],
    market: dict,
    telemetry: Any,
    strategy: str | None,
    cfg: Settings | None = None,
    *,
    client: Any = None,
) -> int:
    """
    Log a trade_outcome for each position (dicts with side/contracts/price) on
    the now-settled `market`. Returns how many were recorded. Skips positions
    the market can't resolve (returns 0 for the ticker in that case).

    `client`, when supplied, routes REAL-MONEY positions (the position row's
    own placement-time is_live stamp) through Kalshi's settled position record
    instead of the local model -- see build_live_trade_outcome. PAPER positions
    always take the local path, unchanged: their P&L must stay comparable to
    the backtest distribution the paper gate scores against, and there is no
    exchange record of a trade that was never placed.

    The return value is what the caller uses to decide whether the position may
    be cleared. A position whose outcome could NOT be recorded is not counted,
    so it can be retried rather than silently dropped.
    """
    recorded = 0
    for pos in positions:
        side = pos.get("side")
        # PositionTracker rows carry the exact size as `contracts_units`
        # (integer hundredths, see kbot.quantity). Everything below this point
        # is dollar math, so convert once, here, and keep the fraction: a
        # 0.5-contract fill settles for half as much as a whole one.
        # `contracts` is still accepted for callers that reconstruct positions
        # from the event log, where the field is a whole-contract count.
        if pos.get("contracts_units") is not None:
            contracts = units_to_contracts(pos["contracts_units"])
        else:
            contracts = pos.get("contracts")
        price = pos.get("price")
        if not side or not contracts or price is None:
            continue
        # fair_p / net_edge from the position dict if present, else from the
        # position_open event (the tracker doesn't store them; the event does).
        fair_p = pos.get("fair_p")
        if fair_p is None and hasattr(telemetry, "position_open_fair_p"):
            fair_p = telemetry.position_open_fair_p(ticker, side)
        net_edge = pos.get("net_edge")
        if net_edge is None and hasattr(telemetry, "position_open_field"):
            net_edge = telemetry.position_open_field(ticker, side, "net_edge")
        raw_fair_p = pos.get("raw_fair_p")
        if raw_fair_p is None and hasattr(telemetry, "position_open_field"):
            raw_fair_p = telemetry.position_open_field(ticker, side, "raw_fair_p")
        # The position row's own placement-time stamp (persisted by
        # PositionTracker); absent -> paper, never inferred from cfg.
        is_live = bool(pos.get("is_live", False))
        if is_live and client is not None:
            payload = build_live_trade_outcome(
                client, ticker, side, contracts, price, market, strategy, cfg,
                fair_p=fair_p, net_edge_dollars=net_edge, raw_fair_p=raw_fair_p,
            )
        else:
            payload = build_trade_outcome(
                ticker, side, contracts, price, market, strategy, cfg,
                fair_p=fair_p, net_edge_dollars=net_edge, raw_fair_p=raw_fair_p,
                is_live=is_live,
            )
        if payload is None:
            log.warning("outcomes.unresolved", ticker=ticker, side=side, is_live=is_live)
            continue
        telemetry.log_trade_outcome(ticker, payload)
        recorded += 1
    return recorded


# ── One-time backfill from telemetry position_open events ────────────────────

def backfill_outcomes(
    client: Any,
    telemetry: Any,
    cfg: Settings | None = None,
    *,
    get_market: Callable[[str], dict] | None = None,
) -> dict:
    """
    Record trade_outcome events for historical closed positions that never got
    one. Source is the telemetry `position_open` events (each carries
    ticker/side/contracts/price/strategy); the positions DB is transient and
    may already be empty. Idempotent: skips any (ticker, side) that already has
    a trade_outcome. One get_market call per unique ticker (cached).

    Returns a summary dict (recorded / skipped_existing / skipped_unresolved /
    skipped_incomplete / per_strategy).
    """
    cfg = cfg or _default_settings
    fetch = get_market if get_market is not None else client.get_market

    existing = {
        (e["ticker"], e["payload"].get("side"))
        for e in telemetry.recent(event_type="trade_outcome", limit=1_000_000)
    }

    # One position per (ticker, side); keep the first seen (opens shouldn't dup).
    positions: dict[tuple[str, str], dict] = {}
    for e in telemetry.recent(event_type="position_open", limit=1_000_000):
        p = e["payload"]
        key = (e["ticker"], p.get("side"))
        positions.setdefault(key, {**p, "ticker": e["ticker"]})

    market_cache: dict[str, dict | None] = {}
    recorded = skipped_existing = skipped_unresolved = skipped_incomplete = 0
    per_strategy: dict[str, int] = defaultdict(int)

    for (ticker, side), p in positions.items():
        if (ticker, side) in existing:
            skipped_existing += 1
            continue
        contracts, price = p.get("contracts"), p.get("price")
        if not side or not contracts or price is None:
            skipped_incomplete += 1
            continue

        if ticker not in market_cache:
            try:
                market_cache[ticker] = fetch(ticker)
            except Exception as exc:  # noqa: BLE001
                log.warning("outcomes.get_market_failed", ticker=ticker, error=str(exc)[:120])
                market_cache[ticker] = None
        market = market_cache[ticker]
        if not market:
            skipped_unresolved += 1
            continue

        strategy = p.get("strategy") or strategy_for_ticker(ticker)
        payload = build_trade_outcome(
            ticker, side, contracts, price, market, strategy, cfg,
            fair_p=p.get("fair_p"), net_edge_dollars=p.get("net_edge"),
            raw_fair_p=p.get("raw_fair_p"),
            # From the position_open event's own placement-time stamp. Events
            # predating it have no flag -> paper. Deliberately NOT taken from
            # the event's dry_run column: that records the config of whichever
            # process wrote the row, so backfilling from a CLI shell would
            # relabel history by whatever DRY_RUN happened to be set there.
            is_live=bool(p.get("is_live", False)),
        )
        if payload is None:
            skipped_unresolved += 1
            continue
        telemetry.log_trade_outcome(ticker, payload)
        recorded += 1
        per_strategy[strategy or "unknown"] += 1

    summary = {
        "recorded": recorded,
        "skipped_existing": skipped_existing,
        "skipped_unresolved": skipped_unresolved,
        "skipped_incomplete": skipped_incomplete,
        "per_strategy": dict(per_strategy),
    }
    log.info("outcomes.backfill_done", **summary)
    return summary


def reconcile_live_outcomes(
    client: Any,
    telemetry: Any,
    cfg: Settings | None = None,
    *,
    repair_is_live: bool = False,
) -> dict:
    """
    Record a Kalshi-reconciled trade_outcome for every REAL-MONEY position that
    never got one. Idempotent; safe to re-run.

    Source is the `position_open` events carrying `is_live: true` -- the
    placement-time stamp, the same one kbot.live_pnl selects on. The positions
    DB is not consulted: a settled position is deleted from it, and the whole
    reason a live trade can go unrecorded is that the row was cleared before
    (or without) its outcome being written.

    Idempotency is on (ticker, side) -- the identity kbot.outcomes.
    backfill_outcomes dedupes on, kbot.live_pnl.live_outcomes collapses on, and
    SheetsClient.upsert_live_pnl keys the sheet on. A (ticker, side) that
    already has a trade_outcome of ANY kind is left completely alone, so this
    can never append a duplicate row to Live P&L no matter how often it runs.

    `repair_is_live` addresses the one case the above deliberately skips: an
    outcome that EXISTS but is stamped paper while its position_open says the
    order was placed with real money. That is a real trade missing from
    real-money accounting. It is off by default because flipping is_live on a
    recorded outcome is the one edit that can fabricate real trades if the
    premise is ever wrong -- the count is always reported so the operator can
    see whether any exist and opt in. Even when on, it only ever flips outcomes
    whose OWN position_open event says live; it never infers from config or
    gate level.

    Returns a summary dict.
    """
    cfg = cfg or _default_settings

    outcomes_by_key: dict[tuple[str, str], dict] = {}
    for e in telemetry.recent(event_type="trade_outcome", limit=1_000_000):
        outcomes_by_key.setdefault((e["ticker"], e["payload"].get("side")), e)

    live_opens: dict[tuple[str, str], dict] = {}
    for e in telemetry.recent(event_type="position_open", limit=1_000_000):
        p = e["payload"]
        if not bool(p.get("is_live", False)):
            continue
        live_opens.setdefault((e["ticker"], p.get("side")), {**p, "ticker": e["ticker"]})

    market_cache: dict[str, dict] = {}
    recorded = skipped_existing = skipped_unsettled = skipped_incomplete = 0
    mislabelled = repaired = 0
    per_source: dict[str, int] = defaultdict(int)

    for (ticker, side), p in sorted(live_opens.items()):
        existing = outcomes_by_key.get((ticker, side))
        if existing is not None:
            skipped_existing += 1
            if not bool(existing["payload"].get("is_live", False)):
                mislabelled += 1
                if repair_is_live:
                    _set_outcome_live(telemetry, existing)
                    repaired += 1
                else:
                    log.warning(
                        "outcomes.live_outcome_stamped_paper",
                        ticker=ticker, side=side,
                        hint="re-run with repair_is_live=True to correct",
                    )
            continue

        contracts, price = p.get("contracts"), p.get("price")
        if not side or not contracts or price is None:
            skipped_incomplete += 1
            continue

        if ticker not in market_cache:
            try:
                market_cache[ticker] = client.get_market(ticker) or {}
            except Exception as exc:  # noqa: BLE001 — an unreachable market must
                # not abort the run; Kalshi's position record may still settle it.
                log.warning("outcomes.get_market_failed", ticker=ticker, error=str(exc)[:120])
                market_cache[ticker] = {}

        payload = build_live_trade_outcome(
            client, ticker, side, contracts, price, market_cache[ticker],
            p.get("strategy") or strategy_for_ticker(ticker), cfg,
            fair_p=p.get("fair_p"), net_edge_dollars=p.get("net_edge"),
            raw_fair_p=p.get("raw_fair_p"),
        )
        if payload is None:
            skipped_unsettled += 1
            log.info("outcomes.live_not_settled_yet", ticker=ticker, side=side)
            continue

        telemetry.log_trade_outcome(ticker, payload)
        recorded += 1
        per_source[payload.get("pnl_source", "unknown")] += 1

    summary = {
        "recorded": recorded,
        "skipped_existing": skipped_existing,
        "skipped_unsettled": skipped_unsettled,
        "skipped_incomplete": skipped_incomplete,
        "mislabelled_paper": mislabelled,
        "repaired_is_live": repaired,
        "per_source": dict(per_source),
    }
    log.info("outcomes.reconcile_live_done", **summary)
    return summary


def _set_outcome_live(telemetry: Any, event: dict) -> None:
    """
    Stamp is_live=True on an already-recorded trade_outcome whose position_open
    says it was placed with real money.

    Same deliberate exception to the append-only events log as backfill_fair_p:
    a JSON field updated in place, no row inserted or deleted, so the
    (ticker, side) identity every downstream dedupe relies on is untouched.
    """
    import json as _json

    conn = telemetry._conn
    row = conn.execute(
        "SELECT id, payload FROM events WHERE event_type = 'trade_outcome' "
        "AND ticker = ? AND json_extract(payload, '$.side') = ? ORDER BY id LIMIT 1",
        (event["ticker"], event["payload"].get("side")),
    ).fetchone()
    if row is None:
        return
    payload = _json.loads(row[1])
    payload["is_live"] = True
    payload["is_live_repaired"] = True  # audit trail: this flag was not original
    conn.execute("UPDATE events SET payload = ? WHERE id = ?", (_json.dumps(payload), row[0]))
    conn.commit()
    log.info("outcomes.is_live_repaired", ticker=event["ticker"],
             side=event["payload"].get("side"))


def backfill_fair_p(telemetry: Any) -> dict:
    """
    Retro-fill `fair_p`/`model_prob` and `net_edge_dollars` onto EXISTING
    trade_outcome events that lack them, by joining each to its position_open
    event's recorded fill-time values (fair_p / net_edge).

    For trade_outcomes whose position_open event also lacks the source field
    (older records that predate it), nothing is changed -- they stay as-is.
    Idempotent per-field: a field an outcome already carries is never
    rewritten. Returns a summary (updated / already_had / no_source, counted
    per event: an event is "updated" if at least one field was filled).

    NOTE: this deliberately writes to the otherwise append-only events log --
    it's a one-time enrichment (updating a JSON field in place, not inserting
    or deleting rows), the only place the codebase mutates a logged event.
    """
    import json as _json

    conn = telemetry._conn  # one-time migration reaches into the store directly
    rows = conn.execute(
        "SELECT id, ticker, payload FROM events WHERE event_type = 'trade_outcome'"
    ).fetchall()

    updated = already_had = no_source = 0
    for event_id, ticker, payload_json in rows:
        payload = _json.loads(payload_json)
        side = payload.get("side")
        changed = False
        missing_any = False

        if payload.get("fair_p") is None:
            missing_any = True
            fair_p = telemetry.position_open_fair_p(ticker, side) if side else None
            if fair_p is not None:
                payload["fair_p"] = fair_p
                payload["model_prob"] = fair_p
                changed = True

        if payload.get("net_edge_dollars") is None:
            missing_any = True
            net_edge = (
                telemetry.position_open_field(ticker, side, "net_edge")
                if side and hasattr(telemetry, "position_open_field") else None
            )
            if net_edge is not None:
                payload["net_edge_dollars"] = net_edge
                changed = True

        if changed:
            conn.execute("UPDATE events SET payload = ? WHERE id = ?", (_json.dumps(payload), event_id))
            updated += 1
        elif missing_any:
            no_source += 1
        else:
            already_had += 1
    conn.commit()

    summary = {"updated": updated, "already_had": already_had, "no_source": no_source}
    log.info("outcomes.backfill_fair_p_done", **summary)
    return summary
