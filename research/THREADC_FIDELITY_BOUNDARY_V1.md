# Thread C collection fidelity boundary, v1

**Status:** frozen with the ticker/trade bank. Written before any collected
message was inspected.

This document states, explicitly and in advance, what the Thread C bank
preserves and what it does not. It exists so that no later analysis can quietly
assume a fidelity the archive never had.

## What changed, and why

The original Thread C design banked the raw `orderbook_delta` channel — full L2
snapshots and deltas — for every open market in the frozen collection universe.
A full live-evening capacity test falsified that design **operationally**, not
scientifically:

| observed | value |
| --- | --- |
| segment state | COMPLETE, integrity ok |
| errors / reconnects / error-26 | 0 / 0 / 0 |
| simultaneous subscribed markets | 10,270 on one session |
| peak RSS | ~402 MB |
| CPU | ~88% of one OCPU |
| raw orderbook storage | ~3.4+ GiB/hour |

At ~3.4 GiB/hour, 24/7 collection through the configured November collection end
is physically impractical on the available storage. The design works; it does
not fit. **No book contents were inspected to reach that conclusion** — the
decision came from byte counts, CPU, RSS and connection health only.

The replacement banks Kalshi's public `ticker` and `trade` channels for the same
frozen universe, under the same append-only rules and the same 24/7 continuous
coverage.

## What the bank preserves

Every message received on either channel, byte for byte, with no sampling, no
throttling, no coalescing, and no selection by market state.

**Per ticker message** (as Kalshi sends it): market ticker/id; `yes_bid_dollars`
and `yes_ask_dollars`; `yes_bid_size_fp` and `yes_ask_size_fp`; `price_dollars`;
`volume_fp`; `open_interest_fp`; `dollar_volume`; `dollar_open_interest`;
`last_trade_size_fp`; `ts_ms` (preferred) and the deprecated `ts` / `time`.

**Per trade message:** `trade_id`; market ticker; `yes_price_dollars` and
`no_price_dollars`; `count_fp`; the canonical taker fields the protocol supplies
(`taker_side`, `taker_outcome_side`, `taker_book_side`, `is_block_trade`);
`ts_ms` and the deprecated `ts`.

**Per frame, added by the recorder:** receive timestamp, WebSocket shard label,
connection epoch, direction, and the SHA-256 of the exact bytes received.

## The boundary: displayed state at the touch, not the executable ladder

`ticker` preserves **the displayed state at the touch**. It is not a book.

### Questions this bank can support

* Was at least X displayed contracts quoted at the current best ask (or best
  bid) at timestamp T?

  Subject always to the ordinary distinction between a **historical displayed
  quote** and a **guaranteed live fill**: a size that was displayed at T is
  evidence about the state of the book at T, never a promise that an order sent
  at T would have filled against it.

* What was the touch price and displayed touch size over time, and what public
  trades printed, at what price, in what size, on which side.

### Questions this bank cannot support, and never will

* Full L2 ladders.
* Quantities at any price away from the best bid/ask.
* Queue position.
* Every cancellation or modification away from the touch. Only changes that
  alter a displayed touch field are observable at all.
* Deep-book order flow imbalance.
* L2 microprice.
* Average fill price for an order large enough to exhaust the displayed touch
  and walk deeper levels.

### The rule that follows

**Do not later infer deeper-book execution from ticker data.** An estimate of
depth beyond the touch, reconstructed from touch observations, is a fabrication
regardless of how it is labelled.

**Do not compensate for that loss by narrowing scope.** The frozen universe, the
game times, the times-to-close and the research horizons stay exactly as they
are. Choosing markets, innings, or T-minus windows where touch-only data happens
to look adequate would convert an honest fidelity limit into a hidden selection
effect — which is worse than the limit.

## Initial state, gaps, and reconstruction

The bank is reconstructible at an arbitrary timestamp only if the state at the
start of each subscription is explicit. Two facts govern this:

1. **`orderbook_delta` sends an `orderbook_snapshot` on subscribe. `ticker` does
   not send anything on subscribe.** A market that has not traded and whose
   quotes have not moved is simply silent, so absence of a ticker message can
   never be read as "the state is what it was".

2. **`GET /markets` carries the same displayed touch fields** —
   `yes_bid_dollars`, `yes_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`,
   `last_price_dollars`, `volume_fp`, `open_interest_fp` — that the ticker
   channel carries.

So the recorder establishes the initial state explicitly, from REST, at every
subscription establishment: at startup, at every reconnect, and for every market
newly added to a live subscription. Each such state is banked as an append-only
row stamped with the shard, the connection epoch and the REST observation time,
so a reader can always answer "what was displayed when this subscription began"
without assuming anything.

Reconnects and gaps remain detectable from the session/epoch metadata already in
the archive: every connection epoch is a row, every disconnect a health event,
and every sequence gap is recorded rather than smoothed over.

## What did not change

Collection Universe v1 and its frozen predicate; Rule 1 append-only additions;
incomplete-census behaviour; daily 09:00 UTC full-universe discovery; the shared
REST budget; the restart-storm guard; WS-first startup ordering; daily UTC
internal archive rotation; segment states, integrity checks, hashes and
manifests; low-disk terminal behaviour; the collection-end configuration and its
monotone-later rule; systemd restart behaviour; the daily health check; and the
structural no-order path.
