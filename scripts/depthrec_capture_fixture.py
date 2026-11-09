#!/usr/bin/env python
"""
Capture the classifier regression fixture from live REST. Run once; commit the
result.

    python scripts/depthrec_capture_fixture.py --write

The fixture holds REAL Kalshi 1-minute candlestick JSON for settled
KXBTC15M/KXETH15M markets, plus each market's real settlement, plus the firing
set the PILOT's own pipeline produces from that data. tests/
test_depthrec_classifier.py then requires three things to agree:

  1. the pilot's pipeline over the fixture
         market_calibration_study.sample_market
      -> market_calibration_study.write_csv        (one batched CSV, as the
      -> test_longshot_rule.load_observations       pilot wrote calib_full.csv)
      -> test_longshot_rule.qualifying(0.70, 7)
  2. depthrec.classify, market by market, with settlement UNKNOWN
  3. the firing set committed in this fixture

(1) vs (2) is what proves the adapter has not changed what the imported
classifier sees. (3) is what stops both from drifting together: a committed
expectation cannot be re-derived by the code it is checking.

The pilot path is given each market's REAL settled outcome; the adapter path is
given the placeholder it uses live, because at T-7 the outcome does not exist
yet. Their agreeing is therefore also a demonstration on real data that the
placeholder cannot reach the firing decision.

READ-ONLY. Three read methods, no writes to Kalshi.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from depthrec import classify, frozen  # noqa: E402
from kbot.config import settings  # noqa: E402
from kbot.kalshi_client import KalshiClient  # noqa: E402

import market_calibration_study as mcs  # noqa: E402
import test_longshot_rule as tls  # noqa: E402
from pull_history import resolved_yes_from_market  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "depthrec_classifier_pilot.json"

_PER_SERIES = 40
"""Markets pulled per series. Enough that the fixture straddles the threshold in
both directions and includes NO-side favorites; small enough to read."""


def parse_iso(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def capture(client: KalshiClient) -> list[dict]:
    out: list[dict] = []
    for series in frozen.CENSUS_SERIES:
        client._read_bucket.acquire()
        raw = client._markets_api.get_markets_without_preload_content(
            series_ticker=series, status="settled", limit=_PER_SERIES
        )
        payload = json.loads(raw.read())
        for market in payload.get("markets", []):
            ticker = market.get("ticker")
            close_ts = parse_iso(market.get("close_time"))
            if not ticker or close_ts is None:
                continue
            settled_yes = resolved_yes_from_market(market)
            if settled_yes is None:
                continue
            target_ts = close_ts - frozen.TARGET_OFFSET_SECONDS
            try:
                candles = client.get_candlesticks(
                    series, ticker,
                    start_ts=close_ts - frozen.CENSUS_CANDLE_LEAD_SECONDS,
                    end_ts=target_ts,
                    period_interval=1,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {ticker}: candles failed: {exc}")
                continue
            if not candles:
                continue
            out.append({
                "series": series,
                "ticker": ticker,
                "close_ts": close_ts,
                "target_ts": target_ts,
                "settled_yes": bool(settled_yes),
                "candles": candles,
            })
            time.sleep(0.25)
        print(f"  [{series}] captured {sum(1 for m in out if m['series'] == series)} markets")
    return out


def pilot_firing(markets: list[dict], tmp_csv: Path) -> list[dict]:
    """
    The pilot's own pipeline over the whole fixture, in one batched CSV -- which
    is how the pilot actually ran (one calib_full.csv over every market).
    """
    study_obs = []
    for m in markets:
        study_obs.extend(mcs.sample_market(
            m["series"], m["ticker"], m["close_ts"], m["candles"],
            m["settled_yes"], [frozen.RULE_SLICE_MINUTES],
        ))
    mcs.write_csv(tmp_csv, study_obs, classify.FEE_COEFFICIENT, tls._CONTRACTS_PER_ORDER)
    loaded = tls.load_observations(tmp_csv)
    fired = tls.qualifying(loaded, frozen.RULE_THRESHOLD, frozen.RULE_SLICE_MINUTES)
    rows = []
    for obs in fired:
        side, entry = tls.favorite_side(obs.yes_price)
        rows.append({
            "ticker": obs.ticker, "series": obs.series, "yes_price": obs.yes_price,
            "price_source": obs.price_source, "side": side, "entry_price": entry,
        })
    return sorted(rows, key=lambda r: r["ticker"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    client = KalshiClient(settings)
    try:
        markets = capture(client)
    finally:
        client.close()
    if not markets:
        print("captured nothing", file=sys.stderr)
        return 1

    tmp_csv = FIXTURE.parent / "_capture_tmp.csv"
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    try:
        expected = pilot_firing(markets, tmp_csv)
    finally:
        tmp_csv.unlink(missing_ok=True)

    prices = sorted(
        p for p in (
            mcs.yes_price_from_candle(
                mcs.candle_at_or_before(mcs.sort_candles(m["candles"]), m["target_ts"]) or {}
            )[0] for m in markets
        ) if p is not None
    )
    payload = {
        "captured_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "doc": "research/THREAD_A_EXECUTION_MECHANICS_2026-08-22.md @ e7c8c87",
        "threshold": frozen.RULE_THRESHOLD,
        "slice_minutes": frozen.RULE_SLICE_MINUTES,
        "note": (
            "Real Kalshi 1-minute candles for settled Thread A markets. "
            "expected_firing is what the PILOT's own pipeline "
            "(sample_market -> write_csv -> load_observations -> qualifying) "
            "selects from `markets`, computed at capture time and committed so "
            "it cannot be re-derived from the code under test."
        ),
        "markets": markets,
        "expected_firing": expected,
    }

    print(f"markets: {len(markets)}  firing: {len(expected)}")
    if prices:
        print(f"T-7 price range: {prices[0]:.4f} .. {prices[-1]:.4f}")
    print(f"favorite sides fired: "
          f"YES={sum(1 for r in expected if r['side'] == 'YES')} "
          f"NO={sum(1 for r in expected if r['side'] == 'NO')}")

    if args.write:
        FIXTURE.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
    else:
        print("(dry run; pass --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
