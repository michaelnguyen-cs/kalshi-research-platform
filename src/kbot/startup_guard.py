"""
Fail-closed startup guard: refuse to build an order-capable engine unless the
effective series and DRY_RUN are both unambiguously resolvable, and refuse
outright to run any KXETH* series outside dry-run.

WHY THIS EXISTS
---------------
KXETH15M is the newest strategy in the repo: no backtest history, no fitted
calibrator, trading on raw lognormal probabilities, and required to earn its
way up from the "backtest" gate on forward evidence alone (see
kbot.strategy.eth_15m). Nothing in the pre-existing safety stack is
ETH-specific:

  * kbot.cli's --live path checks KALSHI_ENV=prod + DRY_RUN=false + a typed
    confirmation. Series-blind.
  * kbot.runner.Runner.__init__ requires dry_run or live_confirmed=True.
    Series-blind.
  * Runner._gate_check refuses to route a real order for a strategy whose
    gate level is not in _LIVE_APPROVED_LEVELS. Series-blind, and it is a
    PER-SIGNAL check inside the running loop -- by the time it says no, the
    live client, the live executor and the live WS session already exist.
  * kbot.paper.PaperTrader raises unless dry_run. Series-blind, and it is
    bypassed entirely by the --strategies / --live paths that build a Runner
    directly.

So a single `kbot run --strategies kxeth15m --live` against a promoted gate
was the whole distance between "unvalidated model" and "real money". This
module closes that at construction time instead of at signal time.

WHAT "FAIL CLOSED" MEANS HERE
-----------------------------
Every ambiguity raises StartupGuardError and nothing is constructed. The guard
never downgrades to dry-run and carries on: silently continuing under a
narrower interpretation of a config the operator got wrong is how a safety
check becomes a rubber stamp.

DRY_RUN, SPECIFICALLY
---------------------
By the time any component reads it, `Settings.dry_run` is already a strict
`bool` -- pydantic parsed it at Settings() construction and would have raised
on an unparseable string. That parse is LOOSER than this project wants: it
accepts "1"/"yes"/"on"/"t"/"y" as true and "0"/"no"/"off"/"f"/"n" as false, so
`DRY_RUN=0` silently arms live mode. This guard re-reads the RAW value from the
same sources pydantic-settings reads (process environment first, then the
.env file, case-insensitively -- Settings sets case_sensitive=False) and
requires the literal token `true` or `false`. Anything else set is fatal.

An ABSENT DRY_RUN is not fatal: absent means the Settings default applies, and
that default is True -- the closed direction, and the only value absence can
ever produce. What absence can never do is arm live mode, because a resolved
`dry_run is False` with no raw source is itself rejected below. Live mode
therefore always requires someone to have typed the word `false`.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, NamedTuple

import structlog

log = structlog.get_logger(__name__)

# Series whose live trading is refused outright. Matched as a case-insensitive
# PREFIX on the resolved series, not an equality test against "KXETH15M": a
# future KXETH hourly/daily series would otherwise inherit none of this.
ETH_SERIES_PREFIX = "KXETH"

# The only two raw DRY_RUN spellings this project accepts. Deliberately
# narrower than pydantic's bool parser -- see the module docstring.
RECOGNIZED_TRUE = "true"
RECOGNIZED_FALSE = "false"

# Distinguishes "caller did not supply a raw value, go read the environment"
# from "caller supplied None, meaning DRY_RUN is genuinely unset". None is a
# meaningful input here, so it cannot double as the default.
_UNSET: Any = object()


class StartupGuardError(RuntimeError):
    """
    A fail-closed refusal to start. Raised before any order-capable component
    is constructed; there is no recovery path and no dry-run fallback.
    """


class GuardResult(NamedTuple):
    """What the guard resolved, for logging and for tests to assert on."""

    series: frozenset[str]
    dry_run: bool


def raw_dry_run_value(cfg: Any = None) -> str | None:
    """
    The RAW DRY_RUN string as pydantic-settings would have seen it, or None if
    it is not set anywhere.

    Resolution order mirrors pydantic-settings' own precedence: process
    environment first, then the .env file named by the Settings model_config.
    Both lookups are case-insensitive because Settings sets
    case_sensitive=False, so `Dry_Run=false` in a shell is a live-arming value
    that a case-sensitive check would walk straight past.

    Raises StartupGuardError if the .env file exists but cannot be read or
    parsed -- an unreadable config source is an unresolved DRY_RUN, which is
    exactly the case this module refuses to guess at.
    """
    for key, value in os.environ.items():
        if key.lower() == "dry_run":
            return value

    env_file: Any = ".env"
    model_config = getattr(cfg, "model_config", None)
    if isinstance(model_config, Mapping):
        env_file = model_config.get("env_file", ".env")
    # env_file may legitimately be None (an env-only Settings) or a list of
    # candidate files; pydantic-settings applies a list in order, later files
    # winning, so the last existing one is the one whose value survives.
    if env_file is None:
        return None
    if isinstance(env_file, (list, tuple)):
        candidates = [str(p) for p in env_file]
    else:
        candidates = [str(env_file)]

    for path in reversed(candidates):
        if not os.path.isfile(path):
            continue
        try:
            from dotenv import dotenv_values

            values = dotenv_values(path)
        except Exception as exc:  # noqa: BLE001 -- unreadable config is fatal, not ignorable
            raise StartupGuardError(
                f"Cannot read {path!r} to verify DRY_RUN ({type(exc).__name__}: {exc}). "
                "Refusing to start with an unverifiable DRY_RUN."
            ) from exc
        for key, value in values.items():
            if key is not None and key.lower() == "dry_run":
                return value
    return None


def resolve_dry_run(cfg: Any, *, raw: Any = _UNSET) -> bool:
    """
    The effective DRY_RUN, or raise StartupGuardError if it is not an explicit
    recognized true/false value.

    `raw` is the raw environment/.env string (None = unset); it defaults to
    reading the environment via raw_dry_run_value(). Tests pass it explicitly.
    """
    if raw is _UNSET:
        raw = raw_dry_run_value(cfg)

    resolved = getattr(cfg, "dry_run", None)
    # `bool` and nothing else. A duck-typed cfg carrying the STRING "false"
    # would be truthy everywhere in this codebase -- a live config that reads
    # as dry-run at every call site.
    if not isinstance(resolved, bool):
        raise StartupGuardError(
            f"DRY_RUN resolved to {resolved!r} ({type(resolved).__name__}), not a bool. "
            "Refusing to start: every dry-run check in this process is a plain "
            "truth test, so a non-bool silently answers them all."
        )

    if raw is None:
        # Unset: the Settings default applies. That default is True, so the
        # only value absence can produce is the closed one. A resolved False
        # with no raw source means live mode was armed by something other than
        # an explicit config value -- refuse it.
        if resolved is False:
            raise StartupGuardError(
                "DRY_RUN is not set in the environment or .env, but the resolved "
                "config says dry_run=False. Live mode requires an explicit "
                f"DRY_RUN={RECOGNIZED_FALSE}; refusing to start."
            )
        return True

    if not isinstance(raw, str):
        raise StartupGuardError(
            f"DRY_RUN raw value {raw!r} is not a string. Refusing to start."
        )

    normalized = raw.strip().lower()
    if normalized == RECOGNIZED_TRUE:
        parsed = True
    elif normalized == RECOGNIZED_FALSE:
        parsed = False
    else:
        raise StartupGuardError(
            f"DRY_RUN={raw!r} is not an explicit recognized true/false value. "
            f"Set it to exactly {RECOGNIZED_TRUE!r} or {RECOGNIZED_FALSE!r} "
            "(pydantic would also accept 1/0/yes/no/on/off/t/f/y/n; this project "
            "does not, because DRY_RUN=0 is too quiet a way to arm live mode)."
        )

    if parsed is not resolved:
        raise StartupGuardError(
            f"DRY_RUN={raw!r} disagrees with the resolved config value "
            f"dry_run={resolved!r}. Refusing to start on an ambiguous DRY_RUN."
        )
    return parsed


def _series_of_strategy(name: str, strategy: Any) -> set[str]:
    """
    Every Kalshi series `strategy` can trade, upper-cased.

    Unions series_tickers (what the runner subscribes and routes on) with the
    calibrator-domain attributes series_15m / series_hourly, because a
    strategy can carry a series identity in either place -- BTCHourlyStrategy
    is constructed with a narrowed series_tickers while series_15m keeps the
    full domain.
    """
    found: set[str] = set()

    declared = getattr(strategy, "series_tickers", None) or []
    if isinstance(declared, str):  # a bare string is a typo, not a one-item list
        raise StartupGuardError(
            f"Strategy {name!r} declares series_tickers as the string {declared!r}; "
            "it must be a list of series. Refusing to start."
        )
    for entry in declared:
        if isinstance(entry, str) and entry.strip():
            found.add(entry.strip().upper())

    for attr in ("series_15m", "series_hourly"):
        value = getattr(strategy, attr, None)
        if isinstance(value, str) and value.strip():
            found.add(value.strip().upper())

    if not found:
        raise StartupGuardError(
            f"Cannot resolve the effective series for strategy {name!r}: it declares "
            "no series_tickers and no series_15m/series_hourly. Refusing to start "
            "rather than run an order-capable engine against an unknown series."
        )
    return found


def _series_of_ticker(ticker: Any) -> str:
    """
    The series prefix of an explicit ticker ("KXETH15M-26JUL311800-00" ->
    "KXETH15M"). Explicit tickers bypass series discovery, so they are a second
    independent source of the effective series and are checked too.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise StartupGuardError(
            f"Cannot resolve the effective series for ticker {ticker!r}. Refusing to start."
        )
    prefix = ticker.strip().split("-", 1)[0].strip().upper()
    if not prefix:
        raise StartupGuardError(
            f"Cannot resolve the effective series for ticker {ticker!r}. Refusing to start."
        )
    return prefix


def resolve_series(
    strategies: Mapping[str, Any],
    tickers: Iterable[Any] | None = None,
) -> frozenset[str]:
    """
    The union of every series this process could trade, or raise
    StartupGuardError if it cannot be resolved.
    """
    if not strategies:
        raise StartupGuardError(
            "Cannot resolve the effective series: no strategies were loaded. "
            "Refusing to start."
        )

    series: set[str] = set()
    for name, strategy in strategies.items():
        series |= _series_of_strategy(name, strategy)
    for ticker in tickers or []:
        series.add(_series_of_ticker(ticker))
    return frozenset(series)


def assert_startup_safe(
    strategies: Mapping[str, Any],
    cfg: Any,
    *,
    tickers: Iterable[Any] | None = None,
    raw_dry_run: Any = _UNSET,
) -> GuardResult:
    """
    Fail-closed pre-flight check. Call this BEFORE constructing anything that
    can submit an order (kbot.kalshi_client.KalshiClient, kbot.execution
    .Executor) -- kbot.runner.Runner.__init__ calls it as its first statement,
    which is the one place every order-capable path funnels through.

    Raises StartupGuardError on: an unresolvable effective series, a DRY_RUN
    that is not an explicit recognized true/false value, or any KXETH* series
    with DRY_RUN not true.
    """
    series = resolve_series(strategies, tickers)
    dry_run = resolve_dry_run(cfg, raw=raw_dry_run)

    eth = sorted(s for s in series if s.startswith(ETH_SERIES_PREFIX))
    if eth and dry_run is not True:
        raise StartupGuardError(
            f"Refusing to start: series {eth} may only be traded with DRY_RUN="
            f"{RECOGNIZED_TRUE}, and DRY_RUN is {RECOGNIZED_FALSE}. "
            "KXETH15M has no backtest history and no fitted calibrator -- it "
            "trades on raw lognormal probabilities and must earn promotion on "
            "forward paper evidence (see kbot.strategy.eth_15m). Run it as a "
            "paper deployment; changing that is a deliberate edit to "
            "kbot.startup_guard, not a flag."
        )

    log.info(
        "startup_guard.ok",
        series=sorted(series),
        dry_run=dry_run,
        eth_series=eth,
    )
    return GuardResult(series=series, dry_run=dry_run)
