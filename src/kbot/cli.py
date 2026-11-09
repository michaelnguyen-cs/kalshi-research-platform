"""
Typer CLI entry points.

All commands are exposed via `kbot <command>`.  The app is wired into
pyproject.toml as the `kbot` script entry point.

Commands:
  kbot markets [--status open|closed|settled] [--limit N]
  kbot balance
  kbot stream TICKER [--channels ...]
  kbot backtest --strategy STRATEGY --data PATH [--bankroll N]
  kbot paper --strategy STRATEGY TICKERS...
  kbot run --strategy STRATEGY TICKERS... [--live]
  kbot log [--type TYPE] [--limit N]
  kbot gates status
  kbot gates promote STRATEGY
"""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

import structlog
import typer
from rich.console import Console
from rich.table import Table

from kbot.config import settings

log = structlog.get_logger(__name__)
app = typer.Typer(no_args_is_help=True, help="kbot — Kalshi algorithmic trading bot")
console = Console(legacy_windows=False)

gates_app = typer.Typer(no_args_is_help=True, help="RULE R3 kill-fast strategy gate registry")
app.add_typer(gates_app, name="gates")

# ── Dead-man's switch (2026-07) ────────────────────────────────────────────────
# The kbot-watchdog scheduled task died silently on 2026-07-01
# (STATUS_CONTROL_C_EXIT) and nobody noticed until a manual check the next
# day. This threshold is deliberately independent of both the heartbeat's
# own "OK alive" display logic (600s) and whatever the scheduled task
# reports (see _check_watchdog_task) -- it fires on its own so a dead bot
# can't hide behind a task that still says "Running".
_HEARTBEAT_DEADMAN_SECONDS: float = 30 * 60
_WATCHDOG_TASK_NAME = "kbot-watchdog"
# On Linux the watchdog role is filled by a systemd service, not a scheduled
# task -- the unit docs/CLOUD_MIGRATION.md registers as kbot.service. Kept as
# the bare name here: `systemctl is-active <name>` implies the .service suffix.
_LINUX_SERVICE_NAME = "kbot"


def heartbeat_is_stale(
    hb_path: Path,
    now: float,
    max_age_seconds: float = _HEARTBEAT_DEADMAN_SECONDS,
) -> bool:
    """
    True if `hb_path` is missing, unreadable, or older than `max_age_seconds`.

    A missing/unreadable file counts as stale -- if the bot never got far
    enough to write a heartbeat, that's exactly the condition this switch
    exists to catch.
    """
    if not hb_path.exists():
        return True
    try:
        hb = json.loads(hb_path.read_text())
        age_s = now - hb.get("ts", 0)
    except (json.JSONDecodeError, OSError, TypeError):
        return True
    return age_s > max_age_seconds


# Task states / Last Result codes that mean "the watchdog is fine".
# 267009 = 0x41301 = SCHED_S_TASK_RUNNING -- normal while the task is active.
# -2147020576 = 0x800710E0 -- Task Scheduler records this when the 5-minute
# re-check trigger (see install-task.ps1) fires while the watchdog is already
# running and MultipleInstancesPolicy=IgnoreNew correctly refuses to start a
# duplicate. That is the expected, healthy outcome of the sleep/wake-recovery
# design, not an error -- treat it as OK so `kbot status` doesn't false-alarm.
_WATCHDOG_OK_STATUSES = {"running", "ready"}
_WATCHDOG_OK_RESULTS = {"0", "267009", "-2147020576"}


def _check_watchdog_task(
    task_name: str = _WATCHDOG_TASK_NAME,
    service_name: str = _LINUX_SERVICE_NAME,
) -> None:
    """
    Report on the process supervisor that keeps kbot alive, warning if it
    looks unhealthy. The supervisor differs by OS -- a scheduled task on
    Windows (schtasks), a systemd service on Linux (systemctl) -- so dispatch
    on platform.system(). Best-effort throughout: every path swallows its own
    errors so `kbot status` never crashes because of this check.
    """
    system = platform.system()
    if system == "Windows":
        _check_watchdog_task_windows(task_name)
    elif system == "Linux":
        _check_systemd_service_linux(service_name)
    else:
        console.print(
            f"[yellow]Watchdog health check not supported on {system or 'unknown'!r}; "
            f"skipping.[/yellow]"
        )


def _check_systemd_service_linux(service_name: str = _LINUX_SERVICE_NAME) -> None:
    """
    Report whether the kbot systemd service is active via `systemctl
    is-active` -- the same machine-readable probe docs/CLOUD_MIGRATION.md uses
    to health-check the kbot unit. is-active prints the state word to
    stdout ("active", "inactive", "failed", "activating", ...) and exits 0 only
    when active. "activating" is the transient RestartSec backoff after a crash
    -- not up yet, so it's reported like any other not-active state.
    Best-effort: a failure to query is itself a warning, never raised.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"[yellow]Could not query systemd service {service_name!r}: {exc}[/yellow]")
        return

    state = proc.stdout.strip()
    if state == "active":
        return

    console.print(
        f"[red]!! SYSTEMD SERVICE {service_name!r} NOT ACTIVE[/red]  "
        f"state={state or 'unknown'!r} — the service may have died or never started. "
        f"Check `systemctl status {service_name}`. See docs/CLOUD_MIGRATION.md."
    )


def _check_watchdog_task_windows(task_name: str = _WATCHDOG_TASK_NAME) -> None:
    """
    Query the kbot-watchdog Windows scheduled task and print a warning if
    it's not in a healthy state (Status not Running/Ready, or Last Result
    not a known-good code). schtasks is Windows-only and this is best-effort
    -- any failure to query is itself reported as a warning, never raised,
    so `kbot status` never crashes because of this check.
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"[yellow]Could not query scheduled task {task_name!r}: {exc}[/yellow]")
        return

    if proc.returncode != 0:
        console.print(
            f"[red]!! Scheduled task {task_name!r} not found or query failed[/red] "
            f"(schtasks exit {proc.returncode}) — is the watchdog installed? "
            f"See scripts/install-task.ps1."
        )
        return

    status_val: str | None = None
    result_val: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("Status:"):
            status_val = line.split(":", 1)[1].strip()
        elif line.startswith("Last Result:"):
            result_val = line.split(":", 1)[1].strip()

    status_ok = status_val is not None and status_val.strip().lower() in _WATCHDOG_OK_STATUSES
    result_ok = result_val is not None and result_val.strip() in _WATCHDOG_OK_RESULTS

    if not (status_ok and result_ok):
        console.print(
            f"[red]!! WATCHDOG TASK UNHEALTHY[/red]  status={status_val!r}  "
            f"last_result={result_val!r} — task may have died silently, check it."
        )


def _configure_logging(verbose: bool) -> None:
    from kbot.log_config import configure_logging

    configure_logging(verbose)


@app.callback()
def _global_options(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show DEBUG log output"),
) -> None:
    _configure_logging(verbose)


# ── markets ───────────────────────────────────────────────────────────────────

@app.command()
def markets(
    status: Optional[str] = typer.Option(None, help="Filter: open | closed | settled"),
    limit: int = typer.Option(50, help="Max markets to return"),
    series: Optional[str] = typer.Option(None, "--series", help="Series ticker (e.g. KXBTC)"),
) -> None:
    """
    List Kalshi markets.

    Use --series KXBTC to fetch all markets for a specific series, grouped by
    event, with ticker parsing validation.
    """
    from kbot.kalshi_client import KalshiClient

    if series:
        _markets_by_series(series, status=status, limit=limit)
        return

    with KalshiClient(settings) as client:
        mkts = client.get_markets(status=status, limit=limit)

    table = Table(title=f"Markets ({len(mkts)} returned)")
    table.add_column("Ticker", style="cyan")
    table.add_column("Status")
    table.add_column("Title")
    for m in mkts:
        table.add_row(m.get("ticker", ""), m.get("status", ""), m.get("title", "")[:60])
    console.print(table)


def _markets_by_series(series: str, status: Optional[str], limit: int) -> None:
    """Fetch events for a series and display markets with parser validation."""
    from datetime import datetime, timezone

    from kbot.kalshi_client import KalshiClient
    from kbot.strategy.btc_hourly import _parse_ticker

    with KalshiClient(settings) as client:
        events = client.get_events(series_ticker=series, status=status, limit=limit)

    if not events:
        console.print(f"[yellow]No events found for series {series!r}.[/yellow]")
        return

    total_markets = sum(len(ev.get("markets", [])) for ev in events)
    console.print(
        f"[bold]Series {series}[/bold] — {len(events)} event(s), "
        f"{total_markets} market(s)\n"
    )

    for ev in events:
        ev_ticker  = ev.get("event_ticker", "?")
        ev_title   = ev.get("title", "")[:60]
        ev_markets = ev.get("markets", [])
        console.print(f"[cyan]{ev_ticker}[/cyan]  {ev_title}")

        table = Table(show_header=True, header_style="bold", padding=(0, 1))
        table.add_column("Market Ticker", style="dim cyan")
        table.add_column("yes_ask", justify="right")
        table.add_column("Status")
        table.add_column("Parsed direction")
        table.add_column("Strike")
        table.add_column("Expiry (UTC)")

        for mkt in ev_markets:
            ticker = mkt.get("ticker", "")
            ask    = mkt.get("yes_ask_dollars", "")
            mstatus = mkt.get("status", "")

            parsed = _parse_ticker(ticker)
            if parsed is not None:
                strike, expiry_ts, direction = parsed
                dt_str = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                dir_str = f"[green]{direction}[/green]"
                strike_str = f"${strike:,.0f}" if strike == int(strike) else f"${strike:,.2f}"
            else:
                dir_str = "[red]UNPARSEABLE[/red]"
                strike_str = ""
                dt_str = ""

            table.add_row(ticker, ask or "", mstatus, dir_str, strike_str, dt_str)

        console.print(table)
        console.print()


# ── balance ───────────────────────────────────────────────────────────────────

@app.command()
def balance() -> None:
    """Show portfolio balance (requires credentials)."""
    from kbot.kalshi_client import KalshiClient

    with KalshiClient(settings) as client:
        bal = client.get_balance()
    console.print_json(json.dumps(bal))


# ── stream ────────────────────────────────────────────────────────────────────

@app.command()
def stream(
    ticker: str = typer.Argument(..., help="Market ticker to stream"),
    channels: list[str] = typer.Option(
        ["orderbook_delta", "ticker"], "--channel", "-c", help="WS channels"
    ),
) -> None:
    """Stream live market data for TICKER to stdout."""
    from kbot.ws_client import WSClient

    def on_msg(msg: dict) -> None:
        console.print_json(json.dumps(msg))

    client = WSClient(settings, tickers=[ticker], channels=channels, on_message=on_msg)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


# ── backtest ──────────────────────────────────────────────────────────────────

@app.command()
def backtest(
    strategy_name: str = typer.Option(..., "--strategy", help="Strategy slug (e.g. btc_hourly)"),
    data: Path = typer.Option(..., help="Path to CSV backtest data"),
    bankroll: float = typer.Option(1000.0, help="Starting bankroll in USD"),
) -> None:
    """Replay historical data through a strategy and print metrics."""
    from kbot.backtest import load_csv, run_backtest

    strategy = _load_strategy(strategy_name)
    rows = load_csv(data)
    result = run_backtest(strategy, rows, starting_bankroll=bankroll)

    table = Table(title="Backtest Results")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Trades placed", str(result.total_trades))
    table.add_row("Ticks skipped", str(result.skipped_ticks))
    table.add_row("Invalid price rows skipped", str(result.skipped_invalid_price_rows))
    table.add_row("Win rate", f"{result.win_rate:.1%}")
    table.add_row("Total P&L ($)", f"{result.total_return_pct:.2f}")
    table.add_row("Sharpe (annualised)", f"{result.sharpe:.3f}")
    table.add_row("Max drawdown", f"{result.max_drawdown_pct:.1%}")
    console.print(table)

    fee_table = Table(title="Fee-Tier Report (RULE R4)")
    fee_table.add_column("Price bucket")
    fee_table.add_column("Trades", justify="right")
    fee_table.add_column("Win rate", justify="right")
    fee_table.add_column("Breakeven (taker)", justify="right")
    fee_table.add_column("Breakeven (maker)", justify="right")
    fee_table.add_column("Net P&L ($)", justify="right")
    for row in result.fee_tier_report():
        fee_table.add_row(
            row["label"],
            str(row["trades"]),
            f"{row['win_rate']:.1%}",
            f"{row['breakeven_wr_taker']:.1%}",
            f"{row['breakeven_wr_maker']:.1%}",
            f"{row['net_pnl']:.2f}",
        )
    console.print(fee_table)

    calib_table = Table(title="Calibration Report (model fair_p vs actual win rate)")
    calib_table.add_column("fair_p bucket")
    calib_table.add_column("Trades", justify="right")
    calib_table.add_column("Actual win rate", justify="right")
    calib_table.add_column("Mean ask", justify="right")
    calib_table.add_column("Mean net_edge claimed", justify="right")
    for row in result.calibration_report():
        calib_table.add_row(
            row["label"],
            str(row["trades"]),
            f"{row['win_rate']:.1%}",
            f"{row['mean_ask']:.3f}",
            f"{row['mean_net_edge']:.3f}",
        )
    console.print(calib_table)


# ── paper ─────────────────────────────────────────────────────────────────────

@app.command()
def paper(
    strategy_name: Optional[str] = typer.Option(
        None, "--strategy", help="Single strategy slug -- legacy single-strategy mode (regression path)"
    ),
    strategies: Optional[str] = typer.Option(
        None, "--strategies", help="all|name1,name2 -- multi-strategy mode, reads STRATEGY_ALLOCATIONS"
    ),
    tickers: Optional[list[str]] = typer.Argument(default=None, help="Market tickers (or use --series / auto-discovery)"),
    series: Optional[str] = typer.Option(None, "--series", help="--strategy only: subscribe to nearest active event in this series"),
) -> None:
    """
    Run paper trading on the live feed (dry-run, no real orders).

    --strategy NAME runs exactly one strategy (pass explicit TICKERS or
    --series KXBTC to auto-subscribe to the nearest active event) -- this
    is the regression-guaranteed single-strategy path, unchanged since
    before the multi-strategy runner.

    --strategies all|name1,name2 (default: all) runs every strategy listed
    in STRATEGY_ALLOCATIONS (or the requested subset) through one shared
    kbot.runner.Runner, auto-discovering tickers from each strategy's own
    series unless explicit TICKERS are given.
    """
    if not settings.dry_run:
        typer.echo("ERROR: DRY_RUN must be true for paper trading.", err=True)
        raise typer.Exit(1)

    # Bounded, rotating logs for the long-running service -- the app owns the
    # file so volume can't run away the way raw stdout did. Path comes from
    # settings (env LOG_PATH; default logs/paper.log) so co-tenant services on
    # one host don't interleave into one file and share one rotation budget.
    from kbot.log_config import add_rotating_file_handler

    log_path = Path(settings.log_path)
    add_rotating_file_handler(log_path)
    log.info("paper.logging_to_file", path=str(log_path))

    if strategy_name and strategies:
        typer.echo("ERROR: pass either --strategy or --strategies, not both.", err=True)
        raise typer.Exit(1)

    if strategy_name:
        _run_single_strategy_paper(strategy_name, list(tickers) if tickers else None, series)
        return

    _run_multi_strategy_paper(strategies or "all", list(tickers) if tickers else None)


def _run_single_strategy_paper(strategy_name: str, tickers: list[str] | None, series: str | None) -> None:
    """The regression-guaranteed --strategy path: identical to pre-Phase-B2 behavior."""
    from kbot.kalshi_client import KalshiClient
    from kbot.paper import PaperTrader

    if series:
        with KalshiClient(settings) as client:
            events = client.get_events(series_ticker=series, status="open", limit=5)
        if not events:
            typer.echo(f"ERROR: No open events found for series {series!r}.", err=True)
            raise typer.Exit(1)
        events_sorted = sorted(
            events,
            key=lambda e: e.get("close_time") or e.get("expected_expiration_time") or "",
        )
        nearest = events_sorted[0]
        ev_ticker = nearest.get("event_ticker", "?")
        resolved_tickers = [m.get("ticker", "") for m in nearest.get("markets", []) if m.get("ticker")]
        console.print(
            f"[green]Series {series!r}[/green] — nearest event [cyan]{ev_ticker}[/cyan] "
            f"({len(resolved_tickers)} markets)"
        )
    elif tickers:
        resolved_tickers = list(tickers)
    else:
        typer.echo("ERROR: provide TICKERS or --series.", err=True)
        raise typer.Exit(1)

    strategy = _load_strategy(strategy_name)
    trader = PaperTrader(strategy, resolved_tickers, settings)
    console.print(
        f"[green]Paper trading started[/green] — strategy={strategy_name}, "
        f"tickers={len(resolved_tickers)}"
    )
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    finally:
        trader.close()


def _run_multi_strategy_paper(strategies_arg: str, tickers: list[str] | None) -> None:
    from kbot.runner import Runner

    loaded_strategies, allocations = _resolve_strategies_and_allocations(strategies_arg)
    runner = Runner(loaded_strategies, allocations, settings, tickers=tickers)
    console.print(
        f"[green]Multi-strategy paper trading started[/green] — "
        f"strategies={sorted(loaded_strategies)}, allocations={allocations}"
    )
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    finally:
        runner.close()


# ── run (live) ────────────────────────────────────────────────────────────────

@app.command()
def run(
    strategy_name: Optional[str] = typer.Option(
        None, "--strategy", help="Single strategy slug -- legacy single-strategy mode (regression path)"
    ),
    strategies: Optional[str] = typer.Option(
        None, "--strategies", help="all|name1,name2 -- multi-strategy mode, reads STRATEGY_ALLOCATIONS"
    ),
    tickers: Optional[list[str]] = typer.Argument(
        default=None, help="Market tickers to trade (optional in --strategies mode -- auto-discovered)"
    ),
    live: bool = typer.Option(False, "--live", help="Enable live order placement"),
) -> None:
    """
    Run the bot live.

    Requires --live flag + KALSHI_ENV=prod + DRY_RUN=false in .env
    + interactive confirmation prompt. Without --live, this is paper mode
    (identical to `kbot paper`).

    --strategy NAME is the regression-guaranteed single-strategy path.
    --strategies all|name1,name2 (default: all) runs every strategy in
    STRATEGY_ALLOCATIONS through one shared Runner -- gate level still
    gates which strategies may ever route a real order (see kbot.runner;
    unvetted strategies are skipped even in --live mode).
    """
    if strategy_name and strategies:
        typer.echo("ERROR: pass either --strategy or --strategies, not both.", err=True)
        raise typer.Exit(1)

    if live:
        if settings.kalshi_env != "prod":
            typer.echo(
                "ERROR: --live requires KALSHI_ENV=prod in .env", err=True
            )
            raise typer.Exit(1)
        if settings.dry_run:
            typer.echo(
                "ERROR: --live requires DRY_RUN=false in .env", err=True
            )
            raise typer.Exit(1)

        console.print("[bold red]⚠  LIVE MODE — REAL MONEY AT RISK[/bold red]")
        confirm = typer.prompt("Type CONFIRM LIVE to proceed")
        if confirm != "CONFIRM LIVE":
            typer.echo("Aborted.", err=True)
            raise typer.Exit(1)

        console.print("[green]Live trading started[/green]")
        if strategy_name:
            loaded_strategies = {strategy_name: _load_strategy(strategy_name)}
            allocations = {strategy_name: 1.0}
        else:
            loaded_strategies, allocations = _resolve_strategies_and_allocations(strategies or "all")
        _run_live_loop(loaded_strategies, allocations, list(tickers) if tickers else None)
    elif strategy_name:
        # Default (no --live): paper mode, regression path.
        from kbot.paper import PaperTrader
        strategy = _load_strategy(strategy_name)
        trader = PaperTrader(strategy, list(tickers) if tickers else [], settings)
        try:
            asyncio.run(trader.run())
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/yellow]")
        finally:
            trader.close()
    else:
        _run_multi_strategy_paper(strategies or "all", list(tickers) if tickers else None)


# ── sheets-sync ───────────────────────────────────────────────────────────────

@app.command(name="sheets-sync")
def sheets_sync() -> None:
    """
    Full re-sync from SQLite → Google Sheet.

    Creates any missing tabs (Trade Log, Daily Summary, Calibration,
    Skipped Signals, Research Log), then backfills from the telemetry
    database.  Safe to run repeatedly; clear the Trade Log tab first if
    you want a clean rebuild (running twice will append duplicate rows).
    """
    from kbot.sheets import SheetsClient
    from kbot.telemetry import Telemetry

    with Telemetry(settings) as tel:
        # Scoped so this deployment ensures ITS per-strategy tabs rather than
        # the other one's (the Trade Log / Skipped / Daily Summary tabs this
        # command actually backfills are shared and unaffected).
        client = SheetsClient.for_settings(settings)
        client.sync_all(tel)

    console.print("[green]sheets-sync complete.[/green]")


# ── research ──────────────────────────────────────────────────────────────────

@app.command()
def research(
    once: bool = typer.Option(False, "--once", help="Run all topics once and exit"),
) -> None:
    """
    Run the research / monitoring agent.

    --once  run all topics now and exit (use for cron or testing).
    Without --once, loops continuously every RESEARCH_INTERVAL_HOURS.

    The agent uses the Anthropic API with web search — set ANTHROPIC_API_KEY.
    Findings are written to SQLite and the Research Log sheet tab.
    High-impact findings trigger a critical alert and raise advisory flags
    that cause the run loop to stand down until the flags are cleared.
    """
    import time as _time

    from kbot.alerts import Alerter
    from kbot.research.agent import ResearchAgent
    from kbot.research.flags import update_from_digest
    from kbot.research.sinks import write_digest
    from kbot.sheets import SheetsClient
    from kbot.telemetry import Telemetry

    def _run_once() -> int:
        agent   = ResearchAgent(settings)
        alerter = Alerter(settings)
        sheets  = SheetsClient(settings)
        with Telemetry(settings) as tel:
            digests = agent.run_all()
            for d in digests:
                write_digest(d, telemetry=tel, sheets=sheets, alerter=alerter)
                update_from_digest(d)
        return len(digests)

    if once:
        n = _run_once()
        console.print(f"[green]Research complete — {n} topic(s) processed.[/green]")
        return

    interval = settings.research_interval_hours * 3600
    console.print(
        f"[green]Research agent running[/green] — "
        f"every {settings.research_interval_hours}h  (Ctrl-C to stop)"
    )
    while True:
        try:
            n = _run_once()
            console.print(f"[dim]Research tick: {n} topics[/dim]")
        except Exception as exc:  # noqa: BLE001
            log.error("research.loop.error", error=str(exc)[:300])
        _time.sleep(interval)


# ── learn ─────────────────────────────────────────────────────────────────────

def _current_balance() -> float | None:
    """
    Current Kalshi account balance in dollars, or None if unavailable.

    Best-effort: no credentials, no network, or an API error all mean the Live
    P&L balance column is left empty for rows written this run. That is the
    correct failure mode -- an empty cell is honest, a 0 would read as "the
    account is empty" and make the drift column report the entire cumulative
    P&L as unexplained.
    """
    from kbot.kalshi_client import KalshiClient
    from kbot.live_pnl import balance_dollars

    try:
        with KalshiClient(settings) as client:
            return balance_dollars(client.get_balance())
    except Exception as exc:  # noqa: BLE001
        log.warning("cli.balance_unavailable", error=str(exc)[:200])
        return None


@app.command()
def learn(
    evaluate: bool = typer.Option(False, "--evaluate", help="Run calibration + performance report"),
    retrain:  bool = typer.Option(False, "--retrain",  help="Run champion/challenger and print decision"),
) -> None:
    """
    Self-learning loop commands.

    --evaluate   compute calibration and performance metrics from the
                 trade journal and push to the Calibration sheet tab.
    --retrain    run walk-forward champion/challenger; print the
                 promote/keep decision and validation metrics.
                 Retrain is deliberately manual — do not wire to a frequent
                 schedule without human review of the decision logs.
    """
    if not evaluate and not retrain:
        console.print("[yellow]Pass --evaluate or --retrain (or both).[/yellow]")
        raise typer.Exit(1)

    if evaluate:
        from kbot.learning.evaluate import performance_report, write_report
        from kbot.learning.calibration import reliability_table
        from kbot.sheets import SheetsClient
        from kbot.telemetry import Telemetry

        with Telemetry(settings) as tel:
            report = performance_report(tel, cfg=settings)
            # for_settings scopes Calibration/Performance/Live P&L to the
            # strategy this deployment runs, so BTC and ETH stop overwriting
            # each other in the shared spreadsheet.
            sheets = SheetsClient.for_settings(settings)
            write_report(report, tel, sheets=sheets, balance=_current_balance())

        table = Table(title="Performance Report")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Total trades",   str(report["total_trades"]))
        table.add_row("Wins",           str(report["wins"]))
        table.add_row("Losses",         str(report["losses"]))
        table.add_row("Win rate",       f"{report['win_rate']:.1%}" if report["win_rate"] is not None else "—")
        table.add_row("Net P&L",        f"${report['net_pnl']:.4f}")
        table.add_row("Brier score",    str(report["brier_score"]) or "—")
        table.add_row("Avg pred edge",  f"${report['avg_predicted_edge']:.4f}" if report["avg_predicted_edge"] is not None else "—")
        table.add_row("Avg realised",   f"${report['avg_realized_pnl']:.4f}" if report["avg_realized_pnl"] is not None else "—")
        table.add_row("Edge pred err",  f"${report['edge_prediction_err']:.4f}" if report["edge_prediction_err"] is not None else "—")
        console.print(table)

        if report.get("calibration_buckets"):
            bt = Table(title="Calibration Buckets")
            bt.add_column("Bucket")
            bt.add_column("N", justify="right")
            bt.add_column("Mean pred", justify="right")
            bt.add_column("Realized", justify="right")
            bt.add_column("Brier", justify="right")
            for b in report["calibration_buckets"]:
                bt.add_row(
                    b["bucket_label"], str(b["count"]),
                    f"{b['mean_pred']:.3f}", f"{b['realized_win_rate']:.3f}",
                    f"{b['brier_contribution']:.5f}",
                )
            console.print(bt)

    if retrain:
        from kbot.learning.registry import ModelRegistry
        from kbot.learning.retrain import run_champion_challenger
        from kbot.learning.evaluate import load_outcomes
        from kbot.telemetry import Telemetry

        with Telemetry(settings) as tel:
            outcomes = load_outcomes(tel)

        if len(outcomes) < 10:
            console.print(
                f"[yellow]Only {len(outcomes)} trade outcomes — need ≥10 for "
                "walk-forward evaluation.  Record more trades first.[/yellow]"
            )
            raise typer.Exit(0)

        registry = ModelRegistry()
        result   = run_champion_challenger(outcomes, registry, cfg=settings)
        registry.close()

        table = Table(title=f"Champion/Challenger — Decision: {result['decision'].upper()}")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Champion score",   f"{result['champion_score']:.6f}")
        table.add_row("Challenger score", f"{result['challenger_score']:.6f}")
        table.add_row("Improvement",
                      f"{result['challenger_score'] - result['champion_score']:.6f}")
        table.add_row("Decision",         result["decision"].upper())
        if result["promoted_params"]:
            table.add_row("New kelly_fraction", str(result["promoted_params"]["kelly_fraction"]))
            table.add_row("New min_net_edge",   str(result["promoted_params"]["min_net_edge"]))
        console.print(table)


# ── daemon ────────────────────────────────────────────────────────────────────

@app.command(name="backfill-outcomes")
def backfill_outcomes_cmd() -> None:
    """
    One-time (idempotent) backfill of trade_outcome events for historical
    closed positions that never got one.

    Reads position_open events from telemetry, fetches each ticker's settled
    market from Kalshi, and records won/lost + net P&L. Safe to re-run: any
    (ticker, side) that already has a trade_outcome is skipped. This is what
    populates resolved_paper_trades / the paper gate after the fact.
    """
    from kbot.kalshi_client import KalshiClient
    from kbot.outcomes import backfill_fair_p, backfill_outcomes
    from kbot.telemetry import Telemetry

    with KalshiClient(settings) as client, Telemetry(settings) as tel:
        summary = backfill_outcomes(client, tel, settings)
        # Also carry fair_p forward onto existing outcomes that lack it (older
        # records whose position_open event has no fair_p stay as-is).
        fp = backfill_fair_p(tel)

    table = Table(title="Trade-outcome backfill")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Recorded", str(summary["recorded"]))
    table.add_row("Skipped (already had outcome)", str(summary["skipped_existing"]))
    table.add_row("Skipped (unresolved/no market)", str(summary["skipped_unresolved"]))
    table.add_row("Skipped (incomplete open event)", str(summary["skipped_incomplete"]))
    table.add_row("fair_p added to existing outcomes", str(fp["updated"]))
    table.add_row("fair_p: no source (left as-is)", str(fp["no_source"]))
    console.print(table)
    if summary["per_strategy"]:
        console.print("[bold]Recorded per strategy:[/bold]")
        for strat, n in sorted(summary["per_strategy"].items()):
            console.print(f"  {strat}: {n}")


@app.command(name="reconcile-live")
def reconcile_live_cmd(
    repair_is_live: bool = typer.Option(
        False,
        "--repair-is-live",
        help="Also stamp is_live on existing outcomes whose position_open says "
             "the order was placed with real money. Off by default.",
    ),
    push: bool = typer.Option(
        True, "--push/--no-push", help="Push the Live P&L tab after reconciling.",
    ),
) -> None:
    """
    Reconcile REAL-MONEY trades against Kalshi and refresh the Live P&L tab.

    For every position_open stamped is_live that has no trade_outcome, this
    pulls the market's ACTUAL settled result and realized P&L from Kalshi
    (GET /portfolio/positions) and records it. Unlike `backfill-outcomes`, the
    P&L is what the exchange actually credited -- fees included as charged --
    not the local fee-model estimate.

    Idempotent on (ticker, side): a trade that already has an outcome is left
    untouched, and the Live P&L upsert keys on the same identity, so re-running
    converges instead of duplicating rows.
    """
    from kbot.kalshi_client import KalshiClient
    from kbot.live_pnl import balance_dollars, push_live_pnl
    from kbot.outcomes import reconcile_live_outcomes
    from kbot.sheets import SheetsClient
    from kbot.telemetry import Telemetry

    with KalshiClient(settings) as client, Telemetry(settings) as tel:
        summary = reconcile_live_outcomes(
            client, tel, settings, repair_is_live=repair_is_live,
        )

        upsert: dict[str, int] | None = None
        balance: float | None = None
        if push:
            try:
                balance = balance_dollars(client.get_balance())
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]Balance unavailable: {str(exc)[:120]}[/yellow]")
            # Same scoping helper `learn --evaluate` uses, so both paths write
            # the SAME Live P&L tab and converge on it instead of maintaining
            # two differently-scoped tabs that each look incomplete.
            sheets = SheetsClient.for_settings(settings)
            upsert = push_live_pnl(tel, sheets, balance=balance)

    table = Table(title="Live-trade reconciliation (source: Kalshi positions)")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Recorded (new outcomes)", str(summary["recorded"]))
    table.add_row("Skipped (already had outcome)", str(summary["skipped_existing"]))
    table.add_row("Skipped (not settled at Kalshi yet)", str(summary["skipped_unsettled"]))
    table.add_row("Skipped (incomplete open event)", str(summary["skipped_incomplete"]))
    table.add_row("Existing outcomes mislabelled paper", str(summary["mislabelled_paper"]))
    table.add_row("Repaired is_live", str(summary["repaired_is_live"]))
    console.print(table)

    if summary["per_source"]:
        console.print("[bold]P&L source:[/bold]")
        for source, n in sorted(summary["per_source"].items()):
            console.print(f"  {source}: {n}")

    if summary["mislabelled_paper"] and not repair_is_live:
        console.print(
            "[yellow]Some real-money trades are recorded as paper. "
            "Re-run with --repair-is-live to correct them.[/yellow]"
        )

    if upsert is not None:
        console.print(
            f"[bold]Live P&L tab:[/bold] appended={upsert['appended']} "
            f"updated={upsert['updated']} unchanged={upsert['unchanged']}"
        )
        if balance is not None:
            console.print(f"[bold]Kalshi balance:[/bold] ${balance:,.2f}")


@app.command()
def daemon() -> None:
    """
    Start the scheduled daemon (research + evaluate).

    Runs research every RESEARCH_INTERVAL_HOURS and evaluate every
    LEARN_EVAL_INTERVAL_HOURS in a single long-lived process.

    Windows alternative: use Task Scheduler to run
      kbot research --once
      kbot learn --evaluate
    on their respective intervals without a persistent terminal.
    """
    from kbot.scheduler import start_daemon
    start_daemon(settings)


# ── log ───────────────────────────────────────────────────────────────────────

@app.command(name="log")
def show_log(
    event_type: Optional[str] = typer.Option(None, "--type", help="Filter by event type"),
    limit: int = typer.Option(50, help="Number of events to show"),
) -> None:
    """Show recent events from the telemetry database."""
    from kbot.telemetry import Telemetry

    with Telemetry(settings) as tel:
        events = tel.recent(event_type=event_type, limit=limit)

    from datetime import datetime, timezone

    type_colors = {"signal": "green", "order": "yellow", "heartbeat": "blue", "evaluation": "cyan"}
    for ev in events:
        ts     = datetime.fromtimestamp(ev["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        etype  = ev["event_type"]
        ticker = (ev["ticker"] or "")
        color  = type_colors.get(etype, "white")
        p      = ev["payload"]

        # Build a compact key=value summary from whichever payload fields are present.
        parts: list[str] = []
        for key in ("side", "ask", "fair_p", "net_edge", "contracts", "skip_reason",
                    "btc_price", "vol", "bankroll", "client_order_id"):
            val = p.get(key)
            if val is not None:
                parts.append(f"{key}={val}")

        summary = "  ".join(parts) if parts else json.dumps(p)[:100]
        dry_tag = "" if ev["dry_run"] else " [red][LIVE][/red]"
        console.print(f"[dim]{ts}[/dim]  [{color}]{etype:12s}[/{color}]{dry_tag}  {ticker}  {summary}")


# ── status ────────────────────────────────────────────────────────────────────

@app.command()
def status(
    since: int | None = typer.Option(
        None,
        "--since",
        help="Only count today's events after this UTC hour (0-23).  "
             "E.g. --since 14 shows data since 14:00 UTC today.",
        min=0,
        max=23,
    ),
) -> None:
    """Show live paper-trading health: heartbeat age, today's signal/eval counts."""
    import sqlite3
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    now = time.time()

    # ── Dead-man's switch + watchdog task self-check (2026-07) ──────────────────
    # Independent of the heartbeat display below and of each other: either can
    # fire on its own so a dead bot can't hide behind a task that still says
    # "Running" or a heartbeat check that only warns at a shorter threshold.
    # Same setting the runner writes through (env HEARTBEAT_PATH) -- a
    # hardcoded literal here would report on whichever service happens to own
    # ./kbot_heartbeat.json rather than the one this environment describes.
    hb_path = Path(settings.heartbeat_path)
    if heartbeat_is_stale(hb_path, now):
        console.print(
            "[bold red]!! HEARTBEAT STALE >30min — bot may be down, check watchdog[/bold red]"
        )
    _check_watchdog_task()

    # ── Heartbeat file ────────────────────────────────────────────────────────
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text())
            age_s = now - hb.get("ts", 0)
            age_str = f"{int(age_s // 60)}m {int(age_s % 60)}s ago"
            alive_icon = "[green]OK alive[/green]" if age_s < 600 else "[red]!! STALE[/red]"
            btc_str = f"${hb.get('btc_price', 0):,.0f}" if hb.get("btc_price") else "n/a"
            vol_str = f"{hb.get('vol_annualised', 0)*100:.1f}%" if hb.get("vol_annualised") else "n/a"
            console.print(
                f"\n[bold]kbot paper status[/bold]  ({datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})"
            )
            console.print(f"  Heartbeat:  {age_str}  {alive_icon}")
            console.print(
                f"  BTC:        {btc_str}  |  Vol (annual):{vol_str}"
                f"  |  Books:{hb.get('books_count', '?')}"
                f"  |  Active tickers:{hb.get('active_tickers_count', '?')}"
                f"  |  Bankroll:${hb.get('bankroll', 0):,.2f}"
            )

            # ── Per-strategy blocks (Phase B2 multi-strategy runner) ────────────
            strategies = hb.get("strategies")
            if strategies:
                console.print("\n  [bold]Strategies[/bold]:")
                for name, s in sorted(strategies.items()):
                    if s.get("advisory_paused"):
                        status_icon = "[red]!! ADVISORY PAUSED[/red]"
                    elif s.get("paused"):
                        status_icon = "[yellow]gate paused[/yellow]"
                    else:
                        status_icon = "[green]active[/green]"
                    console.print(
                        f"    {name:<14} alloc:{s.get('allocation', 0):.2f}"
                        f"  gate:{s.get('gate_level', '?')}  {status_icon}"
                    )
                    console.print(
                        f"      eval cycles:{s.get('eval_cycles', 0):,}"
                        f"  |  signals:{s.get('session_signals', 0)}"
                        f"  |  evals logged:{s.get('session_evals_logged', 0):,}"
                    )
            else:
                # Pre-Phase-B2 heartbeat file (flat schema) -- render the old way.
                console.print(
                    f"  Session:    {hb.get('eval_cycles', 0):,} eval cycles"
                    f"  |  signals:{hb.get('session_signals', 0)}"
                    f"  |  evals logged:{hb.get('session_evals_logged', 0):,}"
                )

            driver_exposure = hb.get("driver_exposure") or {}
            if driver_exposure:
                console.print("\n  [bold]Event-driver exposure[/bold] (shared, full bankroll):")
                for driver, dollars in sorted(driver_exposure.items(), key=lambda kv: -kv[1]):
                    console.print(f"    {driver:<20}  ${dollars:,.2f}")
        except (json.JSONDecodeError, KeyError) as exc:
            console.print(f"[yellow]Heartbeat file unreadable: {exc}[/yellow]")
    else:
        console.print(
            "\n[yellow]No heartbeat file found.[/yellow]  "
            "Is [cyan]kbot paper[/cyan] running?  "
            "File appears after the first 5-minute checkpoint."
        )

    # ── Database stats ────────────────────────────────────────────────────────
    # Honours EVENTS_DB_PATH like every other Telemetry consumer -- this was a
    # hardcoded literal, so `kbot status` read the default DB regardless of
    # which deployment's environment it was run under.
    db_path = Path(settings.events_db_path)
    if not db_path.exists():
        console.print("[yellow]No telemetry DB found.[/yellow]")
        return

    conn = sqlite3.connect(str(db_path))
    try:
        # Window start: midnight UTC, or --since hour if provided
        _today_utc = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        since_hour = since if since is not None else 0
        window_start = _today_utc.replace(hour=since_hour).timestamp()

        def _count(event_type: str) -> int:
            return conn.execute(
                "SELECT count(*) FROM events WHERE event_type=? AND ts>=?",
                (event_type, window_start),
            ).fetchone()[0]

        n_signals   = _count("signal")
        n_evals     = _count("evaluation")
        n_orders    = _count("order")
        n_hb        = _count("heartbeat")

        since_label = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%H:%M UTC")
        header_tag  = f"since {since_label} (--since {since})" if since is not None else f"since {since_label}"
        console.print(f"\n  [bold]Today[/bold] ({header_tag}):")
        console.print(f"    Signals fired  (contracts > 0):  {n_signals:,}")
        console.print(f"    Near-miss evals (net_edge > -0.10): {n_evals:,}")
        console.print(f"    Orders executed:                  {n_orders:,}")
        console.print(f"    Heartbeat ticks:                  {n_hb:,}")

        # Best near-miss in window
        best = conn.execute(
            "SELECT ticker, payload FROM events "
            "WHERE event_type IN ('evaluation','signal') AND ts>=? "
            "ORDER BY json_extract(payload,'$.net_edge') DESC LIMIT 1",
            (window_start,),
        ).fetchone()
        if best:
            p = json.loads(best[1])
            console.print(
                f"\n  Best today:  {best[0]}  {p.get('side','')}  "
                f"ask={p.get('ask','')}  fair_p={p.get('fair_p','')}  "
                f"net_edge={p.get('net_edge','')}  contracts={p.get('contracts','')}"
            )

        # Last 5 recent evaluations
        recent = conn.execute(
            "SELECT ts, event_type, ticker, payload FROM events "
            "WHERE event_type IN ('evaluation','signal','order') "
            "ORDER BY id DESC LIMIT 5",
        ).fetchall()
        if recent:
            console.print("\n  [bold]Recent events (last 5):[/bold]")
            for row_ts, row_type, row_ticker, row_payload in recent:
                p = json.loads(row_payload)
                ts_str = datetime.fromtimestamp(row_ts, tz=timezone.utc).strftime("%H:%M:%S")
                ticker_short = (row_ticker or "")[-20:]
                type_style = "[green]signal[/green]" if row_type == "signal" else row_type
                console.print(
                    f"    {ts_str}  {type_style}  {ticker_short}"
                    f"  {p.get('side','')}  ask={p.get('ask','')} "
                    f" net_edge={p.get('net_edge','')} contracts={p.get('contracts','')}"
                )
    finally:
        conn.close()

    # ── Adverse selection ─────────────────────────────────────────────────────
    try:
        from kbot.analytics.adverse_selection import strategy_summary
        from kbot.telemetry import Telemetry

        telem = Telemetry(db_path=db_path)
        try:
            strategies = sorted(telem.distinct_markout_strategies())
            if strategies:
                console.print("\n  [bold]Adverse selection[/bold]:")
                for strat in strategies:
                    s = strategy_summary(telem, strat)
                    avg, neg = s["avg_markout_60s"], s["pct_negative_60s"]
                    avg_str = f"{avg:+.4f}" if avg is not None else "n/a"
                    neg_str = f"{neg * 100:.0f}%" if neg is not None else "n/a"
                    flag_str = "[red]FLAGGED[/red]" if s["flag_state"] else "[green]ok[/green]"
                    console.print(
                        f"    {strat:<20}  avg markout(60s): {avg_str}"
                        f"  |  negative: {neg_str}"
                        f"  |  n={s['sample_size']}"
                        f"  |  {flag_str}"
                    )
        finally:
            telem.close()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Adverse-selection summary unavailable: {exc}[/yellow]")

    console.print()


# ── gates (RULE R3 kill-fast) ────────────────────────────────────────────────

@gates_app.command("status")
def gates_status() -> None:
    """Show every strategy's gate level, pause state, and backtest P&L p5."""
    from kbot.gates import GateRegistry

    with GateRegistry() as registry:
        gates = registry.list_gates()

    if not gates:
        console.print("[yellow]No strategy gates registered yet.[/yellow]")
        return

    table = Table(title="Strategy Gates")
    table.add_column("Strategy")
    table.add_column("Level")
    table.add_column("Paused", justify="center")
    table.add_column("Backtest P&L p5", justify="right")
    table.add_column("Notes")
    for gate in gates:
        p5 = gate.backtest_pnl_p5
        table.add_row(
            gate.strategy,
            gate.level,
            "[red]YES[/red]" if gate.paused else "no",
            f"{p5:.4f}" if p5 is not None else "n/a",
            gate.notes,
        )
    console.print(table)


_FORCE_CONFIRM_PHRASE = "I UNDERSTAND THE RISKS"


@gates_app.command("promote")
def gates_promote(
    strategy: str,
    to: Optional[str] = typer.Option(
        None, "--to",
        help="Assert the target level (one step up, e.g. 'paper'). Promoting to 'paper' enforces the paper-gate check.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Bypass the paper-gate check (requires typing the confirmation phrase).",
    ),
) -> None:
    """
    Manually promote STRATEGY one gate level and clear its kill-fast pause.

    Promotions that land on 'paper' (i.e. from 'backtest') are gated by
    check_paper_promotion: 14+ days since backtest registration, 50+ resolved
    paper trades, positive fee-adjusted paper P&L, no kill-fast trip in the
    last 7 days, and no adverse-selection flag. If any fail, the promotion is
    refused and the failing criteria are listed. --force bypasses the check
    after you type the confirmation phrase.
    """
    from kbot.gates import GateRegistry, LEVELS, check_paper_promotion
    from kbot.telemetry import Telemetry

    with GateRegistry() as registry:
        gate = registry.get_gate(strategy)
        current = gate.level if gate else LEVELS[0]
        idx = LEVELS.index(current) if current in LEVELS else 0
        target = LEVELS[min(len(LEVELS) - 1, idx + 1)]

        if to is not None and to != target:
            typer.echo(
                f"ERROR: --to {to!r} but the next level up from {current!r} is {target!r}. "
                "Only one-step promotion is supported.",
                err=True,
            )
            raise typer.Exit(1)

        # The paper gate: backtest -> paper is the transition that must be earned.
        if target == "paper":
            if force:
                confirm = typer.prompt(
                    f'Type "{_FORCE_CONFIRM_PHRASE}" to force promotion, bypassing the paper-gate check'
                )
                if confirm.strip() != _FORCE_CONFIRM_PHRASE:
                    typer.echo("Confirmation phrase did not match. Promotion aborted.", err=True)
                    raise typer.Exit(1)
                console.print("[yellow]Paper-gate check BYPASSED via --force.[/yellow]")
            else:
                with Telemetry(settings) as tel:
                    result = check_paper_promotion(strategy, registry, tel, settings)
                if not result.passed:
                    console.print(
                        f"[red]Promotion of {strategy!r} to 'paper' REFUSED[/red] — failed criteria:"
                    )
                    for reason in result.failures:
                        console.print(f"  [red]x[/red] {reason}")
                    console.print(
                        "Re-run with [bold]--force[/bold] to override "
                        "(requires typing the confirmation phrase)."
                    )
                    raise typer.Exit(1)
                console.print(f"[green]Paper-gate check PASSED[/green] for {strategy!r}.")

        new_level = registry.promote(strategy)
    console.print(f"[green]Promoted[/green] {strategy!r} -> level {new_level!r}, pause cleared.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_strategy(name: str):
    strategies = {
        "kxbtc15m": _load_kxbtc15m,
        "kxeth15m": _load_kxeth15m,
        "btc_hourly": _load_btc_hourly,
        "base_rate": _load_base_rate,
    }
    if name not in strategies:
        typer.echo(f"Unknown strategy: {name!r}.  Available: {list(strategies)}", err=True)
        raise typer.Exit(1)
    return strategies[name]()


def _load_kxbtc15m():
    """
    The live KXBTC15M strategy: BTCHourlyStrategy scoped to ONLY the
    KXBTC15M series, gated/named as "kxbtc15m" (its own gate entry), with
    the bootstrapped KXBTC15M champion calibrator installed. This is the
    strategy the live paper bot runs (2026-07: btc_hourly retired -- no
    discriminating signal on 60-minute contracts).
    """
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    strategy = BTCHourlyStrategy(settings, series_tickers=["KXBTC15M"], name="kxbtc15m")
    _install_champion_calibrator(strategy, series="KXBTC15M")
    return strategy


def _load_kxeth15m():
    """
    The KXETH15M strategy: ETH15MStrategy (a subclass of BTCHourlyStrategy --
    same lognormal model and sizing path, ETH ticker grammar and eth_spot
    feed), gated/named as "kxeth15m" with its own gate entry.

    NEW as of 2026-07 with NO backtest history and NO fitted calibrator:
    _install_champion_calibrator no-ops unless a champion in THIS deployment's
    registry names series KXETH15M, so it starts on raw lognormal probabilities
    and must earn backtest -> paper on its own forward evidence. Run it as its
    own service with its own EVENTS_DB_PATH/GATES_DB_PATH/REGISTRY_DB_PATH --
    kbot.runner._assert_isolated_domains refuses to co-load it with kxbtc15m
    precisely because a shared registry would cross-contaminate calibrators.
    """
    from kbot.strategy.eth_15m import ETH15MStrategy
    strategy = ETH15MStrategy(settings)
    _install_champion_calibrator(strategy, series="KXETH15M")
    return strategy


def _load_btc_hourly():
    """
    Legacy KXBTC hourly strategy -- RETIRED for live trading (2026-07: the
    lognormal model has no discriminating signal on 60-minute contracts, see
    its gate note). Kept loadable for research/backtests only. Still installs
    the KXBTC15M calibrator harmlessly (series-scoped -- never touches hourly
    tickers) since the class default trades both series.
    """
    from kbot.strategy.btc_hourly import BTCHourlyStrategy
    strategy = BTCHourlyStrategy(settings)
    _install_champion_calibrator(strategy, series="KXBTC15M")
    return strategy


def _install_champion_calibrator(strategy, *, series: str) -> None:
    """
    Install the model registry's champion calibrator for `series` onto
    `strategy`, if one exists. Silent no-op when the registry is absent/empty
    or the champion carries no calibrator -- the strategy then just uses raw
    model probabilities (apply_calibrator is a no-op on None). The champion's
    params must name this series (params["series"] == series) so a calibrator
    fit on one series is never applied to another -- KXBTC15M and KXBTC have
    distinct miscalibration profiles (2026-07 audit).
    """
    try:
        from kbot.learning.registry import ModelRegistry

        registry = ModelRegistry()
        try:
            champion = registry.get_champion()
        finally:
            registry.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("calibrator.load_failed", series=series, error=str(exc)[:120])
        return

    if champion is None or champion.calibrator is None:
        return
    if champion.params.get("series") != series:
        return
    strategy.set_calibrator(champion.calibrator, series=series)
    log.info(
        "calibrator.installed",
        series=series,
        tag=champion.tag,
        version_id=champion.id,
    )


def _load_base_rate():
    from kbot.strategy.base_rate import BaseRateStrategy
    return BaseRateStrategy(settings)


def _resolve_strategies_and_allocations(strategies_arg: str) -> tuple[dict, dict[str, float]]:
    """
    Parse --strategies all|name1,name2 against config.strategy_allocations,
    load each requested strategy, and return (name -> Strategy instance,
    name -> allocation fraction).
    """
    from kbot.config import parse_strategy_allocations

    all_allocations = parse_strategy_allocations(settings.strategy_allocations)

    if strategies_arg.strip().lower() == "all":
        names = list(all_allocations)
    else:
        names = [n.strip() for n in strategies_arg.split(",") if n.strip()]
        unknown = [n for n in names if n not in all_allocations]
        if unknown:
            typer.echo(
                f"ERROR: {unknown} not in STRATEGY_ALLOCATIONS "
                f"({list(all_allocations)}).  Add them there or drop them from --strategies.",
                err=True,
            )
            raise typer.Exit(1)

    loaded = {name: _load_strategy(name) for name in names}
    allocations = {name: all_allocations[name] for name in names}
    return loaded, allocations


def _run_live_loop(strategies: dict, allocations: dict[str, float], tickers: list[str] | None) -> None:
    """
    Internal: wires the live feed loop through kbot.runner.Runner with a
    real (non-dry-run, live_confirmed=True) Executor. Extracted so `run`
    command stays readable.

    Per-strategy gate level still gates which strategies may ever route a
    real order (see Runner._gate_check / _LIVE_APPROVED_LEVELS) -- passing
    an unvetted strategy here does not bypass that; it just gets skipped.
    """
    from kbot.runner import Runner

    runner = Runner(strategies, allocations, settings, tickers=tickers, live_confirmed=True)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Live trading stopped.[/yellow]")
    finally:
        runner.close()
