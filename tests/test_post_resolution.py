"""
Tests for kbot.scanners.post_resolution.PostResolutionScanner (MARKET_RESEARCH.md
item 3 / Source 5): settlement detection, window open/close, discounted
edge-gate sizing, and books-dict sharing.
"""

from __future__ import annotations

import time

import pytest

from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import OrderAck
from kbot.models.types import OrderBook, PriceLevel, Side, SizingResult
from kbot.scanners.post_resolution import PostResolutionScanner
from kbot.strategy.base import Strategy
from kbot.telemetry import Telemetry


def _cfg(**kwargs) -> Settings:
    return Settings(
        kalshi_key_id="test",
        dry_run=True,
        min_net_edge=kwargs.get("min_net_edge", 0.10),
        post_resolution_window_minutes=kwargs.get("post_resolution_window_minutes", 30.0),
        post_resolution_edge_discount=kwargs.get("post_resolution_edge_discount", 0.30),
    )


def _book(ticker: str, yes_ask: float = 0.40, no_ask: float = 0.58) -> OrderBook:
    return OrderBook(
        ticker=ticker,
        yes=[PriceLevel(price=yes_ask, quantity_units=10000)],
        no=[PriceLevel(price=no_ask, quantity_units=10000)],
    )


def _signal(ticker: str, net_edge: float, contracts: int) -> SizingResult:
    return SizingResult(
        ticker=ticker, side=Side.YES, contracts=contracts, price_dollars=0.40,
        fair_p=0.40 + net_edge, net_edge_dollars=net_edge, kelly_fraction_used=0.1,
    )


class _FakeStrategy(Strategy):
    """
    Controllable Strategy stub. evaluate_fn receives (book, cfg) so tests
    can inspect exactly which cfg.min_net_edge the scanner swapped in, and
    decide contracts based on it (mirroring how a real strategy's own
    internal edge gate would behave).
    """

    def __init__(self, name, series_tickers, cfg, expiry_map=None, evaluate_fn=None):
        self._name = name
        self.series_tickers = list(series_tickers)
        self.required_feeds: list[str] = []
        self._cfg = cfg
        self._expiry_map = dict(expiry_map or {})
        self._evaluate_fn = evaluate_fn
        self.evaluate_calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def expiry_ts(self, ticker: str) -> float | None:
        return self._expiry_map.get(ticker)

    def evaluate(self, book, *, btc_price=None, btc_vol=None, bankroll, current_time=None):
        self.evaluate_calls.append({"ticker": book.ticker, "min_net_edge": self.cfg.min_net_edge, "bankroll": bankroll})
        if self._evaluate_fn is not None:
            return self._evaluate_fn(book, self.cfg)
        return []


@pytest.fixture()
def mock_client(mocker):
    client = mocker.MagicMock()
    # fill_count echoes the request -- an immediate_or_cancel ack reporting 0
    # fills means nothing traded, and the executor then opens no position.
    client.place_order.side_effect = (
        lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
            client_order_id="cid-1", order_id="oid-1", dry_run=True,
            fill_count_units=count * 100, remaining_count_units=0, average_fill_price=price_dollars,
        )
    )
    return client


@pytest.fixture()
def telemetry(tmp_path):
    tel = Telemetry(db_path=tmp_path / "events.db")
    yield tel
    tel.close()


@pytest.fixture()
def executor(mock_client, telemetry):
    return Executor(mock_client, _cfg(), telemetry=telemetry)


def _scanner(books, strategies, executor, telemetry, cfg, allocations=None):
    allocations = allocations or {name: 1.0 for name in strategies}
    return PostResolutionScanner(
        books, strategies, executor, telemetry,
        bankroll_provider=lambda name: 1000.0 * allocations[name],
        cfg=cfg,
    )


# ── Settlement detection ──────────────────────────────────────────────────────

def test_detects_settlement_when_expiry_has_passed(executor, telemetry):
    cfg = _cfg()
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    assert "SERIESA" in scanner.active_windows
    assert scanner.active_windows["SERIESA"].settled_ticker == "SERIESA-1"


def test_does_not_detect_settlement_before_expiry(executor, telemetry):
    cfg = _cfg()
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now + 600})
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    assert scanner.active_windows == {}


def test_ticker_with_unknown_expiry_is_ignored(executor, telemetry):
    cfg = _cfg()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={})  # no expiry_ts entry -> None
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    assert scanner.active_windows == {}


def test_settlement_only_triggers_once(executor, telemetry):
    cfg = _cfg()
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()
    opened_at_first = scanner.active_windows["SERIESA"].opened_at
    scanner.tick()  # a second tick must not re-detect / re-open the SAME settlement
    assert scanner.active_windows["SERIESA"].opened_at == opened_at_first


def test_ticker_not_owned_by_any_strategy_is_ignored(executor, telemetry):
    cfg = _cfg()
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"OTHER-1": _book("OTHER-1")}  # not owned by "a"
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    assert scanner.active_windows == {}


# ── Window open/close ─────────────────────────────────────────────────────────

def test_window_stays_open_within_duration(executor, telemetry, mocker):
    cfg = _cfg(post_resolution_window_minutes=30.0)
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)
    scanner.tick()

    mocker.patch("time.time", return_value=now + 20 * 60)  # 20 min later -- still within 30
    scanner.tick()

    assert "SERIESA" in scanner.active_windows


def test_window_closes_after_duration_elapses(executor, telemetry, mocker):
    cfg = _cfg(post_resolution_window_minutes=30.0)
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)
    scanner.tick()
    assert "SERIESA" in scanner.active_windows

    mocker.patch("time.time", return_value=now + 31 * 60)  # 31 min later -- past the 30-min window
    scanner.tick()

    assert "SERIESA" not in scanner.active_windows


def test_window_close_logs_summary_counts(executor, telemetry, mocker):
    cfg = _cfg(min_net_edge=0.10, post_resolution_window_minutes=30.0, post_resolution_edge_discount=0.30)
    now = time.time()

    def evaluate_fn(book, strat_cfg):
        if book.ticker == "SERIESA-1":
            return []  # the settled ticker itself
        # net_edge=0.08 clears the discounted bar (0.07) but not the normal bar (0.10)
        contracts = 10 if strat_cfg.min_net_edge <= 0.08 else 0
        return [_signal(book.ticker, net_edge=0.08, contracts=contracts)]

    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10}, evaluate_fn=evaluate_fn)
    books = {"SERIESA-1": _book("SERIESA-1"), "SERIESA-2": _book("SERIESA-2")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)
    scanner.tick()  # opens window, evaluates SERIESA-2 -> signal fires

    window = scanner.active_windows["SERIESA"]
    assert window.signals_fired == 1
    assert window.cleared_discounted == 1
    assert window.cleared_normal == 0  # 0.08 < 0.10 normal bar

    mocker.patch("time.time", return_value=now + 31 * 60)
    scanner.tick()

    events = telemetry.recent("post_resolution_window_closed")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["settled_ticker"] == "SERIESA-1"
    assert payload["signals_fired"] == 1
    assert payload["cleared_discounted"] == 1
    assert payload["cleared_normal"] == 0


def test_new_settlement_in_same_series_restarts_window(executor, telemetry, mocker):
    cfg = _cfg(post_resolution_window_minutes=30.0)
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)
    scanner.tick()
    first_opened_at = scanner.active_windows["SERIESA"].opened_at

    later = now + 10 * 60
    mocker.patch("time.time", return_value=later)
    strat._expiry_map["SERIESA-2"] = later - 5
    books["SERIESA-2"] = _book("SERIESA-2")
    scanner.tick()

    window = scanner.active_windows["SERIESA"]
    assert window.settled_ticker == "SERIESA-2"
    assert window.opened_at > first_opened_at


# ── Discounted edge gate during an active window ─────────────────────────────

def test_edge_discount_lets_below_normal_signal_through(executor, telemetry, mock_client):
    cfg = _cfg(min_net_edge=0.10, post_resolution_edge_discount=0.30)  # discounted bar = 0.07
    now = time.time()

    def evaluate_fn(book, strat_cfg):
        if book.ticker == "SERIESA-1":
            return []
        contracts = 10 if strat_cfg.min_net_edge <= 0.08 else 0
        return [_signal(book.ticker, net_edge=0.08, contracts=contracts)]

    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10}, evaluate_fn=evaluate_fn)
    books = {"SERIESA-1": _book("SERIESA-1"), "SERIESA-2": _book("SERIESA-2")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    mock_client.place_order.assert_called_once()
    window = scanner.active_windows["SERIESA"]
    assert window.signals_fired == 1


def test_evaluate_uses_discounted_cfg_and_restores_original(executor, telemetry):
    cfg = _cfg(min_net_edge=0.10, post_resolution_edge_discount=0.30)
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books = {"SERIESA-1": _book("SERIESA-1"), "SERIESA-2": _book("SERIESA-2")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    # The strategy's own cfg must be back to normal after the tick.
    assert strat.cfg.min_net_edge == pytest.approx(0.10)
    # But the evaluate() call for SERIESA-2 must have seen the discounted value.
    calls_for_2 = [c for c in strat.evaluate_calls if c["ticker"] == "SERIESA-2"]
    assert len(calls_for_2) == 1
    assert calls_for_2[0]["min_net_edge"] == pytest.approx(0.07)


def test_no_evaluation_outside_active_window(executor, telemetry):
    cfg = _cfg()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={})  # never settles
    books = {"SERIESA-1": _book("SERIESA-1"), "SERIESA-2": _book("SERIESA-2")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    assert strat.evaluate_calls == []  # no active window -> no discounted evaluation at all


def test_normal_gate_restored_after_window_expires(executor, telemetry, mocker, mock_client):
    cfg = _cfg(min_net_edge=0.10, post_resolution_window_minutes=30.0, post_resolution_edge_discount=0.30)
    now = time.time()

    def evaluate_fn(book, strat_cfg):
        if book.ticker == "SERIESA-1":
            return []
        contracts = 10 if strat_cfg.min_net_edge <= 0.08 else 0
        return [_signal(book.ticker, net_edge=0.08, contracts=contracts)]

    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10}, evaluate_fn=evaluate_fn)
    books = {"SERIESA-1": _book("SERIESA-1"), "SERIESA-2": _book("SERIESA-2")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()
    assert mock_client.place_order.call_count == 1  # fired once during the window

    mocker.patch("time.time", return_value=now + 31 * 60)  # window closes
    scanner.tick()  # no active window left -> SERIESA-2 not re-evaluated by the scanner at all

    assert mock_client.place_order.call_count == 1  # unchanged -- no further post-resolution firing
    assert scanner.active_windows == {}


def test_settled_ticker_itself_is_never_traded(executor, telemetry, mock_client):
    cfg = _cfg(min_net_edge=0.10, post_resolution_edge_discount=0.30)
    now = time.time()

    def evaluate_fn(book, strat_cfg):
        # Always signals, regardless of ticker -- if the settled ticker
        # itself gets evaluated, this would incorrectly fire on it too.
        return [_signal(book.ticker, net_edge=0.08, contracts=10)]

    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10}, evaluate_fn=evaluate_fn)
    books = {"SERIESA-1": _book("SERIESA-1")}  # ONLY the settled ticker is tracked
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()

    mock_client.place_order.assert_not_called()
    assert scanner.active_windows["SERIESA"].signals_fired == 0


# ── Books sharing (read-only, no copying) ────────────────────────────────────

def test_scanner_shares_books_reference_not_a_copy(executor, telemetry):
    cfg = _cfg()
    strat = _FakeStrategy("a", ["SERIESA"], cfg)
    books = {"SERIESA-1": _book("SERIESA-1")}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    assert scanner._books is books  # same object, not copied


def test_scanner_observes_books_mutated_externally(executor, telemetry):
    cfg = _cfg()
    now = time.time()
    strat = _FakeStrategy("a", ["SERIESA"], cfg, expiry_map={"SERIESA-1": now - 10})
    books: dict[str, OrderBook] = {}
    scanner = _scanner(books, {"a": strat}, executor, telemetry, cfg)

    scanner.tick()
    assert scanner.active_windows == {}  # ticker doesn't exist in books yet

    books["SERIESA-1"] = _book("SERIESA-1")  # external mutation, e.g. the runner's WS handler
    scanner.tick()

    assert "SERIESA" in scanner.active_windows  # scanner sees the update without being told directly
