"""
The recorder has no order path, and it is structural rather than promised.

Three independent barriers, each tested here:

  1. no module under src/threadc imports KalshiClient -- the class that owns
     place_order() and cancel_order() -- so no order-capable object is ever
     constructed or even in scope;
  2. the HTTP layer issues GET only and refuses any path outside a four-entry
     read allowlist, so even a mistaken call cannot reach an order endpoint;
  3. the census adapter that lets depthrec's proven RestCensus run on top of
     that layer exposes exactly two attributes and no trading method.

Also pinned: the recorder never widens beyond the read endpoints in practice,
and nothing in the package writes an order-shaped WebSocket command.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from threadc import frozen, readonly  # noqa: E402
from threadc_fakes import FakeAPI  # noqa: E402

NO_CONFIG_FILE = Path(__file__).with_name("no-such-threadc-config.json")
"""A path that does not exist, so threadc.config.load() builds the configuration
from the overrides alone.

Passing None here would resolve to config/threadc.json and quietly layer these
overrides on top of the committed production file -- including its ticker-only
channel selection, which is a production decision and not this test's subject."""


PACKAGE = frozen.REPO_ROOT / "src" / "threadc"
MODULES = sorted(PACKAGE.rglob("*.py"))

ORDER_SYMBOLS = ("place_order", "cancel_order", "create_order", "amend_order",
                 "decrease_order", "batch_create_orders", "PortfolioApi")


def _code_without_docstrings(path: Path) -> str:
    """Source with docstrings removed, so prose about orders is not a match."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_ids = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
        and isinstance(n.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        if id(node) in doc_ids:
            node.value = ""
    return ast.dump(tree)


def test_no_threadc_module_imports_kalshi_client():
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                assert "KalshiClient" not in names, f"{path} imports KalshiClient"
                assert node.module != "kbot.kalshi_client", f"{path} imports {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "kbot.kalshi_client", f"{path} imports {alias.name}"


def test_no_order_symbol_appears_in_executable_code():
    for path in MODULES:
        code = _code_without_docstrings(path)
        for symbol in ORDER_SYMBOLS:
            assert symbol not in code, f"{path} references {symbol!r}"


ALLOWED_WS_COMMANDS = {"subscribe", "update_subscription"}
"""Every WebSocket command this package may construct.

Kalshi's order path is REST, not WebSocket, so this is not the primary barrier --
the HTTP allowlist and the absence of any client with a place_order() are. It is
the belt to that pair of braces: a recorder that only ever says `subscribe` and
`update_subscription` cannot be turned into one that says anything else by an
edit that slips past review."""


def test_the_only_ws_commands_built_are_subscribe_and_update():
    """
    Thread C now builds its own subscribe (threadc.session), because it banks
    `ticker` + `trade` rather than Thread A's frozen `orderbook_delta`. So the
    check is no longer "no module builds a command" -- it is that every command
    literal any module builds is one of exactly two harmless ones.
    """
    found: set[str] = set()
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "cmd":
                    assert isinstance(value, ast.Constant), (
                        f"{path} builds a WS command from a non-literal"
                    )
                    assert value.value in ALLOWED_WS_COMMANDS, (
                        f"{path} builds WS command {value.value!r}"
                    )
                    found.add(value.value)
    assert found == ALLOWED_WS_COMMANDS


def test_allowlist_is_exactly_the_four_read_endpoints():
    assert [rx.pattern for rx in readonly.ALLOWED_PATHS] == [
        r"^/series$", r"^/series/[^/]+$", r"^/events$", r"^/markets$",
    ]


def test_http_layer_refuses_anything_outside_the_allowlist():
    api = readonly.ReadOnlyAPI.__new__(readonly.ReadOnlyAPI)  # no credentials needed
    for path in ("/portfolio/orders", "/portfolio/orders/abc", "/portfolio/positions",
                 "/exchange/status", "/series/KXMLB/markets", "/markets/KXMLB-M01"):
        with pytest.raises(readonly.ReadOnlyViolation):
            readonly.ReadOnlyAPI.get_raw(api, path)


def test_census_adapter_exposes_no_trading_surface():
    adapter = readonly.CensusClientAdapter(FakeAPI())
    public = {n for n in dir(adapter) if not n.startswith("__")}
    for symbol in ORDER_SYMBOLS:
        assert symbol not in public
    # Exactly what depthrec.census.RestCensus reaches for, and nothing more.
    assert public == {"api", "close", "_read_bucket", "_markets_api"}
    markets_api = {n for n in dir(adapter._markets_api) if not n.startswith("_")}
    assert markets_api == {"get_markets_without_preload_content"}


def test_recorder_only_ever_calls_read_endpoints(tmp_path):
    """
    End to end: after a startup, a market census and a discovery census, every
    path the recorder touched is on the read allowlist.
    """
    import asyncio

    from threadc import config as tc
    from threadc.recorder import ThreadCRecorder
    from threadc_fakes import FakeSession, ample_disk, event, market

    api = FakeAPI(
        catalog=["KXMLBGAME"],
        events={"KXMLBGAME": [event("KXMLBGAME", "Pro Baseball")]},
        markets={"KXMLBGAME": [market("KXMLBGAME", 1)]},
    )
    cfg = tc.load(NO_CONFIG_FILE, overrides={
        "archive_dir": str(tmp_path),
        "collection_end_utc": "2020-01-01T00:00:00Z",
        "disk_reserve_bytes": 10 * 1024**3,
        "discovery_daily_target_utc": "00:00:00",
    })
    rec = ThreadCRecorder(
        cfg, api=api, git_commit="deadbeef",
        session_factory=lambda *, tickers, label: FakeSession(tickers=tickers, label=label),
        disk_check=ample_disk(),
    )
    asyncio.run(rec.run())
    asyncio.run(_discovery(rec))

    assert api.calls, "the recorder made no requests at all"
    for path, _params in api.calls:
        assert any(rx.match(path) for rx in readonly.ALLOWED_PATHS), path


async def _discovery(rec):
    await rec.run_discovery_once()
