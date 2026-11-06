from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from .models import AdapterError

logger = logging.getLogger(__name__)


REPORT_TYPES = {"zcfzb": "资产负债表", "lrb": "利润表", "xjllb": "现金流量表"}


def clean(value: Any) -> Any:
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()[:10]
    return value.item() if hasattr(value, "item") else value


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [{str(key): clean(value) for key, value in row.items()} for row in frame.to_dict("records")]


SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(SHANGHAI).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return local_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as exc:
            raise ValueError(f"invalid bar time: {value!r}") from exc
    raise ValueError(f"invalid bar time: {value!r}")


def bar_window(params: dict[str, Any], count: int, fallback_days: int) -> tuple[datetime, datetime]:
    today = datetime.now(SHANGHAI).date()
    end_value = params.get("end_time")
    end = (
        local_datetime(end_value)
        if end_value is not None
        else datetime.combine(today, datetime.max.time()).replace(microsecond=0)
    )
    start_value = params.get("start_time")
    start = (
        local_datetime(start_value)
        if start_value is not None
        else datetime.combine(end.date(), datetime.min.time()) - timedelta(days=fallback_days)
    )
    return start, end


class AkShareClient:
    def __init__(self, api: Any = ak, timeout: float = 30.0) -> None:
        self.api = api
        self.timeout = timeout
        self._snapshot: list[dict[str, Any]] = []
        self._snapshot_at = 0.0

    async def _run(self, function: Any, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            frame = await asyncio.wait_for(asyncio.to_thread(function, **kwargs), self.timeout)
            if frame is None:
                return []
            if not isinstance(frame, pd.DataFrame):
                raise ValueError("AkShare did not return a DataFrame")
            return records(frame)
        except (Exception, asyncio.TimeoutError) as exc:
            raise AdapterError(f"AkShare request failed: {type(exc).__name__}: {exc}") from exc

    async def _spot(self) -> list[dict[str, Any]]:
        if not self._snapshot or time.monotonic() - self._snapshot_at > 15:
            rows = await self._run(self.api.stock_zh_a_spot_tx)
            if not rows:
                raise AdapterError("AkShare returned no realtime quotes")
            self._snapshot = rows
            self._snapshot_at = time.monotonic()
        return self._snapshot

    async def call(self, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if method in ("codes.latest_stock_list", "helpers.full_quotes"):
            spot = await self._spot()
            if method == "codes.latest_stock_list":
                return [{"full_code": row["code"], "name": row.get("name")} for row in spot]
            codes = set(params.get("codes", []))
            quotes = []
            for row in spot:
                code = row.get("code")
                if not code or codes and code not in codes:
                    continue
                try:
                    price, pct = float(row["zxj"]), float(row["zdf"])
                    prev = price / (1 + pct / 100)
                except (ValueError, TypeError, ZeroDivisionError):
                    continue
                quotes.append({"code": code, "name": row.get("name"), "price": price,
                               "pre_close_price": prev, "pct": pct,
                               "volume": float(row["volume"]) if row.get("volume") is not None else None,
                               "amount": float(row["turnover"]) if row.get("turnover") is not None else None})
            return quotes

        code = params.get("code", "")
        symbol = code[2:] if isinstance(code, str) and code[:2] in ("sh", "sz", "bj") else code
        if method == "bars.get":
            period = params.get("period", "day")
            count = int(params.get("count", 800))
            if period == "day":
                start, end = bar_window(params, count, count * 2 + 10)
                rows = await self._run(
                    self.api.stock_zh_a_hist,
                    symbol=symbol,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                )
                field = "日期"
            elif period in ("1m", "5m", "15m", "30m", "60m"):
                start, end = bar_window(params, count, max(5, count // 240 * 2 + 5))
                rows = await self._run(
                    self.api.stock_zh_a_hist_min_em,
                    symbol=symbol,
                    start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                    period=period[:-1],
                    adjust="",
                )
                field = "时间"
            else:
                raise AdapterError(f"unsupported AkShare bar period: {period}")
            return [{"code": code, "date" if period == "day" else "datetime": row[field],
                     "open": row.get("开盘"), "high": row.get("最高"), "low": row.get("最低"),
                     "close": row.get("收盘"), "volume": row.get("成交量"), "amount": row.get("成交额")}
                    for row in rows[-count:]]

        if method == "quotes.get_depth":
            result = []
            for full_code in params["codes"]:
                rows = await self._run(self.api.stock_bid_ask_em, symbol=full_code[2:])
                values = {row["item"]: row["value"] for row in rows}
                def levels(prefix: str) -> list[dict[str, float]]:
                    return [{"price": float(values[f"{prefix}_{i}"]), "volume": float(values[f"{prefix}_{i}_vol"])}
                            for i in range(1, 6) if values.get(f"{prefix}_{i}") is not None
                            and values.get(f"{prefix}_{i}_vol") is not None]
                result.append({"code": full_code, "buy_levels": levels("buy"), "sell_levels": levels("sell")})
            return result

        if method == "corporate.adjustment_factors":
            rows = await self._run(self.api.stock_zh_a_daily, symbol=code, adjust="qfq-factor")
            start = params.get("start_date")
            return [{"date": row["date"], "qfq_scale": float(row["qfq_factor"])} for row in rows
                    if row.get("qfq_factor") is not None and (not start or row["date"] >= start)]

        if method == "f10.finance_report":
            report = REPORT_TYPES.get(params.get("report_type"))
            if report is None:
                raise AdapterError(f"unsupported AkShare report type: {params.get('report_type')}")
            return await self._run(self.api.stock_financial_report_sina, stock=code, symbol=report)
        raise AdapterError(f"unsupported AkShare method: {method}")

    async def financial_metrics(self, code: str, symbol: str | None = None) -> list[dict[str, Any]]:
        """财务指标多期序列(同花顺源, 不受东方财富限流)。

        返回字段经显式映射(见 METRIC_MAP + 元数据字段), 同花顺摘要全量 25 列:
          symbol, period_end, announce_date (元数据)
          + 23 指标列(盈利/每股/盈利能力/运营效率/偿债能力)
        """
        num = code[2:] if len(code) > 2 and code[:2].isalpha() else code
        try:
            frame = await self._run(self.api.stock_financial_abstract_ths, symbol=num)
        except (Exception, asyncio.TimeoutError) as exc:
            logger.warning("AkShare financial_metrics failed for %s: %s", code, exc)
            return []
        if isinstance(frame, list):
            frame = pd.DataFrame(frame)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return []
        period_col = "报告期"
        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            period = row.get(period_col)
            period_end = str(period) if period is not None else None
            if period_end is None:
                continue
            rec: dict[str, Any] = {
                "symbol": symbol,
                "period_end": period_end,
                # 同花顺摘要仅含报告期, 披露日以报告期近似(同季披露, 偏差天数对分析无影响)。
                "announce_date": period_end,
            }
            for cn, en in METRIC_MAP.items():
                if cn in frame.columns:
                    value = _clean_num(row.get(cn))
                    if en not in rec or rec[en] is None:
                        rec[en] = value
            rows.append(rec)
        return rows

    async def financial_income(self, code: str, symbol: str | None = None) -> list[dict[str, Any]]:
        """利润表(新浪源, ELTDX 网关 lrb 恒空): 中文科目 -> 标准字段名。"""
        from .report_maps import INCOME_ZH_MAP, _norm_date

        num = code[2:] if len(code) > 2 and code[:2].isalpha() else code
        try:
            raw = await self._run(self.api.stock_financial_report_sina, stock=num, symbol="利润表")
        except (Exception, asyncio.TimeoutError) as exc:
            logger.warning("AkShare financial_income failed for %s: %s", code, exc)
            return []
        if not raw:
            return []
        rows: list[dict[str, Any]] = []
        for r in raw:
            period_end = _norm_date(r.get("报告日"))
            if period_end is None:
                continue
            ann = _norm_date(r.get("公告日期")) or period_end
            rec: dict[str, Any] = {
                "symbol": symbol,
                "period_end": period_end,
                "announce_date": ann,
            }
            for zh, en in INCOME_ZH_MAP.items():
                value = r.get(zh)
                if isinstance(value, (int, float)):
                    rec[en] = float(value)
                else:
                    rec[en] = _clean_num(value) if value is not None else None
            rows.append(rec)
        return rows

def _clean_num(value: Any) -> Any:
    """清洗同花顺财务指标文本: 去 %, 亿/万/千/万亿换算为绝对数值, 'False'/空 -> None。"""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("False", "None", "", "-", "nan", "NaN"):
        return None
    negative = s.startswith("-")
    s = s.lstrip("-")
    pct = False
    if s.endswith("%"):
        s = s[:-1]
        pct = True
    mult = 1.0
    for suffix, m in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4), ("千", 1e3)):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            mult *= m
            break
    try:
        num = float(s)
    except ValueError:
        return None
    if negative:
        num = -num
    num *= mult
    if pct:
        num /= 100.0
    return num


# metrics 字段映射(同花顺摘要 -> 面板 59 个规范内部字段的子集)
#   仅保留面板规范字段; 非规范字段(资本公积金/流动比率/周转天数/产权比率等)不映射。
#   元数据列(3, 方法内生成): symbol, period_end, announce_date。roa/operating_cash_to_revenue 跨表派生。
METRIC_MAP = {
    # 盈利
    "净利润": "net_income_attributable",
    "扣非净利润": "net_income_deducted",
    "净利润同比增长率": "net_income_yoy",
    "营业总收入": "revenue",
    "营业总收入同比增长率": "revenue_yoy",
    # 每股
    "基本每股收益": "eps_basic",
    "每股净资产": "bps",
    "每股经营现金流": "ocfps",
    # 盈利能力
    "销售净利率": "net_margin",
    "销售毛利率": "gross_margin",
    "净资产收益率": "roe",
    "净资产收益率-摊薄": "roe_diluted",
    # 运营/偿债(规范字段)
    "存货周转率": "inventory_turnover",
    "资产负债率": "debt_to_asset_ratio",
}
