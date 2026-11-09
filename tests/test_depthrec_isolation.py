"""
Isolation: the research recorder must not touch the trading bot, and the trading
bot must not be migrated to the research price convention.

Section 6: "The production trading bot is not migrated. It keeps
`use_yes_price: False`. Migrating it as part of this task is unnecessary
coupling and another way to contaminate the research project."

KILL_CRITERIA.md section 2 pins the classifier's two scripts. Those may be
imported and called; they may not be edited.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from depthrec import integrity  # noqa: E402
from depthrec import pinned_hashes as ph  # noqa: E402

REPO = integrity.REPO_ROOT
DEPTHREC = REPO / "src" / "depthrec"


def depthrec_sources() -> list[Path]:
    return sorted(DEPTHREC.glob("*.py"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_recorder_never_imports_the_bots_execution_path():
    """
    Anything that can place an order is out of bounds. This package makes three
    kinds of Kalshi call -- list markets, read candlesticks, open a market-data
    WebSocket -- and no writes.
    """
    forbidden = {"kbot.execution", "kbot.execution_maker", "kbot.runner", "kbot.risk"}
    for path in depthrec_sources():
        assert not (imported_modules(path) & forbidden), path.name


def test_the_recorder_never_calls_an_order_method():
    banned = ("place_order", "cancel_order", "create_order", "batch_create")
    for path in depthrec_sources():
        src = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in src, f"{path.name} mentions {name}"


def test_the_recorder_does_not_import_the_bots_websocket_client():
    """
    kbot.ws_client is frozen on use_yes_price False. Reusing it would either
    silently record the wrong convention or force a migration section 6 forbids.
    """
    for path in depthrec_sources():
        assert "kbot.ws_client" not in imported_modules(path), path.name


def test_the_production_bot_still_uses_the_legacy_convention():
    src = (REPO / "src" / "kbot" / "ws_client.py").read_text(encoding="utf-8")
    assert '"use_yes_price": False' in src, (
        "section 6: the production trading bot is NOT migrated"
    )


def test_the_recorder_is_frozen_on_the_unified_convention():
    src = (DEPTHREC / "session.py").read_text(encoding="utf-8")
    assert '"use_yes_price": self._use_yes_price' in src
    frozen_src = (DEPTHREC / "frozen.py").read_text(encoding="utf-8")
    assert "USE_YES_PRICE = True" in frozen_src


def test_the_pinned_classifier_scripts_are_unmodified():
    """
    The recorder imports these two; it must never have edited them. The study is
    byte-pinned; the longshot script is pinned element-wise because it changed
    after the freeze for unrelated diagnostics.
    """
    report = integrity.verify(
        expected_study=ph.EXPECTED_STUDY,
        expected_longshot_manifest=ph.EXPECTED_LONGSHOT_MANIFEST,
    )
    assert report.ok, report.problems


def test_no_bot_module_imports_the_recorder():
    """The coupling must not run the other way either."""
    for path in sorted((REPO / "src" / "kbot").rglob("*.py")):
        assert not any(
            m == "depthrec" or m.startswith("depthrec.") for m in imported_modules(path)
        ), path
