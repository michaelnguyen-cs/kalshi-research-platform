"""
The ticker size-only emission probe: does Kalshi's `ticker` channel emit an
update when the DISPLAYED SIZE AT THE TOUCH changes and the touch PRICES do not?

This module exists to answer exactly one protocol question, before any recorder
is built on the answer. Kalshi documents that "updates [are] sent whenever any
ticker field changes" and documents `yes_bid_size_fp` / `yes_ask_size_fp` as
ticker fields. That is documentation. A future liquidity gate would depend on
fresh displayed size at the touch, so the behaviour is established here from the
live feed instead.

Why the existing ~3.6 GB Thread C capacity archive cannot answer it: that
archive carries `orderbook_delta` frames only. With no simultaneously observed
ticker stream there is nothing to match a book event against, and "no ticker
message in the archive" would be a property of the subscription rather than of
the exchange.

So: one connection, both channels, the same markets, the same clock.

    orderbook_delta  ->  reconstructed canonical book  ->  top of book per event
    ticker           ->  the displayed fields, message by message

An OPPORTUNITY is a reconstructed top-of-book transition in which

    * the best bid price is unchanged, AND
    * the best ask price is unchanged, AND
    * the displayed size at one or both of those unchanged best prices changed.

An opportunity counts as EVIDENCE only if it is unambiguous, and the three ways
it can be ambiguous are each counted rather than quietly folded in:

  adjacent book event   another top-of-book change for the same market falls in
                        the matching window, so a ticker message there could
                        have been triggered by that instead.
  no prior ticker       the market's first ticker message in the probe has
                        nothing to diff against, so "no other field changed"
                        cannot be established.
  other field changed   a ticker message arrived carrying the new size, but a
                        touch price, last price, volume, open interest, dollar
                        volume, dollar open interest or last trade size ALSO
                        changed against the previous ticker for that market. Any
                        of those could independently have triggered the message,
                        so it is NOT counted as proof of size-only triggering.

Resolution is STREAMING, not buffered. A probe over the whole frozen universe
sees a large multiple of the events a buffered design could hold, and a probe
that dies of its own memory produces no answer at all. Each opportunity is held
open only until its matching window closes, and each market keeps only the
handful of recent observations the window can still reach.

The verdict thresholds and the minimum evidence count are constants here, fixed
before the probe was run, so the reading of the result is not chosen after
seeing it.

Nothing in this module reports a market identity, a price, an outcome, or any
research statistic. It reports counts of protocol events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from depthrec.book import BookError, to_price, to_qty

# -- Frozen probe parameters --------------------------------------------------

MATCH_WINDOW_PRE_SECONDS = 0.25
"""How far BEFORE a book event a ticker message may be and still be attributed
to it. Small but non-zero: the two channels are multiplexed onto one socket and
the exchange may emit the ticker first."""

MATCH_WINDOW_POST_SECONDS = 2.0
"""How far AFTER a book event a ticker message may be and still be attributed to
it. Generous relative to any plausible fan-out delay, because a window that is
too tight would manufacture "no ticker update" results -- the exact direction of
error that would falsely condemn the channel."""

LAG_GUARD_SECONDS = 0.5
"""How far this consumer may fall behind its own baseline before receive-time
matching stops being trustworthy.

Matching two channels by RECEIVE time is sound only while the consumer keeps up.
Once it is draining a backlog, the order and spacing of receive timestamps stop
tracking the order and spacing of the exchange's own emissions, and a matching
window covers whatever happened to be dequeued rather than what happened at that
moment. Opportunities whose window overlaps a backlog are therefore counted as
AMBIGUOUS rather than as evidence in either direction."""

WARMUP_SECONDS = 120.0
"""The subscription warm-up, treated as one long backlog interval.

Subscribing to the whole frozen universe delivers an `orderbook_snapshot` for
every market at once. That burst is real traffic, but a consumer draining it is
by definition behind, so no opportunity inside the warm-up can be attributed."""

MIN_UNAMBIGUOUS_OPPORTUNITIES = 100
"""Below this the probe reports INSUFFICIENT_PROTOCOL_EVIDENCE and refuses to
infer the semantic from the documentation."""

CONFIRM_RATE = 0.99
REFUTE_RATE = 0.95
"""Match rate >= CONFIRM_RATE confirms; <= REFUTE_RATE refutes; between them is
INDETERMINATE, which is a stop, not a pass."""

VERDICT_CONFIRMED = "TICKER_REFLECTS_SIZE_ONLY_TOUCH_CHANGES"
VERDICT_REFUTED = "TICKER_DOES_NOT_RELIABLY_REFLECT_SIZE_ONLY_TOUCH_CHANGES"
VERDICT_INDETERMINATE = "INDETERMINATE_PROTOCOL_EVIDENCE"
VERDICT_INSUFFICIENT = "INSUFFICIENT_PROTOCOL_EVIDENCE"

TICKER_CHANNEL = "ticker"
ORDERBOOK_CHANNEL = "orderbook_delta"
PROBE_CHANNELS = (ORDERBOOK_CHANNEL, TICKER_CHANNEL)

TICKER_PRICE_FIELDS = ("yes_bid_dollars", "yes_ask_dollars")
"""The touch PRICE fields. A change in either makes a ticker message
price-triggered and therefore useless as size-only evidence."""

TICKER_SIZE_FIELDS = {"bid": "yes_bid_size_fp", "ask": "yes_ask_size_fp"}

TICKER_OTHER_TRIGGER_FIELDS = (
    "price_dollars",
    "volume_fp",
    "open_interest_fp",
    "dollar_volume",
    "dollar_open_interest",
    "last_trade_size_fp",
)
"""Every other documented ticker field that could independently have triggered a
message. Deliberately the full documented set rather than a chosen subset: an
omitted field is an unexamined alternative explanation."""

TICKER_DIFF_FIELDS = TICKER_PRICE_FIELDS + TICKER_OTHER_TRIGGER_FIELDS

SIDES = ("bid", "ask")


# -- Observations -------------------------------------------------------------


@dataclass(frozen=True)
class Top:
    """A canonical top of book. `None` on a side means that side is empty."""

    bid_price: int | None
    bid_qty: int | None
    ask_price: int | None
    ask_qty: int | None

    def price_of(self, side: str) -> int | None:
        return self.bid_price if side == "bid" else self.ask_price

    def qty_of(self, side: str) -> int | None:
        return self.bid_qty if side == "bid" else self.ask_qty

    def price_key(self) -> tuple[int | None, int | None]:
        return (self.bid_price, self.ask_price)


def size_changed_sides(previous: Top, current: Top) -> list[str]:
    """
    Sides whose best price is present and unchanged but whose displayed size at
    that price changed.
    """
    out = []
    for side in SIDES:
        before, after = previous.price_of(side), current.price_of(side)
        if before is None or after is None or before != after:
            continue
        if previous.qty_of(side) != current.qty_of(side):
            out.append(side)
    return out


def is_opportunity(previous: Top, current: Top) -> bool:
    """A top-of-book transition that changes only displayed size at the touch."""
    return previous.price_key() == current.price_key() and bool(
        size_changed_sides(previous, current)
    )


TICKER_CLEAN = "clean"
TICKER_NO_PRIOR = "no_prior"
TICKER_OTHER_CHANGED = "other_changed"


@dataclass(frozen=True)
class TickerObservation:
    """
    One ticker message, reduced at arrival to what matching needs.

    The field diff against the market's PREVIOUS ticker message is computed the
    moment the message arrives and kept as `kind`, so the full previous payload
    never has to be retained per market and pruning can never lose the
    comparison an opportunity later depends on.
    """

    recv_ts: float
    bid_size: int | None
    ask_size: int | None
    kind: str

    def size_of(self, side: str) -> int | None:
        return self.bid_size if side == "bid" else self.ask_size


def reduce_ticker_message(
    msg: dict[str, Any], previous_fields: dict[str, Any] | None, recv_ts: float
) -> tuple[TickerObservation, dict[str, Any]]:
    """
    Reduce one raw ticker body to an observation plus the fields to diff the
    NEXT message against.

    An unparseable size field becomes None rather than raising: a message whose
    size cannot be read is a message that cannot evidence a size change, and
    that is a matching outcome, not a probe failure.
    """

    def size(side: str) -> int | None:
        raw = msg.get(TICKER_SIZE_FIELDS[side])
        if raw is None:
            return None
        try:
            return to_qty(raw)
        except BookError:
            return None

    kept = {f: msg.get(f) for f in TICKER_DIFF_FIELDS}
    if previous_fields is None:
        kind = TICKER_NO_PRIOR
    elif any(kept[f] != previous_fields.get(f) for f in TICKER_DIFF_FIELDS):
        kind = TICKER_OTHER_CHANGED
    else:
        kind = TICKER_CLEAN
    return TickerObservation(recv_ts, size("bid"), size("ask"), kind), kept


class LagHistogram:
    """
    The distribution of (receive time - exchange ts_ms), in fixed buckets.

    Matching two channels against each other by RECEIVE time is only sound while
    the consumer keeps up. If this process ever falls behind, receive timestamps
    stop tracking exchange timestamps, the matching windows stretch over the
    wrong messages, and the resulting counts are an artefact of the probe rather
    than a property of the exchange -- so the backlog is measured and reported
    instead of assumed away.

    The ABSOLUTE lag is meaningless on its own: it carries the host's clock
    offset against Kalshi's and the network path. The SPREAD is what matters,
    and `spread()` -- p99 minus minimum -- is the honest estimate of how far
    behind the consumer ever got.

    Buckets rather than samples, so the memory cost does not grow with a run
    that observes tens of millions of messages.
    """

    BUCKET_SECONDS = 0.05
    MAX_BUCKETS = 600  # 30 s, then one overflow bucket

    def __init__(self) -> None:
        self.count = 0
        self.buckets: dict[int, int] = {}
        self.min_lag: float | None = None
        self.max_lag: float | None = None

    def add(self, lag: float) -> None:
        self.count += 1
        self.min_lag = lag if self.min_lag is None else min(self.min_lag, lag)
        self.max_lag = lag if self.max_lag is None else max(self.max_lag, lag)
        index = int(lag // self.BUCKET_SECONDS)
        index = max(-1, min(index, self.MAX_BUCKETS))
        self.buckets[index] = self.buckets.get(index, 0) + 1

    def quantile(self, q: float) -> float | None:
        if not self.count:
            return None
        target = q * self.count
        seen = 0
        for index in sorted(self.buckets):
            seen += self.buckets[index]
            if seen >= target:
                return round((index + 1) * self.BUCKET_SECONDS, 3)
        return None

    def spread(self) -> float | None:
        """p99 minus the minimum: how far behind the consumer ever got."""
        p99, low = self.quantile(0.99), self.min_lag
        return None if p99 is None or low is None else round(p99 - low, 3)

    def summary(self) -> dict[str, Any]:
        return {
            "messages_with_exchange_timestamp": self.count,
            "min_seconds": None if self.min_lag is None else round(self.min_lag, 3),
            "p50_seconds": self.quantile(0.50),
            "p99_seconds": self.quantile(0.99),
            "max_seconds": None if self.max_lag is None else round(self.max_lag, 3),
            "spread_p99_minus_min_seconds": self.spread(),
        }


def exchange_ts_seconds(msg: dict[str, Any]) -> float | None:
    """The message's exchange timestamp in seconds, preferring `ts_ms`."""
    raw = msg.get("ts_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw) / 1000.0
    raw = msg.get("ts")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def ticker_touch_prices(msg: dict[str, Any]) -> tuple[int | None, int | None]:
    """The ticker message's touch prices on the canonical price scale."""

    def price(name: str) -> int | None:
        raw = msg.get(name)
        if raw is None:
            return None
        try:
            return to_price(raw)
        except BookError:
            return None

    return price("yes_bid_dollars"), price("yes_ask_dollars")


# -- Outcome labels. One per opportunity; they partition the opportunity set. --

OUTCOME_MATCHED = "matched_ticker_carried_changed_size"
OUTCOME_NO_TICKER = "no_ticker_update_in_window"
OUTCOME_TICKER_WITHOUT_SIZE = "ticker_update_did_not_carry_changed_size"
AMBIGUOUS_ADJACENT_BOOK_EVENT = "ambiguous_adjacent_book_event"
AMBIGUOUS_NO_PRIOR_TICKER = "ambiguous_no_prior_ticker_to_diff_against"
AMBIGUOUS_OTHER_FIELD_CHANGED = "ambiguous_other_ticker_field_changed_too"
AMBIGUOUS_CONSUMER_BACKLOG = "ambiguous_consumer_backlog_or_warmup"

UNAMBIGUOUS_OUTCOMES = (OUTCOME_MATCHED, OUTCOME_NO_TICKER, OUTCOME_TICKER_WITHOUT_SIZE)
AMBIGUOUS_OUTCOMES = (
    AMBIGUOUS_ADJACENT_BOOK_EVENT,
    AMBIGUOUS_NO_PRIOR_TICKER,
    AMBIGUOUS_OTHER_FIELD_CHANGED,
    AMBIGUOUS_CONSUMER_BACKLOG,
)
ALL_OUTCOMES = UNAMBIGUOUS_OUTCOMES + AMBIGUOUS_OUTCOMES

_PRECEDENCE = {
    OUTCOME_NO_TICKER: 0,
    OUTCOME_TICKER_WITHOUT_SIZE: 1,
    AMBIGUOUS_NO_PRIOR_TICKER: 2,
    AMBIGUOUS_OTHER_FIELD_CHANGED: 3,
    OUTCOME_MATCHED: 4,
}
"""How competing candidate ticker messages in one window are reconciled.

Strictly increasing in how much the candidate explains. "A message carrying the
new size arrived, but something else changed too" outranks "a message arrived
carrying nothing", because it is a more complete account of the window -- and it
is still counted as AMBIGUOUS, never as evidence. Only a clean, diffable,
size-carrying message reaches MATCHED."""


def outcome_for_candidate(
    observation: TickerObservation, expected: dict[str, int | None]
) -> str:
    """Classify one candidate ticker message against one opportunity."""
    if any(observation.size_of(side) != qty for side, qty in expected.items()):
        return OUTCOME_TICKER_WITHOUT_SIZE
    if observation.kind == TICKER_NO_PRIOR:
        return AMBIGUOUS_NO_PRIOR_TICKER
    if observation.kind == TICKER_OTHER_CHANGED:
        return AMBIGUOUS_OTHER_FIELD_CHANGED
    return OUTCOME_MATCHED


def prefer(current: str, candidate: str) -> str:
    """Keep the most explanatory outcome seen so far in one window."""
    return candidate if _PRECEDENCE[candidate] > _PRECEDENCE[current] else current


# -- Streaming resolution -----------------------------------------------------


@dataclass
class _Pending:
    """One opportunity whose matching window has not closed yet."""

    recv_ts: float
    expected: dict[str, int | None]
    outcome: str = OUTCOME_NO_TICKER
    isolated: bool = True

    backlogged: bool = False

    @property
    def deadline(self) -> float:
        return self.recv_ts + MATCH_WINDOW_POST_SECONDS

    def offer(self, observation: TickerObservation) -> None:
        self.outcome = prefer(self.outcome, outcome_for_candidate(observation, self.expected))

    def resolve(self) -> str:
        """
        The single outcome this opportunity contributes.

        Backlog first: if receive-time matching was not trustworthy across this
        window, nothing else about the window is worth ranking.
        """
        if self.backlogged:
            return AMBIGUOUS_CONSUMER_BACKLOG
        if not self.isolated:
            return AMBIGUOUS_ADJACENT_BOOK_EVENT
        return self.outcome


@dataclass
class _MarketState:
    """Everything one market needs, and nothing that outlives its window."""

    top: Top | None = None
    last_book_event_ts: float | None = None
    last_ticker_fields: dict[str, Any] | None = None
    recent_tickers: list[TickerObservation] = field(default_factory=list)
    pending: list[_Pending] = field(default_factory=list)


@dataclass
class ProbeAnalysis:
    """The whole result. Counts of protocol events and nothing else."""

    outcomes: dict[str, int] = field(default_factory=lambda: {k: 0 for k in ALL_OUTCOMES})
    top_of_book_transitions: int = 0
    orderbook_messages: int = 0
    ticker_messages: int = 0
    trade_messages: int = 0
    markets_with_book_events: int = 0
    markets_with_ticker_events: int = 0
    subscribed_markets: int = 0
    markets_with_initial_orderbook_snapshot: int = 0
    markets_with_initial_ticker: int = 0
    initial_state_window_seconds: float = 0.0
    touch_price_comparisons: int = 0
    touch_price_disagreements: int = 0
    touch_size_comparisons: int = 0
    touch_size_disagreements: int = 0
    errors: int = 0
    reconnects: int = 0
    subscription_limit_errors: int = 0
    sequence_gaps: int = 0
    observed_seconds: float = 0.0
    lag: LagHistogram = field(default_factory=LagHistogram)
    notes: list[str] = field(default_factory=list)

    @property
    def consumer_kept_up(self) -> bool:
        """
        Did this consumer stay close enough to the exchange for receive-time
        matching to be sound overall?

        Reported for context, not used as a gate: a run that includes the
        subscription warm-up burst always shows a large spread, and every
        opportunity inside that burst is already excluded one by one by the
        backlog guard. Gating the whole verdict on it as well would throw away
        a run whose steady state was perfectly sound.
        """
        spread = self.lag.spread()
        return spread is None or spread <= MATCH_WINDOW_POST_SECONDS

    @property
    def qualifying_opportunities(self) -> int:
        return sum(self.outcomes.values())

    @property
    def unambiguous_opportunities(self) -> int:
        return sum(self.outcomes[k] for k in UNAMBIGUOUS_OUTCOMES)

    @property
    def ambiguous_opportunities(self) -> int:
        return sum(self.outcomes[k] for k in AMBIGUOUS_OUTCOMES)

    @property
    def matched(self) -> int:
        return self.outcomes[OUTCOME_MATCHED]

    @property
    def match_rate(self) -> float | None:
        n = self.unambiguous_opportunities
        return None if n == 0 else self.matched / n

    def verdict(self) -> str:
        if self.unambiguous_opportunities < MIN_UNAMBIGUOUS_OPPORTUNITIES:
            return VERDICT_INSUFFICIENT
        rate = self.match_rate or 0.0
        if rate >= CONFIRM_RATE:
            return VERDICT_CONFIRMED
        if rate <= REFUTE_RATE:
            return VERDICT_REFUTED
        return VERDICT_INDETERMINATE

    def report(self) -> dict[str, Any]:
        """
        The ONLY quantities this probe may report.

        No market identities, no prices, no outcomes, no per-market counts, no
        research summary of any kind.
        """
        return {
            "probe": "threadc_ticker_size_only_emission",
            "channels_subscribed": list(PROBE_CHANNELS),
            "verdict": self.verdict(),
            "observed_seconds": round(self.observed_seconds, 1),
            "qualifying_best_touch_size_only_opportunities": self.qualifying_opportunities,
            "unambiguous_opportunities": self.unambiguous_opportunities,
            "with_corresponding_ticker_update_carrying_changed_size": self.matched,
            "without_corresponding_ticker_update": self.outcomes[OUTCOME_NO_TICKER],
            "ticker_update_present_but_size_not_carried":
                self.outcomes[OUTCOME_TICKER_WITHOUT_SIZE],
            "match_rate": None if self.match_rate is None else round(self.match_rate, 6),
            "ambiguity": {
                "total": self.ambiguous_opportunities,
                AMBIGUOUS_ADJACENT_BOOK_EVENT: self.outcomes[AMBIGUOUS_ADJACENT_BOOK_EVENT],
                AMBIGUOUS_NO_PRIOR_TICKER: self.outcomes[AMBIGUOUS_NO_PRIOR_TICKER],
                AMBIGUOUS_OTHER_FIELD_CHANGED: self.outcomes[AMBIGUOUS_OTHER_FIELD_CHANGED],
                AMBIGUOUS_CONSUMER_BACKLOG: self.outcomes[AMBIGUOUS_CONSUMER_BACKLOG],
                "match_window_pre_seconds": MATCH_WINDOW_PRE_SECONDS,
                "match_window_post_seconds": MATCH_WINDOW_POST_SECONDS,
                "sequence_gaps": self.sequence_gaps,
                "ticker_vs_book_touch_price_comparisons": self.touch_price_comparisons,
                "ticker_vs_book_touch_price_disagreements": self.touch_price_disagreements,
                "ticker_vs_book_touch_size_comparisons": self.touch_size_comparisons,
                "ticker_vs_book_touch_size_disagreements": self.touch_size_disagreements,
                "consumer_lag": self.lag.summary(),
                "consumer_kept_up": self.consumer_kept_up,
            },
            "observed_traffic": {
                "orderbook_messages": self.orderbook_messages,
                "ticker_messages": self.ticker_messages,
                "trade_messages": self.trade_messages,
                "top_of_book_transitions": self.top_of_book_transitions,
                "markets_with_book_events": self.markets_with_book_events,
                "markets_with_ticker_events": self.markets_with_ticker_events,
            },
            "initial_state_on_subscription": {
                "subscribed_markets": self.subscribed_markets,
                "window_seconds": self.initial_state_window_seconds,
                "markets_with_orderbook_snapshot_in_window":
                    self.markets_with_initial_orderbook_snapshot,
                "markets_with_ticker_message_in_window": self.markets_with_initial_ticker,
            },
            "errors": self.errors,
            "reconnects": self.reconnects,
            "subscription_limit_error_26_count": self.subscription_limit_errors,
            "thresholds": {
                "min_unambiguous_opportunities": MIN_UNAMBIGUOUS_OPPORTUNITIES,
                "confirm_rate": CONFIRM_RATE,
                "refute_rate": REFUTE_RATE,
            },
            "notes": list(self.notes),
        }


class StreamingMatcher:
    """
    Feed it book tops and ticker messages in receive order; it counts outcomes.

    Time advances only from observed receive timestamps, never from the wall
    clock, so a replay of recorded events produces exactly the same counts as
    the live run did. `flush()` closes every window still open at the end.
    """

    def __init__(self, analysis: ProbeAnalysis | None = None) -> None:
        self.analysis = analysis or ProbeAnalysis()
        self.markets: dict[str, _MarketState] = {}
        self._book_markets: set[str] = set()
        self._ticker_markets: set[str] = set()
        # Backlog bookkeeping. `baseline_lag` is the smallest lag ever seen and
        # stands in for the host's clock offset plus the network path; only
        # excursions ABOVE it mean this consumer is behind.
        self.baseline_lag: float | None = None
        self._backlog_open_since: float | None = None
        self._backlog_intervals: list[tuple[float, float]] = []
        self._first_message_ts: float | None = None

    # -- Backlog ---------------------------------------------------------------

    def note_lag(self, recv_ts: float, lag: float) -> None:
        """
        One observation of (receive time - exchange timestamp).

        Opens a backlog interval when the consumer falls more than
        LAG_GUARD_SECONDS behind its own baseline and closes it when it catches
        up, so an opportunity can later be asked whether its window overlapped
        one. The warm-up is seeded as the first such interval.
        """
        if self._first_message_ts is None:
            self._first_message_ts = recv_ts
            self._backlog_intervals.append((recv_ts - 1.0, recv_ts + WARMUP_SECONDS))
        self.baseline_lag = lag if self.baseline_lag is None else min(self.baseline_lag, lag)
        self.analysis.lag.add(lag)
        behind = (lag - self.baseline_lag) > LAG_GUARD_SECONDS
        if behind and self._backlog_open_since is None:
            self._backlog_open_since = recv_ts
        elif not behind and self._backlog_open_since is not None:
            self._backlog_intervals.append((self._backlog_open_since, recv_ts))
            self._backlog_open_since = None
        self._prune_backlog(recv_ts)

    def _prune_backlog(self, now: float) -> None:
        horizon = now - (MATCH_WINDOW_PRE_SECONDS + MATCH_WINDOW_POST_SECONDS + 1.0)
        if len(self._backlog_intervals) > 8:
            self._backlog_intervals = [
                iv for iv in self._backlog_intervals if iv[1] >= horizon
            ]

    def _window_backlogged(self, pending: _Pending) -> bool:
        start = pending.recv_ts - MATCH_WINDOW_PRE_SECONDS
        end = pending.deadline
        if self._backlog_open_since is not None and self._backlog_open_since <= end:
            return True
        return any(s <= end and e >= start for s, e in self._backlog_intervals)

    def _state(self, market: str) -> _MarketState:
        state = self.markets.get(market)
        if state is None:
            state = _MarketState()
            self.markets[market] = state
        return state

    # -- Book side -------------------------------------------------------------

    def on_top(self, market: str, top: Top, recv_ts: float) -> None:
        """One reconstructed top of book, after a book-changing message."""
        state = self._state(market)
        previous = state.top
        state.top = top
        if previous is None or previous == top:
            # Not a transition: a snapshot restating the same touch, or a delta
            # that only moved depth away from the best price.
            return

        self.analysis.top_of_book_transitions += 1
        self._book_markets.add(market)

        # An earlier opportunity whose window this event falls inside can no
        # longer be attributed cleanly.
        for pending in state.pending:
            if recv_ts <= pending.deadline:
                pending.isolated = False

        if is_opportunity(previous, top):
            expected = {
                side: top.qty_of(side) for side in size_changed_sides(previous, top)
            }
            new = _Pending(recv_ts=recv_ts, expected=expected)
            # Isolation looks backwards too: a book event just before this one,
            # inside the pre-window, is an equally good explanation for any
            # ticker message that arrives.
            if (
                state.last_book_event_ts is not None
                and recv_ts - state.last_book_event_ts <= MATCH_WINDOW_PRE_SECONDS
            ):
                new.isolated = False
            # Ticker messages that arrived just BEFORE the book event are still
            # candidates, so they are offered from the retained recent list.
            for observation in state.recent_tickers:
                if observation.recv_ts >= recv_ts - MATCH_WINDOW_PRE_SECONDS:
                    new.offer(observation)
            state.pending.append(new)

        state.last_book_event_ts = recv_ts
        self._expire(market, state, recv_ts)

    # -- Ticker side -----------------------------------------------------------

    def on_ticker(self, market: str, msg: dict[str, Any], recv_ts: float) -> None:
        """One raw ticker message body."""
        state = self._state(market)
        self._ticker_markets.add(market)
        observation, kept = reduce_ticker_message(msg, state.last_ticker_fields, recv_ts)
        state.last_ticker_fields = kept

        # Independent cross-check of the two channels' conventions. If ticker
        # touch prices and reconstructed touch prices systematically disagree,
        # every size comparison below would silently fail for a reason that has
        # nothing to do with emission semantics -- so it is measured rather than
        # assumed. Aggregate counts only.
        if state.top is not None:
            bid_price, ask_price = ticker_touch_prices(msg)
            if bid_price is not None and state.top.bid_price is not None:
                self.analysis.touch_price_comparisons += 1
                if bid_price != state.top.bid_price:
                    self.analysis.touch_price_disagreements += 1
                elif observation.bid_size is not None and state.top.bid_qty is not None:
                    self.analysis.touch_size_comparisons += 1
                    if observation.bid_size != state.top.bid_qty:
                        self.analysis.touch_size_disagreements += 1
            if ask_price is not None and state.top.ask_price is not None:
                self.analysis.touch_price_comparisons += 1
                if ask_price != state.top.ask_price:
                    self.analysis.touch_price_disagreements += 1
                elif observation.ask_size is not None and state.top.ask_qty is not None:
                    self.analysis.touch_size_comparisons += 1
                    if observation.ask_size != state.top.ask_qty:
                        self.analysis.touch_size_disagreements += 1

        for pending in state.pending:
            if pending.recv_ts - MATCH_WINDOW_PRE_SECONDS <= recv_ts <= pending.deadline:
                pending.offer(observation)

        state.recent_tickers.append(observation)
        self._expire(market, state, recv_ts)

    # -- Window closing --------------------------------------------------------

    def _expire(self, market: str, state: _MarketState, now: float) -> None:
        if state.pending:
            still_open = []
            for pending in state.pending:
                if now > pending.deadline:
                    pending.backlogged = pending.backlogged or self._window_backlogged(pending)
                    self.analysis.outcomes[pending.resolve()] += 1
                else:
                    still_open.append(pending)
            state.pending = still_open
        if state.recent_tickers:
            horizon = now - (MATCH_WINDOW_PRE_SECONDS + MATCH_WINDOW_POST_SECONDS)
            state.recent_tickers = [
                o for o in state.recent_tickers if o.recv_ts >= horizon
            ][-16:]

    def reset_books(self) -> None:
        """
        Forget every reconstructed book.

        Called on a sequence gap or a reconnect: a book carried across missing
        messages would produce transitions the exchange never sent. Pending
        opportunities are resolved on what was seen rather than discarded, since
        their windows were observed intact.
        """
        for state in self.markets.values():
            state.top = None
            state.last_book_event_ts = None

    def flush(self, now: float) -> ProbeAnalysis:
        """Close every open window and finish the counts."""
        if self._backlog_open_since is not None:
            self._backlog_intervals.append((self._backlog_open_since, now))
            self._backlog_open_since = None
        for state in self.markets.values():
            for pending in state.pending:
                pending.backlogged = pending.backlogged or self._window_backlogged(pending)
                self.analysis.outcomes[pending.resolve()] += 1
            state.pending = []
        self.analysis.markets_with_book_events = len(self._book_markets)
        self.analysis.markets_with_ticker_events = len(self._ticker_markets)
        return self.analysis


# -- Helpers ------------------------------------------------------------------


def select_probe_markets(tickers: Iterable[str], limit: int) -> list[str]:
    """
    The mechanical selection rule, fixed before the probe was run.

    Sort every currently-active market in the frozen collection universe by its
    ticker and take the first `limit`. Lexicographic order over exchange-assigned
    tickers is unrelated to activity, spread, depth, liquidity or time to close,
    so the subset is bounded without being chosen for interestingness -- and it
    is reproducible, which a sample drawn from live traffic would not be.
    `limit <= 0` means every active market, which is the neutral default.
    """
    ordered = sorted(set(tickers))
    return ordered if limit <= 0 else ordered[:limit]


def format_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


__all__ = [
    "Top", "TickerObservation", "ProbeAnalysis", "StreamingMatcher",
    "reduce_ticker_message", "ticker_touch_prices", "outcome_for_candidate",
    "LagHistogram", "exchange_ts_seconds",
    "prefer", "is_opportunity", "size_changed_sides", "select_probe_markets",
    "format_report", "PROBE_CHANNELS", "TICKER_CHANNEL", "ORDERBOOK_CHANNEL",
    "VERDICT_CONFIRMED", "VERDICT_REFUTED", "VERDICT_INDETERMINATE",
    "VERDICT_INSUFFICIENT", "MIN_UNAMBIGUOUS_OPPORTUNITIES",
    "OUTCOME_MATCHED", "OUTCOME_NO_TICKER", "OUTCOME_TICKER_WITHOUT_SIZE",
    "AMBIGUOUS_ADJACENT_BOOK_EVENT", "AMBIGUOUS_NO_PRIOR_TICKER",
    "AMBIGUOUS_OTHER_FIELD_CHANGED", "AMBIGUOUS_CONSUMER_BACKLOG",
    "ALL_OUTCOMES", "UNAMBIGUOUS_OUTCOMES",
    "TICKER_CLEAN", "TICKER_NO_PRIOR", "TICKER_OTHER_CHANGED",
]
