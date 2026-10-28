"""Normalize Kalshi API records into the DataFrames the backtester uses.

trades:  ts (UTC datetime), ticker, price (YES price in dollars), count, taker_side ("yes"/"no")
markets: ticker, event_ticker, series_ticker, open_time, close_time, result, fee_type
"""

from pathlib import Path

import pandas as pd

TRADE_COLUMNS = ["ts", "ticker", "price", "count", "taker_side"]
MARKET_COLUMNS = ["ticker", "event_ticker", "series_ticker", "open_time", "close_time", "result", "fee_type"]


def _dollars(record: dict, name: str) -> float | None:
    """Read a price as dollars, accepting the current "_dollars" strings or older integer cents."""
    if record.get(f"{name}_dollars") not in (None, ""):
        return float(record[f"{name}_dollars"])
    if record.get(name) is not None:
        return record[name] / 100
    return None


def normalize_trades(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        if r.get("is_block_trade"):
            continue  # block trades never touch the order book, so they can't fill our resting orders
        rows.append(
            {
                "ts": pd.Timestamp(r["created_time"]),
                "ticker": r["ticker"],
                "price": _dollars(r, "yes_price"),
                "count": float(r.get("count_fp") or r.get("count")),
                "taker_side": r.get("taker_outcome_side") or r.get("taker_side"),
            }
        )
    df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def normalize_markets(records: list[dict], series_ticker: str, fee_type: str = "quadratic") -> pd.DataFrame:
    rows = [
        {
            "ticker": r["ticker"],
            "event_ticker": r["event_ticker"],
            "series_ticker": series_ticker,
            "open_time": pd.Timestamp(r["open_time"]),
            "close_time": pd.Timestamp(r["close_time"]),
            "result": r.get("result") or None,
            "fee_type": fee_type,
        }
        for r in records
    ]
    df = pd.DataFrame(rows, columns=MARKET_COLUMNS)
    for col in ("open_time", "close_time"):
        df[col] = pd.to_datetime(df[col], utc=True)
    return df


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def load_dataset(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load every series under root/<series>/{markets,trades}.parquet."""
    markets = [pd.read_parquet(p) for p in sorted(root.glob("*/markets.parquet"))]
    trades = [pd.read_parquet(p) for p in sorted(root.glob("*/trades.parquet"))]
    if not markets:
        raise FileNotFoundError(f"No collected data under {root}")
    trades_df = pd.concat(trades, ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)
    return pd.concat(markets, ignore_index=True), trades_df
