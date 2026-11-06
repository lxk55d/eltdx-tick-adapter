from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any


_PERIOD_COUNTS = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}


def canonical_symbol(value: Any) -> str | None:
    """把任意格式的 symbol 归一为面板存储键 (600519.SH / 000001.SZ)。

    上游 bars 行内不带 code 时, normalizers 会用请求里的 symbol 兜底回显;
    若请求是裸符号 (TSP v0.3.28 修复轮会这么发), 原样回显会把同一只票写成
    两个不同的 symbol, 分裂分钟/日线分区。这里统一收敛。
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().upper()
    if "." in raw:
        code, market = raw.split(".", 1)
        market = {"SHA": "SH", "SHE": "SZ", "BJA": "BJ"}.get(market, market)
        if code.isdigit() and market in {"SH", "SZ", "BJ"}:
            return f"{code.zfill(6)}.{market}"
        return raw
    if raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
        return f"{raw[2:]}.{raw[:2]}"
    if raw.isdigit() and len(raw) == 6:
        market = "SH" if raw[0] in "569" else "BJ" if raw[0] in "48" else "SZ"
        return f"{raw}.{market}"
    return None


def to_tdx_code(symbol: str) -> str:
    raw = symbol.upper().strip()
    # 前缀式 (SH600519) —— 旧契约
    if raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
        return raw.lower()
    # 裸符号 (600519) —— TSP v0.3.28 修复轮按首位数字推断交易所
    if raw.isdigit() and len(raw) == 6:
        market = "SH" if raw[0] in "569" else "BJ" if raw[0] in "48" else "SZ"
        return f"{'sh' if market == 'SH' else 'bj' if market == 'BJ' else 'sz'}{raw}"
    # 后缀式 (600519.SH / 600519.SHA) —— TSP v0.3.28 起的新契约
    if "." in raw:
        code, market = raw.split(".", 1)
        market = {"SHA": "SH", "SHE": "SZ", "BJA": "BJ"}.get(market, market)
        prefix = {"SZ": "sz", "SH": "sh", "BJ": "bj"}.get(market)
        if prefix is not None and code.isdigit():
            return f"{prefix}{code}"
    raise ValueError(f"unsupported symbol: {symbol}")


def from_tdx_code(value: Any, market: Any = None) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.lower()
    for prefix, suffix in (("sz", "SZ"), ("sh", "SH"), ("bj", "BJ")):
        if raw.startswith(prefix) and raw[len(prefix):].isdigit():
            return f"{raw[len(prefix):].zfill(6)}.{suffix}"
    if raw.isdigit() and isinstance(market, str):
        suffix = {"sz": "SZ", "sh": "SH", "bj": "BJ"}.get(market.lower())
        if suffix is not None:
            return f"{raw.zfill(6)}.{suffix}"
    if value.isdigit() and len(value) == 6:
        suffix = "SH" if value.startswith(("5", "6", "9")) else "BJ" if value.startswith(("4", "8")) else "SZ"
        return f"{value}.{suffix}"
    return value if "." in value else None

def number(row: dict[str, Any], *names: str) -> float | int | None:
    for name in names:
        value = row.get(name)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def normalize_realtime(row: dict[str, Any], *, pct_unit: str = "decimal") -> dict[str, Any] | None:
    symbol = from_tdx_code(
        row.get("symbol") or row.get("code") or row.get("ts_code") or row.get("thscode"),
        row.get("exchange") or row.get("market"),
    )
    last = number(row, "last_price", "price", "last", "close")
    prev = number(row, "prev_close", "prevClose", "prev_price", "pre_close", "pre_close_price", "yesterday_close")
    if symbol is None or last is None or prev is None:
        return None
    result: dict[str, Any] = {
        "symbol": symbol,
        "name": row.get("name") or row.get("stock_name"),
        "last_price": last,
        "prev_close": prev,
        "open": number(row, "open", "open_price"),
        "high": number(row, "high", "high_price"),
        "low": number(row, "low", "low_price"),
        "volume": number(row, "volume", "vol", "total_hand"),
        "amount": number(row, "amount", "amt", "turnover"),
        "change_amount": number(row, "change_amount", "change", "price_change"),
        "amplitude": number(row, "amplitude"),
        "turnover_rate": number(row, "turnover_rate", "turnover_pct"),
    }
    pct = number(row, "change_pct", "price_change_ratio_pct", "changePercent")
    if pct is None:
        pct = number(row, "pct", "percent")
        if pct_unit == "percent" and pct is not None:
            pct /= 100
    elif pct_unit == "percent":
        pct /= 100
    if pct is None and prev:
        pct = (last - prev) / prev
    result["change_pct"] = pct
    for field in ("amplitude", "turnover_rate"):
        if pct_unit == "percent" and result[field] is not None:
            result[field] /= 100
    if result["change_amount"] is None:
        result["change_amount"] = last - prev
    return result


def normalize_daily(row: dict[str, Any], fallback_symbol: str) -> dict[str, Any] | None:
    symbol = (
        from_tdx_code(
            row.get("symbol") or row.get("code") or row.get("ts_code"),
            row.get("exchange") or row.get("market"),
        )
        or canonical_symbol(fallback_symbol)
        or fallback_symbol
    )
    date_value = row.get("date") or row.get("trade_date") or row.get("datetime") or row.get("time") or row.get("date_ms")
    if date_value is None:
        return None
    if isinstance(date_value, (int, float)):
        date = datetime.fromtimestamp(date_value / 1000, tz=ZoneInfo("Asia/Shanghai")).date().isoformat()
    elif isinstance(date_value, datetime):
        date = date_value.date().isoformat()
    else:
        date = str(date_value).replace("T", " ").split(" ", 1)[0]
    return {
        "symbol": symbol,
        "date": date,
        "open": number(row, "open", "open_price"),
        "high": number(row, "high", "high_price"),
        "low": number(row, "low", "low_price"),
        "close": number(row, "close", "close_price", "last"),
        "volume": number(row, "volume", "vol", "total_hand", "volume_lots", "volume_wire_value"),
        "amount": number(row, "amount", "amt", "turnover"),
    }


def normalize_adj_factor(row: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    trade_date = row.get("date") or row.get("trade_date")
    ex_factor = number(row, "qfq_scale", "ex_factor")
    if trade_date is None or ex_factor is None:
        return None
    if isinstance(trade_date, datetime):
        trade_date = trade_date.date().isoformat()
    else:
        trade_date = str(trade_date).replace("T", " ").split(" ", 1)[0]
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "ex_factor": ex_factor,
    }


def normalize_bar(row: dict[str, Any], fallback_symbol: str) -> dict[str, Any] | None:
    symbol = (
        from_tdx_code(
            row.get("symbol") or row.get("code") or row.get("ts_code"),
            row.get("exchange") or row.get("market"),
        )
        or canonical_symbol(fallback_symbol)
        or fallback_symbol
    )
    timestamp = row.get("datetime") or row.get("date") or row.get("time")
    if timestamp is None:
        return None
    if isinstance(timestamp, datetime):
        dt = timestamp
    else:
        text = str(timestamp).replace("T", " ").replace("Z", "")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return {
        "symbol": symbol,
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "open": number(row, "open"),
        "high": number(row, "high"),
        "low": number(row, "low"),
        "close": number(row, "close", "last"),
        "volume": number(row, "volume", "vol", "total_hand", "volume_lots"),
        "amount": number(row, "amount", "amt", "turnover", "volume_wire_value"),
    }


def normalize_depth5(row: dict[str, Any], fallback_symbol: str) -> dict[str, Any] | None:
    symbol = (
        from_tdx_code(
            row.get("symbol") or row.get("code") or row.get("ts_code"),
            row.get("exchange") or row.get("market"),
        )
        or canonical_symbol(fallback_symbol)
        or fallback_symbol
    )

    def levels(value: Any) -> list[list[float]]:
        if not isinstance(value, list):
            return []
        normalized: list[list[float]] = []
        for level in value:
            if isinstance(level, dict):
                price = number(level, "price", "p")
                volume = number(level, "volume", "vol", "v")
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = number({"value": level[0]}, "value")
                volume = number({"value": level[1]}, "value")
            else:
                continue
            if price is not None and volume is not None:
                normalized.append([price, volume])
        return normalized

    if symbol is None:
        return None
    return {
        "symbol": symbol,
        "asks": levels(row.get("sell_levels") or row.get("asks") or row.get("ask")),
        "bids": levels(row.get("buy_levels") or row.get("bids") or row.get("bid")),
    }


def count_for_period(period: str, start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None or end <= start:
        return _PERIOD_COUNTS[period]
    minutes = max(1, int((end - start).total_seconds() / 60))
    # eltdx bars.get rejects page sizes above MAX_KLINE_PAGE_SIZE (800).
    return min(800, max(_PERIOD_COUNTS[period], minutes // int(period[:-1]) + 2))


def count_for_daily(start: datetime | None, end: datetime | None) -> int:
    # 上游 bars.get 单次 page size 上限为 800，超出会直接被拒绝。
    if start is None or end is None or end <= start:
        return 800
    days = max(1, (end.date() - start.date()).days + 1)
    return min(800, max(30, days * 2))
