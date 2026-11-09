"""
Google Sheets sync — mirrors the SQLite trade journal into the configured
Google Sheet so trades and learning data are human-readable.

Design principles:
- All public write methods are best-effort: exceptions are logged and absorbed,
  never propagated to the trading loop.
- The Sheets API enforces ~60 write-requests/minute per project; _with_backoff
  retries on HTTP 429 with simple exponential back-off.
- SheetsClient is lazy-connected: gspread auth happens on the first call that
  needs the spreadsheet, not at import time, so `from kbot.sheets import ...`
  works even without a service-account file present.
- Pass `_spreadsheet` to the constructor to inject a mock in tests.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any

import structlog

from kbot.config import Settings, settings as _default_settings

log = structlog.get_logger(__name__)

# ── Tab names and their column headers ───────────────────────────────────────

TAB_TRADE_LOG     = "Trade Log"
TAB_DAILY_SUMMARY = "Daily Summary"
TAB_CALIBRATION   = "Calibration"
TAB_SKIPPED       = "Skipped Signals"
TAB_RESEARCH      = "Research Log"
TAB_MARKOUT       = "Markout"
TAB_ARB_LOG       = "Arb Log"
TAB_PERFORMANCE   = "Performance"
TAB_LIVE_PNL      = "Live P&L"

# Ordered column headers for each tab.  Row values are built by looking up
# each header key in the supplied dict, so column order is authoritative here.
_HEADERS: dict[str, list[str]] = {
    TAB_TRADE_LOG: [
        "ts_utc", "event_type", "ticker", "dry_run",
        "side", "contracts", "price_dollars", "net_edge_dollars",
        "kelly_fraction_used", "capped", "order_id",
    ],
    TAB_DAILY_SUMMARY: [
        "date", "num_trades", "wins", "losses", "win_rate",
        "gross_pnl", "fees", "net_pnl", "fee_drag_pct",
        "end_bankroll", "kill_switch_tripped",
    ],
    TAB_CALIBRATION: [
        "bucket_label", "bucket_low", "bucket_high",
        "count", "realized_win_rate", "brier_contribution",
    ],
    TAB_SKIPPED: [
        "ts_utc", "ticker", "model_prob", "market_price",
        "edge", "net_edge", "reason_skipped",
    ],
    TAB_RESEARCH: [
        "ts_utc", "topic", "summary", "risk_flags",
        "recommended_action", "raw_json",
    ],
    TAB_MARKOUT: [
        "strategy", "avg_markout_60s", "pct_negative_60s",
        "flag_state", "sample_size", "updated_at_utc",
    ],
    TAB_ARB_LOG: [
        "ts_utc", "check_type", "ticker_a", "ticker_b", "detail", "net_dollars",
    ],
    TAB_PERFORMANCE: [
        "updated_at_utc", "total_trades", "wins", "losses", "win_rate",
        "gross_pnl", "net_pnl", "brier_score", "avg_predicted_edge",
        "avg_realized_pnl", "edge_prediction_err",
    ],
    TAB_LIVE_PNL: [
        "resolved_at", "ticker", "side", "contracts", "fill_price",
        "pnl_dollars", "cumulative_realized_pnl", "kalshi_balance",
        "balance_minus_cumulative",
    ],
}


# ── Strategy-scoped tabs ─────────────────────────────────────────────────────
#
# Two deployments (kxbtc15m and kxeth15m) share ONE spreadsheet via the same
# KALSHI_SHEET_ID. Before this map, both pushed to the single "Calibration" tab
# and whichever ran `kbot learn --evaluate` last silently replaced the other's
# data (push_calibration is a clear-and-rewrite, and each deployment has its
# own events DB, so ETH's buckets contain no BTC trades at all).
#
# The mapping is explicit rather than a computed prefix because the names
# aren't uniform: BTC's report tabs keep their original bare names
# byte-for-byte (renaming a live tab would orphan every chart and reference
# pointing at it), while its P&L tab is "BTC Live P&L" -- an unambiguous name
# is worth more than a consistent rule on the one tab that tracks real money.
#
# An unknown or absent strategy falls back to the base names, which is exactly
# the pre-existing behavior for any caller that doesn't pass one.
_STRATEGY_TABS: dict[str, dict[str, str]] = {
    "kxbtc15m": {
        TAB_CALIBRATION: "Calibration",
        TAB_PERFORMANCE: "Performance",
        TAB_LIVE_PNL:    "BTC Live P&L",
    },
    "kxeth15m": {
        TAB_CALIBRATION: "ETH Calibration",
        TAB_PERFORMANCE: "ETH Performance",
        TAB_LIVE_PNL:    "ETH Live P&L",
    },
}

# Tabs whose title depends on the strategy. Everything else is shared across
# deployments and keeps its single global name.
_SCOPED_TABS: tuple[str, ...] = (TAB_CALIBRATION, TAB_PERFORMANCE, TAB_LIVE_PNL)


def tab_for(base_tab: str, strategy: str | None) -> str:
    """
    The actual worksheet title `base_tab` maps to for `strategy`.

    Unscoped tabs, unknown strategies and strategy=None all return `base_tab`
    unchanged -- so every existing caller keeps writing exactly where it did.
    """
    if strategy is None or base_tab not in _SCOPED_TABS:
        return base_tab
    return _STRATEGY_TABS.get(strategy, {}).get(base_tab, base_tab)


# ── Sync constants ───────────────────────────────────────────────────────────

# Only these event types are trade-journal data; evaluation and heartbeat are
# internal telemetry and must never be backfilled into the Trade Log tab.
_SYNC_EVENT_TYPES: tuple[str, ...] = (
    "signal", "order", "position_open", "position_closed"
)

# Skipped-Signals near-miss threshold: only include skips within 2 cents of
# the edge threshold.  Far-OTM signals (net_edge << 0) are noise.
_SKIP_NEAR_MISS_THRESHOLD: float = -0.02

# Batch-write parameters: 50 rows per API call, 1-second pause between batches.
_SYNC_BATCH_SIZE: int = 50
_SYNC_BATCH_SLEEP: float = 1.0   # seconds between batches (rate-limit guard)

# Tabs ensure_tabs() creates up front. Live P&L is excluded on purpose: it is
# created lazily by upsert_live_pnl on the first real-money trade, so a paper
# deployment never grows an empty real-money tab.
_ENSURE_TABS: tuple[str, ...] = tuple(t for t in _HEADERS if t != TAB_LIVE_PNL)


# ── Arb-log kill switch ──────────────────────────────────────────────────────
#
# The Arb Log is the only tab written from the WS message path: every book
# update re-runs the consistency scan and appends a row per standing
# detection, so a violation that persists across messages is re-logged
# indefinitely. That is what pushed the shared workbook into Google's hard
# 10,000,000-cell ceiling, after which EVERY append returns
#   APIError [400]: This action would increase the number of cells in the
#   workbook above the limit of 10000000 cells.
#
# Retrying that per message is worse than useless. It cannot succeed -- the
# cap is a property of the workbook, not a transient condition -- and each
# attempt costs two BLOCKING https round-trips on the asyncio event loop:
# gspread is synchronous, and Spreadsheet.worksheet() re-fetches the whole
# sheet metadata before the append. At the resulting call rate the 429
# back-off in _with_backoff then sleeps (also blocking) on the same loop.
# Starving the event loop is how a best-effort telemetry write turns into a
# frozen trading loop and a dead-man's-switch restart.
#
# So the first APIError latches this flag and no further arb-log write is
# attempted for the life of the process. Process-wide rather than
# per-instance because the cell cap belongs to the workbook, which every
# SheetsClient in the process shares. In-memory only on purpose: a restart
# re-tries exactly once, which is the cheapest way to notice that the tab has
# been cleared or archived.
_ARB_LOG_DISABLED: bool = False

# Skip the append when the Arb Log tab's own grid is already this large.
# The 10M cap is a WORKBOOK property, so no per-tab number can predict the
# API's answer exactly; this is a cheap upper bound on the one tab that grows
# without bound, read from grid metadata _get_or_create_worksheet has already
# fetched (no extra API call). A runaway arb log has no business holding a
# fifth of a workbook shared with the tabs that track real money.
_ARB_LOG_MAX_CELLS: int = 2_000_000


def arb_log_disabled() -> bool:
    """Whether arb-log writes have been latched off for this process."""
    return _ARB_LOG_DISABLED


def _disable_arb_log(reason: str, **fields: Any) -> None:
    """
    Latch arb-log writes off and emit the ONE warning explaining why.

    Idempotent: re-entering after the latch is set logs nothing, so this can
    be called from any failure path without reintroducing per-cycle spam.
    """
    global _ARB_LOG_DISABLED
    if _ARB_LOG_DISABLED:
        return
    _ARB_LOG_DISABLED = True
    log.warning(
        "sheets.arb_log.disabled",
        reason=reason,
        detail="arb-log writes are off until this process restarts",
        **fields,
    )


def reset_arb_log_disabled() -> None:
    """Re-arm arb-log writes. For tests only — the runtime never re-arms."""
    global _ARB_LOG_DISABLED
    _ARB_LOG_DISABLED = False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_rate_limit(exc: Exception) -> bool:
    """Duck-typed 429 check — works for gspread APIError without importing it."""
    try:
        return exc.response.status_code == 429  # type: ignore[attr-defined]
    except AttributeError:
        return False


def _is_api_error(exc: Exception) -> bool:
    """
    Whether `exc` is a gspread APIError — i.e. Google answered and refused,
    as opposed to a local/transport failure that may well succeed next time.

    Matched by class name up the MRO rather than isinstance so this module
    keeps gspread as a lazy import (see the module docstring).
    """
    return any(base.__name__ == "APIError" for base in type(exc).__mro__)


def _ts_utc(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


def _event_to_trade_row(ev: dict) -> dict:
    """Map a telemetry event dict to a Trade Log row dict."""
    payload = ev.get("payload", {})
    return {
        "ts_utc":              _ts_utc(ev["ts"]),
        "event_type":          ev.get("event_type", ""),
        "ticker":              ev.get("ticker") or "",
        "dry_run":             ev.get("dry_run", True),
        "side":                payload.get("side", ""),
        "contracts":           payload.get("contracts", ""),
        "price_dollars":       payload.get("price_dollars") or payload.get("price", ""),
        "net_edge_dollars":    payload.get("net_edge_dollars", ""),
        "kelly_fraction_used": payload.get("kelly_fraction_used", ""),
        "capped":              payload.get("capped", ""),
        "order_id":            payload.get("order_id", ""),
    }


def _cells_equal(a: Any, b: Any) -> bool:
    """
    Whether two cell values are the same for upsert purposes.

    Everything read back from Sheets is a string, so a numeric cell written as
    0.42 comes back as "0.42" and a naive != would report every row as changed
    on every run -- rewriting the whole tab each evaluate and burning API quota
    for nothing. Numbers are compared numerically (to sub-cent tolerance),
    anything else as trimmed strings.
    """
    if a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _drift(balance: Any, cumulative: Any) -> float | str:
    """
    balance - cumulative, or "" when the balance isn't a number.

    This is the column that makes fees, unsettled positions and any accounting
    gap visible as a number instead of a mystery, so it is only ever computed
    from a real observed balance -- never defaulted to 0, which would read as
    "no drift" when the truth is "no balance recorded".
    """
    try:
        return round(float(balance) - float(cumulative), 4)
    except (TypeError, ValueError):
        return ""


def _grid_cells(ws: Any) -> int | None:
    """
    row_count * col_count for `ws`, or None when it doesn't report both as
    real integers.

    None means "unknown size", and an unknown size must never be read as
    over-limit -- a worksheet stub, or a gspread object built from metadata
    that omitted gridProperties, has to fall through to attempting the write
    rather than silently disabling the log.
    """
    rows, cols = getattr(ws, "row_count", None), getattr(ws, "col_count", None)
    if isinstance(rows, int) and isinstance(cols, int):
        return rows * cols
    return None


def _event_to_skip_row(ev: dict) -> dict:
    """Map a 'skip' telemetry event to a Skipped Signals row dict."""
    payload = ev.get("payload", {})
    return {
        "ts_utc":        _ts_utc(ev["ts"]),
        "ticker":        ev.get("ticker") or payload.get("ticker", ""),
        "model_prob":    payload.get("model_prob", ""),
        "market_price":  payload.get("market_price", ""),
        "edge":          payload.get("edge", ""),
        "net_edge":      payload.get("net_edge", ""),
        "reason_skipped": payload.get("reason_skipped", ""),
    }


# ── Main client ───────────────────────────────────────────────────────────────

class SheetsClient:
    """
    Best-effort Sheets sync client.

    Usage::

        client = SheetsClient()
        client.ensure_tabs()          # idempotent — call once at startup
        client.append_trade(row)      # called after every fill
        client.append_skip(row)       # called for every skipped signal
        client.upsert_daily_summary(today, summary_dict)
        client.push_calibration(buckets)
    """

    def __init__(
        self,
        cfg: Settings | None = None,
        *,
        strategy: str | None = None,
        _spreadsheet: Any = None,   # injected in tests; None → lazy auth
    ) -> None:
        """
        `strategy` scopes the per-strategy tabs (Calibration / Performance /
        Live P&L) to this deployment -- see tab_for. Left None (the default),
        every tab keeps its original global name, so existing callers are
        unaffected. Callers that know which strategy the process runs should
        use SheetsClient.for_settings(cfg), which derives it from config.
        """
        self._cfg = cfg or _default_settings
        self._strategy = strategy
        self.__spreadsheet = _spreadsheet

    @classmethod
    def for_settings(cls, cfg: Settings | None = None, **kwargs: Any) -> "SheetsClient":
        """
        A client scoped to the single strategy this process runs.

        The strategy is read from STRATEGY_ALLOCATIONS, which is what actually
        determines what the process trades. A multi-strategy allocation has no
        single owner for a "Performance" tab, so it falls back to the shared
        base names rather than guessing which strategy the report describes.
        """
        cfg = cfg or _default_settings
        strategy: str | None = None
        try:
            from kbot.config import parse_strategy_allocations
            names = list(parse_strategy_allocations(cfg.strategy_allocations))
            if len(names) == 1:
                strategy = names[0]
        except Exception as exc:  # noqa: BLE001 — never block a push on config parsing
            log.warning("sheets.strategy_scope_unresolved", error=str(exc)[:200])
        return cls(cfg, strategy=strategy, **kwargs)

    @property
    def strategy(self) -> str | None:
        return self._strategy

    def tab(self, base_tab: str) -> str:
        """The worksheet title `base_tab` resolves to for this client's strategy."""
        return tab_for(base_tab, self._strategy)

    # ── Connection ────────────────────────────────────────────────────────────

    def _get_spreadsheet(self) -> Any:
        if self.__spreadsheet is not None:
            return self.__spreadsheet
        import gspread
        gc = gspread.service_account(filename=self._cfg.google_service_account_json)
        self.__spreadsheet = gc.open_by_key(self._cfg.kalshi_sheet_id)
        return self.__spreadsheet

    # ── Retry wrapper ─────────────────────────────────────────────────────────

    def _with_backoff(self, fn: Any, *args: Any, retries: int = 4, **kwargs: Any) -> Any:
        delay = 1.0
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if attempt < retries - 1 and _is_rate_limit(exc):
                    log.warning("sheets.rate_limit", attempt=attempt, delay=delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    # ── Worksheet management ──────────────────────────────────────────────────

    def _get_or_create_worksheet(self, title: str, rows: int = 2000, cols: int = 26) -> Any:
        """
        Fetch `title`, creating it if absent. Tolerates the tab already
        existing, including when it appears between the lookup and the create.

        Two deployments share this spreadsheet and each runs its own evaluate
        on its own timer, so that race is real rather than theoretical. The
        Sheets API rejects add_worksheet for a duplicate title; previously that
        error propagated into the caller's blanket except and the whole push
        was dropped with nothing but a log line to show for it. Re-fetching on
        create failure turns the race into a no-op.
        """
        import gspread
        spreadsheet = self._get_spreadsheet()
        try:
            return spreadsheet.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            pass

        log.info("sheets.creating_tab", title=title)
        try:
            return self._with_backoff(
                spreadsheet.add_worksheet, title=title, rows=rows, cols=cols
            )
        except Exception as exc:  # noqa: BLE001
            # Someone else (the other deployment) may have just created it.
            try:
                ws = spreadsheet.worksheet(title)
                log.info("sheets.tab_created_concurrently", title=title)
                return ws
            except Exception:  # noqa: BLE001
                raise exc

    def _ensure_headers(self, ws: Any, headers: list[str]) -> None:
        """Write the header row only if the sheet is empty or headers differ."""
        existing = self._with_backoff(ws.row_values, 1)
        if existing != headers:
            self._with_backoff(ws.update, "A1", [headers])

    def _append_rows_batched(
        self,
        ws: Any,
        rows: list[list],
        batch_size: int = _SYNC_BATCH_SIZE,
        batch_sleep: float = _SYNC_BATCH_SLEEP,
    ) -> int:
        """
        Write rows to ws in batches using append_rows() (plural).

        One append_rows() call per batch costs one Sheets API quota unit
        instead of N units, reducing API usage by ~50x.  Sleeps between
        batches to stay well under the 60-writes/minute rate limit.

        Returns the number of rows written.
        """
        if not rows:
            return 0
        written = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            self._with_backoff(ws.append_rows, batch, value_input_option="USER_ENTERED")
            written += len(batch)
            if i + batch_size < len(rows):   # no sleep after the final batch
                time.sleep(batch_sleep)
        return written

    # ── Public API ────────────────────────────────────────────────────────────

    def ensure_tabs(self) -> None:
        """
        Create any missing worksheets and write their header rows.

        Idempotent: safe to call on every startup.  Only touches the sheet
        if a tab is missing or its header row doesn't match the canonical list.

        Per-strategy tabs are created under this client's scoped titles (see
        tab_for). The Live P&L tab is deliberately NOT created here -- it is
        created on the first real-money trade that lands in it, so the tab's
        existence means real trades exist rather than "a service started once".
        """
        try:
            created = []
            for base_title in _ENSURE_TABS:
                title = self.tab(base_title)
                ws = self._get_or_create_worksheet(title)
                self._ensure_headers(ws, _HEADERS[base_title])
                created.append(title)
            log.info("sheets.ensure_tabs.ok", tabs=created, strategy=self._strategy)
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.ensure_tabs.failed", error=str(exc)[:300])

    def append_trade(self, trade: dict[str, Any]) -> None:
        """Append one row to Trade Log.  Best-effort — never raises."""
        try:
            ws = self._get_or_create_worksheet(TAB_TRADE_LOG)
            row = [trade.get(col, "") for col in _HEADERS[TAB_TRADE_LOG]]
            self._with_backoff(ws.append_row, row, value_input_option="USER_ENTERED")
            log.debug("sheets.trade_logged", ticker=trade.get("ticker"))
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.append_trade.failed", error=str(exc)[:300])

    def append_skip(self, skip: dict[str, Any]) -> None:
        """Append one row to Skipped Signals.  Best-effort — never raises."""
        try:
            ws = self._get_or_create_worksheet(TAB_SKIPPED)
            row = [skip.get(col, "") for col in _HEADERS[TAB_SKIPPED]]
            self._with_backoff(ws.append_row, row, value_input_option="USER_ENTERED")
            log.debug("sheets.skip_logged", ticker=skip.get("ticker"))
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.append_skip.failed", error=str(exc)[:300])

    def upsert_daily_summary(self, day: date, row_data: dict[str, Any]) -> None:
        """
        Write or update the Daily Summary row for `day`.

        Searches for an existing row whose first cell matches day.isoformat();
        updates it in place if found, appends otherwise.
        """
        try:
            ws = self._get_or_create_worksheet(TAB_DAILY_SUMMARY)
            headers  = _HEADERS[TAB_DAILY_SUMMARY]
            date_str = day.isoformat()
            row_values = [row_data.get(col, "") for col in headers]

            all_values = self._with_backoff(ws.get_all_values)
            for i, existing_row in enumerate(all_values[1:], start=2):  # skip header
                if existing_row and existing_row[0] == date_str:
                    self._with_backoff(ws.update, f"A{i}", [row_values])
                    log.debug("sheets.daily_summary.updated", date=date_str)
                    return

            self._with_backoff(ws.append_row, row_values, value_input_option="USER_ENTERED")
            log.debug("sheets.daily_summary.appended", date=date_str)
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.upsert_daily_summary.failed", error=str(exc)[:300])

    def push_calibration(self, buckets: list[dict[str, Any]]) -> None:
        """
        Overwrite the Calibration tab with `buckets`.

        Each bucket dict must have keys matching _HEADERS[TAB_CALIBRATION].
        Clears and rewrites on every call — calibration is rebuilt from scratch
        by the learning module.
        """
        try:
            ws = self._get_or_create_worksheet(self.tab(TAB_CALIBRATION))
            headers = _HEADERS[TAB_CALIBRATION]
            data_rows = [[b.get(col, "") for col in headers] for b in buckets]
            self._with_backoff(ws.clear)
            self._with_backoff(ws.update, "A1", [headers] + data_rows)
            log.info("sheets.calibration_pushed", buckets=len(buckets), strategy=self._strategy)
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.push_calibration.failed", error=str(exc)[:300])

    def push_performance(self, report: dict[str, Any]) -> None:
        """
        Overwrite the Performance tab with a single summary row from `report`
        (a kbot.learning.evaluate.performance_report result).

        Same clear-and-rewrite shape as push_calibration: the report is
        recomputed from the full outcome history on every evaluate, so the tab
        is a current-state view, not a log -- which also makes re-running
        evaluate inherently non-duplicating here.
        """
        try:
            ws = self._get_or_create_worksheet(self.tab(TAB_PERFORMANCE))
            headers = _HEADERS[TAB_PERFORMANCE]
            row = {
                "updated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
                **{k: ("" if report.get(k) is None else report.get(k)) for k in headers[1:]},
            }
            data_row = [row.get(col, "") for col in headers]
            self._with_backoff(ws.clear)
            self._with_backoff(ws.update, "A1", [headers, data_row])
            log.info(
                "sheets.performance_pushed",
                strategy=self._strategy,
                total_trades=report.get("total_trades"),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.push_performance.failed", error=str(exc)[:300])

    # ── Live (real-money) P&L ────────────────────────────────────────────────

    def upsert_live_pnl(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """
        Idempotently sync real-money trades into this strategy's Live P&L tab.

        `rows` is the COMPLETE set of real-money trades known to the events DB,
        in settlement order, each already carrying its running
        cumulative_realized_pnl (see kbot.live_pnl.build_live_pnl_rows). Rows
        must already be filtered to real money -- this method writes whatever
        it is handed; kbot.live_pnl owns the is_live filter.

        Keyed on (ticker, side), the same identity kbot.outcomes uses to dedupe
        outcomes and the primary key of the positions table. A trade appears at
        most once per settled market, so re-running evaluate any number of
        times converges: existing rows are matched and left alone, only genuinely
        new trades are appended. Nothing is ever blind-appended, so the
        cumulative column cannot be inflated by re-running.

        kalshi_balance is stamped only on rows being appended for the first
        time. It is a point-in-time observation that cannot be reconstructed
        later, so an existing row's recorded balance is preserved verbatim --
        a full rewrite would destroy exactly the history this column exists to
        provide.

        Returns {"appended": n, "updated": n, "unchanged": n} for the caller to
        log. Best-effort: never raises.
        """
        result = {"appended": 0, "updated": 0, "unchanged": 0}
        if not rows:
            return result
        try:
            headers = _HEADERS[TAB_LIVE_PNL]
            ws = self._get_or_create_worksheet(self.tab(TAB_LIVE_PNL))
            existing = self._with_backoff(ws.get_all_values) or []

            if not existing or existing[0] != headers:
                self._with_backoff(ws.update, "A1", [headers])
                existing = [headers]

            # (ticker, side) -> (1-based sheet row index, row values)
            i_ticker, i_side = headers.index("ticker"), headers.index("side")
            index: dict[tuple[str, str], tuple[int, list]] = {}
            for offset, existing_row in enumerate(existing[1:], start=2):
                if len(existing_row) > max(i_ticker, i_side):
                    key = (existing_row[i_ticker], existing_row[i_side])
                    index.setdefault(key, (offset, existing_row))

            to_append: list[list] = []
            for row in rows:
                key = (str(row.get("ticker", "")), str(row.get("side", "")))
                found = index.get(key)
                if found is None:
                    to_append.append([row.get(col, "") for col in headers])
                    continue

                row_idx, old_values = found
                merged = self._merge_live_row(row, old_values, headers)
                if merged is None:
                    result["unchanged"] += 1
                    continue
                self._with_backoff(ws.update, f"A{row_idx}", [merged])
                result["updated"] += 1

            if to_append:
                result["appended"] = self._append_rows_batched(ws, to_append)

            log.info("sheets.live_pnl_upserted", strategy=self._strategy, **result)
            return result
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.upsert_live_pnl.failed", error=str(exc)[:300])
            return result

    @staticmethod
    def _merge_live_row(row: dict[str, Any], old_values: list, headers: list[str]) -> list | None:
        """
        The updated cell values for an already-present trade, or None if the
        sheet row already agrees with the computed one (the overwhelmingly
        common case -- nothing to write).

        The recorded kalshi_balance is carried over from the sheet rather than
        recomputed, and balance_minus_cumulative is re-derived from THAT
        preserved balance, so the drift column stays consistent with the
        balance actually observed when the trade was first logged.
        """
        old = {col: (old_values[i] if i < len(old_values) else "")
               for i, col in enumerate(headers)}

        preserved_balance = old.get("kalshi_balance", "")
        merged = dict(row)
        merged["kalshi_balance"] = preserved_balance
        merged["balance_minus_cumulative"] = _drift(
            preserved_balance, merged.get("cumulative_realized_pnl")
        )

        new_values = [merged.get(col, "") for col in headers]
        if all(_cells_equal(old.get(col, ""), value)
               for col, value in zip(headers, new_values)):
            return None
        return new_values

    def push_markout_summary(self, rows: list[dict[str, Any]]) -> None:
        """
        Overwrite the Markout tab with one row per strategy.

        Each row dict should have keys matching _HEADERS[TAB_MARKOUT]. Clears
        and rewrites on every call, mirroring push_calibration(). Best-effort
        — exceptions are logged and absorbed.
        """
        try:
            ws = self._get_or_create_worksheet(TAB_MARKOUT)
            headers = _HEADERS[TAB_MARKOUT]
            data_rows = [[r.get(col, "") for col in headers] for r in rows]
            self._with_backoff(ws.clear)
            self._with_backoff(ws.update, "A1", [headers] + data_rows)
            log.info("sheets.markout_pushed", strategies=len(rows))
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.push_markout.failed", error=str(exc)[:300])

    def append_arb_log(self, row: dict[str, Any]) -> None:
        """
        Append one row to Arb Log (see kbot.scanners.consistency).

        Best-effort AND self-limiting: never raises, and never keeps trying a
        write that Google has already refused. The first APIError -- or an Arb
        Log tab already past _ARB_LOG_MAX_CELLS -- latches arb-log writes off
        for the rest of the process after a single warning; see the
        _ARB_LOG_DISABLED note for why retrying here is actively harmful.

        Only this tab is affected. Calibration, Performance and Live P&L are
        written from timer/evaluate paths, not from the WS loop, and keep
        their existing per-call error handling.
        """
        if _ARB_LOG_DISABLED:
            return
        try:
            ws = self._get_or_create_worksheet(TAB_ARB_LOG)

            cells = _grid_cells(ws)
            if cells is not None and cells >= _ARB_LOG_MAX_CELLS:
                _disable_arb_log(
                    "tab_near_cell_cap",
                    tab=TAB_ARB_LOG, cells=cells, max_cells=_ARB_LOG_MAX_CELLS,
                )
                return

            row_values = [row.get(col, "") for col in _HEADERS[TAB_ARB_LOG]]
            self._with_backoff(ws.append_row, row_values, value_input_option="USER_ENTERED")
            log.debug("sheets.arb_logged", check_type=row.get("check_type"))
        except Exception as exc:  # noqa: BLE001
            if _is_api_error(exc):
                # Google answered and refused. Whatever the code, repeating the
                # call once per book update is not how it gets fixed.
                _disable_arb_log(
                    "api_error", code=getattr(exc, "code", None), error=str(exc)[:300],
                )
                return
            # A transport/local failure may well succeed next time: log it and
            # leave the writer armed, exactly as before.
            log.error("sheets.append_arb_log.failed", error=str(exc)[:300])

    def append_research(self, digest: dict[str, Any]) -> None:
        """Append one research digest row to Research Log.  Best-effort — never raises."""
        try:
            ws = self._get_or_create_worksheet(TAB_RESEARCH)
            headers = _HEADERS[TAB_RESEARCH]
            # Serialize list/dict fields to JSON strings for readability in Sheets.
            row_data = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                        for k, v in digest.items()}
            row = [row_data.get(col, "") for col in headers]
            self._with_backoff(ws.append_row, row, value_input_option="USER_ENTERED")
            log.debug("sheets.research_logged", topic=digest.get("topic"))
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.append_research.failed", error=str(exc)[:300])

    # ── Full re-sync ──────────────────────────────────────────────────────────

    def sync_all(self, telemetry: Any) -> None:
        """
        Full re-sync from SQLite → Sheet.

        Ensures all tabs exist, then backfills:
          - signal / order / position_open / position_closed  → Trade Log
          - near-miss skip events (net_edge > -0.02)          → Skipped Signals
          - fill / order events grouped by UTC date           → Daily Summary

        evaluation and heartbeat events are intentionally excluded: they are
        internal telemetry only and would flood the Sheets API quota.

        Rows are written in batches of _SYNC_BATCH_SIZE (50) with a
        _SYNC_BATCH_SLEEP (1 s) pause between batches, keeping API usage
        well under the 60-writes/minute limit.

        Note: calling this multiple times appends duplicate Trade Log rows.
        Clear the Trade Log tab first if a clean rebuild is needed.
        """
        self.ensure_tabs()

        # -- Trade Log: signal / order / position_open / position_closed ------
        try:
            trade_evs = telemetry.recent_types(_SYNC_EVENT_TYPES, limit=10_000)
            trade_evs.sort(key=lambda e: e["ts"])   # oldest first (journal order)
            headers   = _HEADERS[TAB_TRADE_LOG]
            trade_rows = [
                [_event_to_trade_row(ev).get(col, "") for col in headers]
                for ev in trade_evs
            ]
            if trade_rows:
                ws = self._get_or_create_worksheet(TAB_TRADE_LOG)
                n  = self._append_rows_batched(ws, trade_rows)
                log.info("sheets.sync_all.trades", rows=n)
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.sync_all.trades_failed", error=str(exc)[:300])

        # -- Skipped Signals: near-misses only (net_edge > -0.02) -------------
        try:
            skip_evs = telemetry.recent(event_type="skip", limit=10_000)
            skip_evs.sort(key=lambda e: e["ts"])
            headers   = _HEADERS[TAB_SKIPPED]
            skip_rows = []
            for ev in skip_evs:
                net_edge = ev.get("payload", {}).get("net_edge")
                if isinstance(net_edge, (int, float)) and net_edge <= _SKIP_NEAR_MISS_THRESHOLD:
                    continue   # far-OTM: not meaningful in the journal
                skip_rows.append(
                    [_event_to_skip_row(ev).get(col, "") for col in headers]
                )
            if skip_rows:
                ws = self._get_or_create_worksheet(TAB_SKIPPED)
                n  = self._append_rows_batched(ws, skip_rows)
                log.info("sheets.sync_all.skips", rows=n)
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.sync_all.skips_failed", error=str(exc)[:300])

        # -- Daily Summary: group fill + order events by UTC date -------------
        try:
            all_trade_evs = telemetry.recent_types(("fill", "order"), limit=10_000)
            by_date: dict[str, list] = {}
            for ev in all_trade_evs:
                d = datetime.fromtimestamp(ev["ts"], tz=timezone.utc).date().isoformat()
                by_date.setdefault(d, []).append(ev)

            ks_events = telemetry.recent(event_type="kill_switch", limit=100)
            ks_dates  = {
                datetime.fromtimestamp(e["ts"], tz=timezone.utc).date().isoformat()
                for e in ks_events
            }

            for date_str, evs in sorted(by_date.items()):
                summary = {
                    "date":                date_str,
                    "num_trades":          len(evs),
                    "wins":                "",
                    "losses":              "",
                    "win_rate":            "",
                    "gross_pnl":           "",
                    "fees":                "",
                    "net_pnl":             "",
                    "fee_drag_pct":        "",
                    "end_bankroll":        "",
                    "kill_switch_tripped": date_str in ks_dates,
                }
                self.upsert_daily_summary(date.fromisoformat(date_str), summary)
        except Exception as exc:  # noqa: BLE001
            log.error("sheets.sync_all.summary_failed", error=str(exc)[:300])

        log.info("sheets.sync_all.done")
