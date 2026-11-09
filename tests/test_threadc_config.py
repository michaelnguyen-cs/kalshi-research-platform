"""
Thread C configuration: the values that must not be guessed, and the one that
must never move backwards.

The load-bearing properties here are all refusals. The collection end may be
extended and never shortened; the reserve R and the discovery-census interval
have no defaults because neither can be chosen honestly yet; and a collection
end without a timezone is rejected rather than read as local time, because a
frozen end date that silently moves by the host's UTC offset is not frozen.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from threadc import config as tc  # noqa: E402
from threadc import frozen  # noqa: E402

NO_CONFIG_FILE = Path(__file__).with_name("no-such-threadc-config.json")
"""A path that does not exist, so threadc.config.load() builds the configuration
from the overrides alone.

Passing None here would resolve to config/threadc.json and quietly layer these
overrides on top of the committed production file -- including its ticker-only
channel selection, which is a production decision and not this test's subject."""


BASE = {
    "collection_end_utc": "2026-11-05T12:00:00Z",
    "disk_reserve_bytes": 10 * 1024**3,
    "discovery_daily_target_utc": "09:00:00",
}


def cfg_for(tmp_path: Path, **kw):
    return tc.load(NO_CONFIG_FILE, overrides={"archive_dir": str(tmp_path), **BASE, **kw})


def test_initial_collection_end_is_configuration_not_code(tmp_path):
    """
    The initial 2026-11-05T12:00:00Z lives in config/threadc.json, not in any
    module. Changing it must not require a code change.
    """
    shipped = json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.json").read_text(encoding="utf-8")
    )
    assert shipped["collection_end_utc"] == "2026-11-05T12:00:00Z"

    # Checked against literals the code actually evaluates, not against prose:
    # the modules discuss the date in their docstrings, which is documentation,
    # not a hardcoded value. A string constant carrying it anywhere else would
    # mean the end date had leaked out of configuration and into code.
    for path in (frozen.REPO_ROOT / "src" / "threadc").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                assert "2026-11-05" not in node.value, (
                    f"{path}:{node.lineno} hardcodes the collection end date"
                )


def test_the_reserve_has_no_default(tmp_path):
    """B_1h has not been measured, so R cannot be computed and is not invented."""
    over = {"archive_dir": str(tmp_path), **BASE}
    over.pop("disk_reserve_bytes")
    with pytest.raises(tc.ConfigError, match="disk_reserve_bytes"):
        tc.load(NO_CONFIG_FILE, overrides=over)


def test_the_discovery_target_falls_back_to_the_frozen_default(tmp_path):
    """
    Unlike the reserve, the cadence IS resolved: once per UTC day at 09:00. The
    production config states it explicitly; the frozen constant is the fallback
    so the capacity config, where discovery is disabled outright, still parses.
    """
    over = {"archive_dir": str(tmp_path), **BASE}
    over.pop("discovery_daily_target_utc")
    cfg = tc.load(NO_CONFIG_FILE, overrides=over)
    assert cfg.discovery_daily_target_utc == frozen.DISCOVERY_DAILY_TARGET_UTC == "09:00:00"

    with pytest.raises(tc.ConfigError, match="discovery_daily_target_utc"):
        tc.load(NO_CONFIG_FILE, overrides={**over, "discovery_daily_target_utc": "25:00"})


def test_discovery_is_disabled_by_mode_in_capacity_runs(tmp_path):
    over = {"archive_dir": str(tmp_path), **BASE}
    assert tc.load(NO_CONFIG_FILE, overrides=over).discovery_enabled is True
    capacity = tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides=over)
    assert capacity.discovery_enabled is False
    # Not a config key: nothing an operator can put in a file switches it on.
    assert "discovery_enabled" not in capacity.raw


def test_shipped_production_config_is_complete_and_loads(tmp_path):
    """
    The committed production config no longer carries a null. Both values that
    were deliberately unsupplied have been supplied from measurement: the
    reserve from the Branch-2 capacity hour, and the channel selection from the
    branch that measurement chose.
    """
    cfg = tc.load(
        frozen.REPO_ROOT / "config" / "threadc.json",
        overrides={"archive_dir": str(tmp_path)},
    )
    assert cfg.mode == frozen.MODE_PRODUCTION
    assert cfg.disk_reserve_bytes == 12841254912
    assert cfg.channels == ("ticker",)


def test_the_shipped_reserve_is_48x_the_branch_2_measurement():
    """
    R = max(10 GiB, 48 x B_1h) evaluated on the ticker-only hour -- the branch
    production actually collects on -- and not on Branch 1's 373,444,608, which
    would size the reserve for a selection this config does not run.
    """
    shipped = json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.json").read_text(encoding="utf-8")
    )
    branch_2_b1h = 267_526_144
    assert shipped["disk_reserve_bytes"] == max(
        tc.MIN_DISK_RESERVE_BYTES, 48 * branch_2_b1h
    ) == 12_841_254_912
    assert shipped["disk_reserve_bytes"] != 48 * 373_444_608


def test_reserve_floor_is_enforced(tmp_path):
    with pytest.raises(tc.ConfigError, match="10 GiB"):
        cfg_for(tmp_path, disk_reserve_bytes=1024)


def test_naive_collection_end_is_refused(tmp_path):
    with pytest.raises(tc.ConfigError, match="no timezone"):
        cfg_for(tmp_path, collection_end_utc="2026-11-05 12:00:00")


def test_collection_end_may_move_later(tmp_path):
    first = cfg_for(tmp_path)
    rec = tc.enforce_monotonic_collection_end(first)
    assert rec["change"] == "collection_end_set"

    same = tc.enforce_monotonic_collection_end(cfg_for(tmp_path))
    assert same["change"] == "unchanged"

    later = cfg_for(tmp_path, collection_end_utc="2026-11-12T12:00:00Z")
    rec2 = tc.enforce_monotonic_collection_end(later)
    assert rec2["change"] == "collection_end_extended"
    assert rec2["previous_collection_end_utc"] == "2026-11-05T12:00:00Z"


def test_collection_end_may_never_move_earlier(tmp_path):
    tc.enforce_monotonic_collection_end(cfg_for(tmp_path))
    earlier = cfg_for(tmp_path, collection_end_utc="2026-10-01T00:00:00Z")
    with pytest.raises(tc.ConfigError, match="EARLIER"):
        tc.enforce_monotonic_collection_end(earlier)


def test_every_accepted_change_is_logged(tmp_path):
    tc.enforce_monotonic_collection_end(cfg_for(tmp_path))
    tc.enforce_monotonic_collection_end(
        cfg_for(tmp_path, collection_end_utc="2026-11-30T12:00:00Z")
    )
    lines = (tmp_path / tc.CONFIG_CHANGE_LOG_FILE).read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(x) for x in lines]
    assert records[0]["change"] == "collection_end_set"
    assert records[1]["change"] == "collection_end_extended"
    assert all("rule" in r for r in records)


# -- Channel selection ---------------------------------------------------------
#
# Both branches of the precommitted fallback hierarchy have been measured over
# the same bracketed hour, the same universe and the same storage schema:
# Branch 1 (ticker+trade) wrote 373,444,608 bytes/hour, Branch 2 (ticker only)
# wrote 267,526,144. Branch 1 does not fit the storage available for the
# collection window, so production selects Branch 2 -- explicitly, because the
# ABSENCE of the key means the frozen pair, which is Branch 1.
#
# The load-bearing property here is still a refusal: the key can subtract from
# frozen.CHANNELS and can never add to it, in either mode.


def test_no_channels_key_means_the_frozen_pair(tmp_path):
    """
    Absence never narrows anything, in either mode. It is also not neutral --
    the frozen pair is Branch 1 -- which is exactly why the production config
    states its selection instead of relying on this default.
    """
    over = {"archive_dir": str(tmp_path), **BASE}
    assert tc.CHANNELS_KEY not in over

    production = tc.load(NO_CONFIG_FILE, overrides=over)
    assert production.channels == frozen.CHANNELS == ("ticker", "trade")
    assert production.channels_are_frozen_pair is True
    assert production.summary()["channels"] == ["ticker", "trade"]

    capacity = tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides=over)
    assert capacity.channels == frozen.CHANNELS

    # An explicit null is the same as absent, not an empty selection.
    assert tc.load(NO_CONFIG_FILE, overrides={**over, "channels": None}).channels == frozen.CHANNELS


def test_production_may_select_ticker_only(tmp_path):
    cfg = cfg_for(tmp_path, channels=["ticker"])
    assert cfg.mode == frozen.MODE_PRODUCTION
    assert cfg.channels == ("ticker",)
    assert cfg.channels_are_frozen_pair is False
    assert cfg.summary()["channels"] == ["ticker"]
    assert cfg.summary()["channels_are_frozen_pair"] is False
    # frozen.CHANNELS itself is untouched by any selection.
    assert frozen.CHANNELS == ("ticker", "trade")


def test_shipped_production_config_selects_ticker_only(tmp_path):
    """
    The committed production file says which branch it collects on, so starting
    production is pointing the recorder at it -- no edit, no flag, no code
    change, and no silent revert to the branch the measurement rejected.
    """
    shipped = json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.json").read_text(encoding="utf-8")
    )
    assert shipped[tc.CHANNELS_KEY] == ["ticker"]

    cfg = tc.load(
        frozen.REPO_ROOT / "config" / "threadc.json",
        overrides={"archive_dir": str(tmp_path)},
    )
    assert cfg.channels == ("ticker",)
    assert cfg.channels_are_frozen_pair is False


def test_the_shipped_capacity_config_still_requests_branch_2(tmp_path):
    shipped = json.loads(
        (frozen.REPO_ROOT / "config" / "threadc.capacity.json").read_text(encoding="utf-8")
    )
    assert shipped[tc.CHANNELS_KEY] == ["ticker"]

    cfg = tc.load(
        frozen.REPO_ROOT / "config" / "threadc.capacity.json",
        mode=frozen.MODE_CAPACITY,
        overrides={"archive_dir": str(tmp_path)},
    )
    assert cfg.channels == ("ticker",)


def test_a_capacity_config_can_still_select_ticker_only(tmp_path):
    cfg = tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides={
        "archive_dir": str(tmp_path), **BASE, "channels": ["ticker"],
    })
    assert cfg.channels == ("ticker",)
    assert cfg.channels_are_frozen_pair is False


def test_the_selection_can_only_subtract_from_the_frozen_pair(tmp_path):
    """
    A key that could ADD a channel would be a fidelity widening dressed as
    configuration -- and orderbook_delta is precisely the design the storage
    budget already rejected. Production can now select, so this refusal has to
    hold in production mode as much as in a measurement.
    """
    def production(**kw):
        return cfg_for(tmp_path, **kw)

    def capacity(**kw):
        return tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides={
            "archive_dir": str(tmp_path), **BASE, **kw,
        })

    for load_one in (production, capacity):
        with pytest.raises(tc.ConfigError, match="not one of the frozen channels"):
            load_one(channels=["ticker", "orderbook_delta"])
        with pytest.raises(tc.ConfigError, match="not one of the frozen channels"):
            load_one(channels=["orderbook_delta"])
        with pytest.raises(tc.ConfigError, match="not one of the frozen channels"):
            load_one(channels=["ticker", "trade", "orderbook_snapshot"])
        with pytest.raises(tc.ConfigError, match="non-empty list"):
            load_one(channels=[])
        with pytest.raises(tc.ConfigError, match="non-empty list"):
            load_one(channels="ticker")
        with pytest.raises(tc.ConfigError, match="more than once"):
            load_one(channels=["ticker", "ticker"])
        with pytest.raises(tc.ConfigError, match="not a channel name"):
            load_one(channels=["ticker", 7])


def test_naming_the_whole_frozen_pair_is_the_frozen_pair(tmp_path):
    """Selecting everything is allowed and is not a widening; it is the default."""
    cfg = cfg_for(tmp_path, channels=["ticker", "trade"])
    assert cfg.channels == frozen.CHANNELS
    assert cfg.channels_are_frozen_pair is True


def test_the_selection_is_normalized_to_the_frozen_order(tmp_path):
    """A set, not an ordering decision: two ways of writing it archive alike."""
    assert cfg_for(tmp_path, channels=["trade", "ticker"]).channels == frozen.CHANNELS
    assert tc.load(NO_CONFIG_FILE, mode=frozen.MODE_CAPACITY, overrides={
        "archive_dir": str(tmp_path), **BASE, "channels": ["trade", "ticker"],
    }).channels == frozen.CHANNELS


def test_a_widening_is_still_only_a_code_change():
    """
    The ceiling did not move when production gained the ability to select. What
    the bank may preserve at most is still the frozen tuple, and nothing in a
    config file reaches it.
    """
    assert frozen.CHANNELS == ("ticker", "trade")
    for name in ("orderbook_delta", "orderbook_snapshot", "fill", "market_lifecycle"):
        assert name not in frozen.CHANNELS
