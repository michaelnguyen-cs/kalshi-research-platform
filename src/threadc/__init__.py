"""
threadc — the Thread C MLB ticker/trade banking recorder.

Operational data banking, and nothing else. This package collects neutral raw
evidence: the untouched WebSocket frames needed to reconstruct order books
later, plus the census and universe records that establish which markets existed
and when the recorder learned of them.

It contains no research analysis. There is deliberately no price summary, no
spread or depth statistic, no per-market activity measure, no settlement
lookup, no strategy and no hypothesis-dependent slicing anywhere in it. What is
recorded is decided by official exchange metadata (Collection Universe v1) and
by the clock, never by anything observed in the books.

READ-ONLY against Kalshi by construction. Nothing here constructs a
KalshiClient -- the class that owns place_order()/cancel_order() -- and the HTTP
layer refuses any method but GET and any path outside a four-entry allowlist.

Reuses the proven Thread A recorder infrastructure rather than reimplementing
it: depthrec.rawcodec (frame compression), depthrec.rawwriter.RawWriter (the
off-loop writer thread), depthrec.session.ResearchSession (the WebSocket
snapshot/delta session) and depthrec.census.RestCensus (the market census and
subscription-refresh mechanism, at its own frozen cadence).
"""

__all__ = ["frozen"]
