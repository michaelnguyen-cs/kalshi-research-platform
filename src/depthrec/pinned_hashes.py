"""
Expected pinned-classifier hashes. GENERATED -- do not hand-edit.

Produced by scripts/depthrec_gen_pinned_hashes.py against the KILL_CRITERIA.md
section 2 frozen commit 6ca4101bf28a6233bb207e875c0bfed03dcd6637 on 2026-08-22T10:19:14+00:00.

These values are committed constants. depthrec.integrity compares the installed
classifier against them; it never recomputes them from HEAD. If a check fails,
the pinned classifier moved -- that is a finding to investigate, not a reason to
re-run the generator.

PINNED_SOURCES below is the human-readable audit trail: the normalised source of
every protected element as it stood at the frozen commit. It is not used by the
check (ast.unparse output can shift between Python versions and a cosmetic shift
must not be able to fail a recorder); it is there so a reviewer can see what was
pinned without running anything.
"""

from __future__ import annotations

_MANIFEST = {
    "expected_longshot_digest": "c7ef132efb26d21a62f2527bfc6f081900fe15486cc6d5fd0381644caaeb0d81",
    "expected_longshot_manifest": {
        "Observation": [
            "24a3bf7a4793a0e413e17d2226c1131bf42e1ad4ee94ebc8c1877720872ba5b8"
        ],
        "_BUCKET_WIDTH": [
            "60be23c7e15599b776f54ef30def5d97dcef2dc3fc8b062e2b94bd94583af49d"
        ],
        "_CONTRACTS_PER_ORDER": [
            "f63e3f55a63a3d0c609f11e80fff73e64c447767ff1cd51b7b7481bbcd2eef07"
        ],
        "_FEE_COEFFICIENT": [
            "a791cfd5267327f77daae7dd74be312ccb6235690c12855d1793528e602302f3"
        ],
        "_MIN_INSAMPLE_TRADES": [
            "0a8732df495c61d598769f86226f33b6a1b388b85b945d88cf8d9d17d7a7d17b"
        ],
        "_THRESHOLDS": [
            "4ed26a3dfc60751aa5ce4a46a1b6ef6eb9917451ab79c346e5a7cdfe1bcc03d2"
        ],
        "favorite_side": [
            "b8fb3f715b1b6a214a3efd9dc4410979eb040af5c7332355d73ff3a5112c8322"
        ],
        "favorite_trades": [
            "68027aeb24e60227de4dedcd57e4ff7efa7ddf236c197000c92ae8c40bdab5b0"
        ],
        "fee_per_contract": [
            "a058aa51fac80b1e76eb25e7f50300e176031c2a4154e52a6a1edf263d451e57"
        ],
        "qualifying": [
            "4887355064e06a1da859dde2d5a5fbd10732a5eca7c29fef237322ece674fbb8"
        ],
        "side_won": [
            "20cafdb46de743a1d9d1f827572f86cf4b17d6f4181ae99659c1d637eaad4c9e"
        ],
        "trade_ev": [
            "0ede509691511273d04974a2f81d40972191422ff3cca65009181e65f3114aba"
        ]
    },
    "expected_study": {
        "bytes": "36707",
        "git_blob": "e8dd06f88f3c4247bcff0d41810ba33030188846",
        "path": "scripts/market_calibration_study.py",
        "sha256": "6e14cf6921efc083bb6242993be8eaa91854e4f8481a98899a92c3ecf3983517"
    },
    "frozen_commit": "6ca4101bf28a6233bb207e875c0bfed03dcd6637",
    "generated_utc": "2026-08-22T10:19:14+00:00",
    "generator_python": "3.11.9",
    "normalisation_version": "depthrec-ast-1",
    "pinned_sources": {
        "Observation": [
            "class Observation:\n    __slots__ = ('series', 'ticker', 'close_ts', 'slice_minutes', 'yes_price', 'price_source', 'settled_yes', 'ev_cheaper_side')\n\n    def __init__(self, series, ticker, close_ts, slice_minutes, yes_price, price_source, settled_yes, ev_cheaper_side):\n        self.series = series\n        self.ticker = ticker\n        self.close_ts = close_ts\n        self.slice_minutes = slice_minutes\n        self.yes_price = yes_price\n        self.price_source = price_source\n        self.settled_yes = settled_yes\n        self.ev_cheaper_side = ev_cheaper_side"
        ],
        "_BUCKET_WIDTH": [
            "_BUCKET_WIDTH = 0.05"
        ],
        "_CONTRACTS_PER_ORDER": [
            "_CONTRACTS_PER_ORDER = 10"
        ],
        "_FEE_COEFFICIENT": [
            "_FEE_COEFFICIENT = 0.07"
        ],
        "_MIN_INSAMPLE_TRADES": [
            "_MIN_INSAMPLE_TRADES = 100"
        ],
        "_THRESHOLDS": [
            "_THRESHOLDS = [0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]"
        ],
        "favorite_side": [
            "def favorite_side(yes_price: float) -> tuple[str, float]:\n    if yes_price >= 0.5:\n        return ('YES', yes_price)\n    return ('NO', 1.0 - yes_price)"
        ],
        "favorite_trades": [
            "def favorite_trades(observations: list[Observation], threshold: float, slice_minutes: int, coefficient: float) -> tuple[list[float], list[bool]]:\n    evs: list[float] = []\n    wins: list[bool] = []\n    for o in observations:\n        if o.slice_minutes != slice_minutes:\n            continue\n        side, entry = favorite_side(o.yes_price)\n        if entry < threshold:\n            continue\n        won = side_won(side, o.settled_yes)\n        evs.append(trade_ev(entry, won, coefficient))\n        wins.append(won)\n    return (evs, wins)"
        ],
        "fee_per_contract": [
            "from market_calibration_study import fee_per_contract"
        ],
        "qualifying": [
            "def qualifying(observations: list[Observation], threshold: float, slice_minutes: int) -> list[Observation]:\n    return [o for o in observations if o.slice_minutes == slice_minutes and favorite_side(o.yes_price)[1] >= threshold]"
        ],
        "side_won": [
            "def side_won(side: str, settled_yes: bool) -> bool:\n    return settled_yes if side == 'YES' else not settled_yes"
        ],
        "trade_ev": [
            "def trade_ev(entry: float, won: bool, coefficient: float=_FEE_COEFFICIENT) -> float:\n    return (1.0 if won else 0.0) - entry - fee_per_contract(entry, _CONTRACTS_PER_ORDER, coefficient)"
        ]
    },
    "protected_seeds": [
        "favorite_side",
        "qualifying",
        "favorite_trades",
        "_THRESHOLDS",
        "_BUCKET_WIDTH",
        "_MIN_INSAMPLE_TRADES",
        "_CONTRACTS_PER_ORDER",
        "_FEE_COEFFICIENT"
    ]
}

FROZEN_COMMIT: str = _MANIFEST["frozen_commit"]
GENERATED_UTC: str = _MANIFEST["generated_utc"]
GENERATOR_PYTHON: str = _MANIFEST["generator_python"]
NORMALISATION_VERSION: str = _MANIFEST["normalisation_version"]

EXPECTED_STUDY: dict[str, str] = _MANIFEST["expected_study"]
"""Whole-file pin for scripts/market_calibration_study.py."""

EXPECTED_LONGSHOT_MANIFEST: dict[str, list[str]] = _MANIFEST["expected_longshot_manifest"]
"""Per-element pin for the protected functions in scripts/test_longshot_rule.py
plus the transitive closure of everything they reference."""

EXPECTED_LONGSHOT_DIGEST: str = _MANIFEST["expected_longshot_digest"]
"""One digest over the whole manifest, for a single-line startup log field."""

PROTECTED_SEEDS: list[str] = _MANIFEST["protected_seeds"]
PINNED_SOURCES: dict[str, list[str]] = _MANIFEST["pinned_sources"]
