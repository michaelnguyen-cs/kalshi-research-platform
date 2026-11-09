"""
Pure data analysis (no strategy changes): does KXBTC15M's direction
autocorrelate at 15/30/60-min lags -- i.e. does "up now" predict
"up at settlement N windows later" at a rate meaningfully above 50%?

Run: python scripts/analyze_15m_momentum.py
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scipy import stats  # noqa: E402

from kbot.strategy.btc_hourly import _parse_ticker  # noqa: E402


def load_directions(csv_path: Path) -> list[tuple[float, int]]:
    """(close_time_ts, direction) per unique ticker, sorted chronologically.
    direction = 1 if resolved_yes (BTC ended >= floor_strike, i.e. "up"), else 0."""
    seen: dict[str, tuple[float, int]] = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            ticker = row["ticker"]
            if ticker in seen:
                continue
            parsed = _parse_ticker(ticker)
            if parsed is None:
                continue
            _, expiry_ts, _direction = parsed
            resolved_yes = str(row["resolved_yes"]).strip().lower() in ("1", "true", "yes")
            seen[ticker] = (expiry_ts, int(resolved_yes))
    return sorted(seen.values(), key=lambda x: x[0])


def analyze_lag(series: list[tuple[float, int]], lag: int, window_minutes: int = 15) -> dict:
    """
    Pair (direction[i], direction[i+lag]) only where windows i and i+lag are
    EXACTLY lag*window_minutes apart (strict contiguity -- skip across gaps
    from missing/unresolved windows so a lag "4 steps in the list" doesn't
    silently become "4 steps plus a 2-hour gap").
    """
    expected_gap = lag * window_minutes * 60
    tolerance = 30  # seconds, for float rounding
    pairs = []
    for i in range(len(series) - lag):
        ts_i, dir_i = series[i]
        ts_j, dir_j = series[i + lag]
        if abs((ts_j - ts_i) - expected_gap) <= tolerance:
            pairs.append((dir_i, dir_j))

    n = len(pairs)
    if n == 0:
        return {"lag": lag, "n": 0}

    same = sum(1 for a, b in pairs if a == b)
    continuation_rate = same / n

    # 2x2 contingency: rows = prev direction (down/up), cols = next direction (down/up)
    up_given_up = sum(1 for a, b in pairs if a == 1 and b == 1)
    total_given_up = sum(1 for a, _ in pairs if a == 1)
    up_given_down = sum(1 for a, b in pairs if a == 0 and b == 1)
    total_given_down = sum(1 for a, _ in pairs if a == 0)

    p_up_given_up = up_given_up / total_given_up if total_given_up else float("nan")
    p_up_given_down = up_given_down / total_given_down if total_given_down else float("nan")

    table = [
        [up_given_down, total_given_down - up_given_down],  # prev=down: [next=up, next=down]
        [up_given_up, total_given_up - up_given_up],        # prev=up:   [next=up, next=down]
    ]
    chi2, p_value, _, _ = stats.chi2_contingency(table)

    # One-sample proportion test: is continuation_rate significantly != 0.5?
    binom_p = stats.binomtest(same, n, 0.5, alternative="two-sided").pvalue

    return {
        "lag": lag,
        "n": n,
        "continuation_rate": continuation_rate,
        "p_up_given_prev_up": p_up_given_up,
        "p_up_given_prev_down": p_up_given_down,
        "n_prev_up": total_given_up,
        "n_prev_down": total_given_down,
        "chi2": chi2,
        "chi2_p_value": p_value,
        "binom_p_value": binom_p,
    }


def main() -> None:
    csv_path = Path("data/kxbtc15m_history.csv")
    series = load_directions(csv_path)
    print(f"Loaded {len(series)} distinct resolved KXBTC15M windows, chronologically ordered.")
    up_count = sum(d for _, d in series)
    print(f"Overall base rate: {up_count}/{len(series)} = {up_count/len(series):.4f} resolved 'up'\n")

    print(f"{'lag':>4} {'window':>8} {'n_pairs':>8} {'cont.rate':>10} {'P(up|up)':>9} {'P(up|down)':>11} {'chi2 p':>9} {'binom p':>9}")
    for lag, minutes in ((1, 15), (2, 30), (4, 60)):
        r = analyze_lag(series, lag)
        if r["n"] == 0:
            print(f"{lag:>4} {minutes:>6}m {'no contiguous pairs found':>40}")
            continue
        print(
            f"{lag:>4} {minutes:>6}m {r['n']:>8} {r['continuation_rate']:>10.4f} "
            f"{r['p_up_given_prev_up']:>9.4f} {r['p_up_given_prev_down']:>11.4f} "
            f"{r['chi2_p_value']:>9.4f} {r['binom_p_value']:>9.4f}"
        )
        print(f"       (n_prev_up={r['n_prev_up']}, n_prev_down={r['n_prev_down']})")


if __name__ == "__main__":
    main()
