"""
Tests for Subsystem 2 — research agent isolation and advisory flag wiring.

Two hard requirements:
  1. The research/ package must never import from kbot.execution (architecture test).
  2. A raised advisory flag must prevent Executor.execute() from placing an order.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kbot.risk as risk_module
from kbot.config import Settings
from kbot.execution import Executor
from kbot.kalshi_client import KalshiClient, OrderAck
from kbot.models.types import Side, SizingResult
from kbot.research.flags import (
    active_flags,
    any_advisory_flag_set,
    clear_all_flags,
    get_flag,
    set_flag,
    update_from_digest,
)
from kbot.research.schema import Finding, ResearchDigest, RiskFlag

# ── Helpers ───────────────────────────────────────────────────────────────────

RESEARCH_DIR = Path(__file__).parent.parent / "src" / "kbot" / "research"


def _cfg(**kw) -> Settings:
    return Settings(
        kalshi_key_id="test",
        kalshi_env=kw.get("kalshi_env", "demo"),
        dry_run=kw.get("dry_run", True),
        min_net_edge=kw.get("min_net_edge", 0.04),
    )


def _result(contracts=5, net_edge_dollars=0.05) -> SizingResult:
    return SizingResult(
        ticker="T1",
        side=Side.YES,
        contracts=contracts,
        price_dollars=0.60,
        net_edge_dollars=net_edge_dollars,
        kelly_fraction_used=0.25,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset all module-level state before and after every test."""
    risk_module._KILL_SWITCH_TRIPPED = False
    clear_all_flags()
    yield
    clear_all_flags()
    risk_module._KILL_SWITCH_TRIPPED = False


# ── Architecture: research/ must not import execution ─────────────────────────

class TestResearchIsolation:
    def _imports_in_file(self, path: Path) -> list[str]:
        """Return every dotted module name imported in a Python source file."""
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        return names

    def test_no_research_module_imports_execution(self):
        """
        Architecture gate: every .py file under research/ must be free of any
        import that references 'execution'.  Uses AST so comments don't count.
        """
        violations: list[str] = []
        for py_file in sorted(RESEARCH_DIR.glob("*.py")):
            for imported in self._imports_in_file(py_file):
                if "execution" in imported:
                    violations.append(f"{py_file.name}: imports '{imported}'")

        assert not violations, (
            "research/ package must NEVER import from execution:\n"
            + "\n".join(violations)
        )

    def test_flags_module_has_no_trading_imports(self):
        """flags.py must not import from edge, risk, or kalshi_client either."""
        forbidden = {"edge", "risk", "kalshi_client", "execution"}
        flags_file = RESEARCH_DIR / "flags.py"
        for imported in self._imports_in_file(flags_file):
            parts = set(imported.split("."))
            overlap = parts & forbidden
            assert not overlap, (
                f"flags.py imports '{imported}' which references {overlap}"
            )


# ── Advisory flags: basic state management ───────────────────────────────────

class TestFlagState:
    def test_all_flags_clear_on_startup(self):
        assert not any_advisory_flag_set()

    def test_set_flag_raises_advisory(self):
        set_flag("api_change_detected", True)
        assert get_flag("api_change_detected")
        assert any_advisory_flag_set()

    def test_clear_all_flags_resets_state(self):
        set_flag("exchange_incident", True)
        set_flag("high_impact_event_imminent", True)
        clear_all_flags()
        assert not any_advisory_flag_set()

    def test_active_flags_returns_set_names(self):
        set_flag("api_change_detected", True)
        set_flag("exchange_incident", True)
        names = active_flags()
        assert "api_change_detected" in names
        assert "exchange_incident" in names
        assert "high_impact_event_imminent" not in names

    def test_set_flag_false_clears_individual(self):
        set_flag("api_change_detected", True)
        set_flag("api_change_detected", False)
        assert not get_flag("api_change_detected")


# ── update_from_digest: correct flag mapping ──────────────────────────────────

class TestUpdateFromDigest:
    def _digest(self, findings=None, risk_flags=None, topic="kalshi_api_changes"):
        from datetime import datetime, timezone
        return ResearchDigest(
            topic=topic,
            generated_utc=datetime.now(tz=timezone.utc),
            summary="test",
            findings=findings or [],
            risk_flags=risk_flags or [],
        )

    def test_high_impact_finding_sets_high_impact_flag(self):
        d = self._digest(
            findings=[Finding(claim="BTC drops 10%", impact="high")],
            topic="macro_crypto_context",
        )
        update_from_digest(d)
        assert get_flag("high_impact_event_imminent")

    def test_low_impact_finding_does_not_set_flag(self):
        d = self._digest(
            findings=[Finding(claim="minor update", impact="low")],
            topic="macro_crypto_context",
        )
        update_from_digest(d)
        assert not any_advisory_flag_set()

    def test_risk_flag_with_api_sets_api_flag(self):
        d = self._digest(
            risk_flags=[RiskFlag(flag="api_breaking_change", action="halt")]
        )
        update_from_digest(d)
        assert get_flag("api_change_detected")

    def test_risk_flag_with_outage_sets_exchange_flag(self):
        d = self._digest(
            risk_flags=[RiskFlag(flag="exchange_outage_detected", action="pause")]
        )
        update_from_digest(d)
        assert get_flag("exchange_incident")

    def test_api_topic_high_impact_sets_api_flag(self):
        d = self._digest(
            findings=[Finding(claim="endpoint removed", impact="high")],
            topic="kalshi_api_changes",
        )
        update_from_digest(d)
        assert get_flag("api_change_detected")


# ── Advisory flag blocks Executor.execute() ───────────────────────────────────

class TestAdvisoryFlagBlocksExecution:
    @pytest.fixture()
    def mock_client(self, mocker):
        client = mocker.MagicMock(spec=KalshiClient)
        # fill_count echoes the request: an ack with 0 fills means the
        # immediate_or_cancel order traded nothing and no position opens.
        client.place_order.side_effect = (
            lambda *, ticker, side, count, price_dollars, **kw: OrderAck(
                client_order_id="cid-1", order_id="oid-1", dry_run=True,
                fill_count_units=count * 100, remaining_count_units=0, average_fill_price=price_dollars,
            )
        )
        return client

    def test_high_impact_flag_blocks_order(self, mock_client):
        set_flag("high_impact_event_imminent", True)
        executor = Executor(mock_client, _cfg())
        ack = executor.execute(_result())
        assert ack is None
        mock_client.place_order.assert_not_called()

    def test_api_change_flag_blocks_order(self, mock_client):
        set_flag("api_change_detected", True)
        executor = Executor(mock_client, _cfg())
        ack = executor.execute(_result())
        assert ack is None
        mock_client.place_order.assert_not_called()

    def test_exchange_incident_flag_blocks_order(self, mock_client):
        set_flag("exchange_incident", True)
        executor = Executor(mock_client, _cfg())
        ack = executor.execute(_result())
        assert ack is None
        mock_client.place_order.assert_not_called()

    def test_no_flag_allows_order(self, mock_client):
        """Regression: clear flags must not block a valid order."""
        executor = Executor(mock_client, _cfg())
        ack = executor.execute(_result())
        assert ack is not None
        mock_client.place_order.assert_called_once()

    def test_cleared_flag_allows_order(self, mock_client):
        """After clearing a raised flag, orders must proceed again."""
        set_flag("api_change_detected", True)
        clear_all_flags()
        executor = Executor(mock_client, _cfg())
        ack = executor.execute(_result())
        assert ack is not None
        mock_client.place_order.assert_called_once()


# ── ResearchDigest schema ─────────────────────────────────────────────────────

class TestResearchDigestSchema:
    def _now(self):
        from datetime import datetime, timezone
        return datetime.now(tz=timezone.utc)

    def test_has_high_impact_true(self):
        d = ResearchDigest(
            topic="t", generated_utc=self._now(), summary="s",
            findings=[Finding(claim="x", impact="high")]
        )
        assert d.has_high_impact()

    def test_has_high_impact_false(self):
        d = ResearchDigest(
            topic="t", generated_utc=self._now(), summary="s",
            findings=[Finding(claim="x", impact="low")]
        )
        assert not d.has_high_impact()

    def test_is_actionable_on_risk_flag(self):
        d = ResearchDigest(
            topic="t", generated_utc=self._now(), summary="s",
            risk_flags=[RiskFlag(flag="some_flag", action="act")]
        )
        assert d.is_actionable()

    def test_is_actionable_false_when_empty(self):
        d = ResearchDigest(topic="t", generated_utc=self._now(), summary="s")
        assert not d.is_actionable()


# ── sinks: write_digest routes correctly ─────────────────────────────────────

class TestSinks:
    def _high_digest(self):
        from datetime import datetime, timezone
        return ResearchDigest(
            topic="kalshi_api_changes",
            generated_utc=datetime.now(tz=timezone.utc),
            summary="Breaking change detected.",
            findings=[Finding(claim="endpoint removed", source_url="https://example.com", impact="high")],
            risk_flags=[RiskFlag(flag="api_breaking_change", action="halt trading")],
            recommended_human_action="Halt the bot and review the API changelog.",
        )

    def _low_digest(self):
        from datetime import datetime, timezone
        return ResearchDigest(
            topic="exchange_operational_status",
            generated_utc=datetime.now(tz=timezone.utc),
            summary="No issues found.",
        )

    def test_write_digest_calls_db(self, mocker):
        from kbot.research.sinks import write_digest
        mock_tel = mocker.MagicMock()
        write_digest(self._high_digest(), telemetry=mock_tel)
        mock_tel.log_research_digest.assert_called_once()

    def test_write_digest_calls_sheets(self, mocker):
        from kbot.research.sinks import write_digest
        mock_sheets = mocker.MagicMock()
        write_digest(self._high_digest(), sheets=mock_sheets)
        mock_sheets.append_research.assert_called_once()

    def test_write_digest_alerts_on_high_impact(self, mocker):
        from kbot.research.sinks import write_digest
        mock_alerter = mocker.MagicMock()
        write_digest(self._high_digest(), alerter=mock_alerter)
        mock_alerter.send_critical.assert_called_once()

    def test_write_digest_no_alert_on_low_impact(self, mocker):
        from kbot.research.sinks import write_digest
        mock_alerter = mocker.MagicMock()
        write_digest(self._low_digest(), alerter=mock_alerter)
        mock_alerter.send_critical.assert_not_called()

    def test_write_digest_sink_failure_does_not_raise(self, mocker):
        from kbot.research.sinks import write_digest
        mock_tel = mocker.MagicMock()
        mock_tel.log_research_digest.side_effect = RuntimeError("db down")
        write_digest(self._high_digest(), telemetry=mock_tel)  # must not raise
