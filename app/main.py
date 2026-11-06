from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query

from .akshare_client import AkShareClient
from .config import load_settings
from .eltdx_client import EltdxGateway, result_rows
from .models import AdjFactorRequest, AdapterError, BarsRequest, DailyRequest, FinancialRequest
from .report_maps import (
    INCOME_MAP,
    METRICS_MAP,
    REPORT_MAPS,
    _norm_date,
    translate_report_row,
)
from .normalizers import (
    count_for_daily, count_for_period, normalize_adj_factor, normalize_bar,
    normalize_daily, normalize_depth5, normalize_realtime, to_tdx_code,
)

SETTINGS = load_settings()
GATEWAY_URL = SETTINGS.gateway_url
logger = logging.getLogger(__name__)


# ELTDX 报表类型 -> AkShare 报表类型。lyb 是 ELTDX 专属利润表代码, AkShare 无对应,
# 需翻译为 lrb(利润表); zcfzb/xjllb 两边一致。zyzb 无 AkShare 对应, 由各调用方自行兜底。
_ELTDX_TO_AKSHARE_REPORT = {"lyb": "lrb", "zcfzb": "zcfzb", "xjllb": "xjllb"}


class FallbackGateway:
    def __init__(self, primary: EltdxGateway, fallback: AkShareClient | None) -> None:
        self.primary = primary
        self.fallback = fallback

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        primary_params = (
            {key: value for key, value in params.items() if key not in ("start_time", "end_time")}
            if method == "bars.get" else params
        )
        try:
            return await self.primary.call(method, primary_params)
        except AdapterError:
            if self.fallback is None:
                raise
            fb_params = dict(params)
            if method == "f10.finance_report":
                ak_rt = self._ELTDX_TO_AKSHARE_REPORT.get(fb_params.get("report_type"))
                if ak_rt is not None:
                    fb_params["report_type"] = ak_rt
            return await self.fallback.call(method, fb_params)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="market-data tick adapter", version="0.3.0", lifespan=lifespan)
gateway = FallbackGateway(
    EltdxGateway(GATEWAY_URL, SETTINGS.gateway_timeout),
    AkShareClient(timeout=SETTINGS.akshare_timeout) if SETTINGS.akshare_enabled else None,
)


def gateway_error(exc: AdapterError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def wall_clock(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if value is None:
        return None
    return str(value).replace("T", " ").split(" ", 1)[0]


def bar_in_range(row: dict[str, Any], start: datetime | None, end: datetime | None) -> bool:
    value = row.get("datetime") or row.get("date") or row.get("time")
    if value is None:
        return False
    if isinstance(value, datetime):
        timestamp = value
    else:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("T", " ").replace("Z", "+00:00"))
        except ValueError:
            return False
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    start_wall = wall_clock(start)
    end_wall = wall_clock(end)
    return (start_wall is None or timestamp >= start_wall) and (end_wall is None or timestamp <= end_wall)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "gateway": GATEWAY_URL}


@app.get("/realtime")
async def realtime(symbols: str | None = Query(default=None)) -> dict[str, list[dict[str, Any]]]:
    del symbols  # The custom-source contract requires a full-market snapshot.
    try:
        listed = await gateway.call("codes.latest_stock_list", {})
        name_by_code: dict[str, str] = {}
        full_codes: list[str] = []
        for row in result_rows(listed):
            code = row.get("code")
            market = row.get("exchange") or row.get("market")
            if isinstance(code, str) and code.isdigit() and isinstance(market, str):
                full = f"{market.lower()}{code}"
            else:
                full = row.get("full_code") or code
            if not full:
                continue
            full_codes.append(full)
            name = row.get("name")
            if isinstance(name, str) and name:
                name_by_code[full.lower()] = name
        if isinstance(listed, list):
            full_codes.extend(code for code in listed if isinstance(code, str))
        full_codes = list(dict.fromkeys(full_codes))
        result = await gateway.call("helpers.full_quotes", {"codes": full_codes})
    except AdapterError as exc:
        raise gateway_error(exc) from exc
    data: list[dict[str, Any]] = []
    for row in result_rows(result):
        normalized = normalize_realtime(row, pct_unit="percent" if any(row.get(key) is not None for key in ("pct", "percent")) else "decimal")
        if normalized is None:
            continue
        if not normalized.get("name"):
            normalized["name"] = name_by_code.get(to_tdx_code(normalized["symbol"]).lower())
        data.append(normalized)
    return {"data": data}


_PAGE_SIZE = 800  # eltdx bars.get 单页上限 (MAX_KLINE_PAGE_SIZE)
_MAX_PAGES = 10   # 单次请求最多翻 10 页 (=8000 根), 防止病态窗口拖垮适配器


def bar_stamp(row: dict[str, Any]) -> datetime | None:
    """取一根 K 线的时间戳 (北京时间墙钟), 用于判断翻页是否已覆盖窗口起点。"""
    value = (
        row.get("time") or row.get("datetime") or row.get("date")
        or row.get("trade_date") or row.get("date_ms")
    )
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (int, float)):
        stamp = datetime.fromtimestamp(value / 1000, tz=ZoneInfo("Asia/Shanghai"))
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("T", " ").replace("Z", "+00:00"))
        except ValueError:
            return None
    return wall_clock(stamp)


async def fetch_bars(
    code: str,
    period: str,
    adjust: str | None,
    start: datetime | None,
    fallback_count: int,
) -> list[dict[str, Any]]:
    """按 offset 翻页拉取 K 线, 直到覆盖窗口起点。

    eltdx 的 `bars.get` 只接受整数 `start`(offset, 0 = 最新一根) + `count`,
    单页上限 800。窗口起点若在 800 根之前, 必须继续翻页 —— 只取一页再按
    end_time 裁剪会整窗为空, 这是历史回溯永远写 0 行的根因。
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "code": code,
            "period": period,
            "count": fallback_count if start is None else _PAGE_SIZE,
        }
        if offset:
            params["start"] = offset
        if adjust:
            params["adjust"] = adjust
        result = await gateway.call("bars.get", params)
        page_rows = result_rows(result)
        if not page_rows:
            break
        rows.extend(page_rows)
        if start is None or len(page_rows) < _PAGE_SIZE:
            break
        stamps = [stamp for stamp in (bar_stamp(row) for row in page_rows) if stamp is not None]
        if not stamps or min(stamps) <= start:
            break
        offset += len(page_rows)
        if offset >= _PAGE_SIZE * _MAX_PAGES:
            break
    return rows


async def bars(request: BarsRequest) -> dict[str, list[dict[str, Any]]]:
    data: list[dict[str, Any]] = []
    desired = count_for_period(request.period, request.start_time, request.end_time)
    start = wall_clock(request.start_time)
    for symbol in request.symbols:
        try:
            rows = await fetch_bars(
                to_tdx_code(symbol), request.period, None, start, desired
            )
        except (AdapterError, ValueError) as exc:
            if isinstance(exc, AdapterError):
                raise gateway_error(exc) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data.extend(
            normalized for row in rows
            if bar_in_range(row, request.start_time, request.end_time)
            and (normalized := normalize_bar(row, symbol))
        )
    return {"data": data}


@app.post("/daily")
async def daily(request: DailyRequest) -> dict[str, list[dict[str, Any]]]:
    data: list[dict[str, Any]] = []
    start = wall_clock(request.start_time)
    start_date = date_text(request.start_time)
    end_date = date_text(request.end_time)
    fallback_count = count_for_daily(request.start_time, request.end_time)
    for symbol in request.symbols:
        try:
            # eltdx bars.get 无时间窗口参数: 先按 offset 翻页取足量, 再本地裁剪。
            rows = await fetch_bars(
                to_tdx_code(symbol), "day", "none", start, fallback_count
            )
        except (AdapterError, ValueError) as exc:
            if isinstance(exc, AdapterError):
                raise gateway_error(exc) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for row in rows:
            normalized = normalize_daily(row, symbol)
            if normalized is None:
                continue
            trade_date = normalized["date"]
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            data.append(normalized)
    return {"data": data}


@app.post("/adj_factor")
async def adj_factor(request: AdjFactorRequest) -> dict[str, list[dict[str, Any]]]:
    # 除权因子必须是自上市起的完整累积链(面板按 symbol/trade_date/ex_factor 全量消费,
    # 自行做增量/过滤), 因此忽略时间窗口, 始终向网关请求全历史, 不按窗口截断。
    data: list[dict[str, Any]] = []
    for symbol in request.symbols:
        try:
            result = await gateway.call(
                "corporate.adjustment_factors", {"code": to_tdx_code(symbol)}
            )
        except (AdapterError, ValueError) as exc:
            if isinstance(exc, AdapterError):
                raise gateway_error(exc) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for row in result_rows(result):
            normalized = normalize_adj_factor(row, symbol)
            if normalized is None:
                continue
            data.append(normalized)
    return {"data": data}


_TABLE_TO_REPORT = {"income": "lyb", "balance_sheet": "zcfzb", "cash_flow": "xjllb"}


@app.post("/financial")
async def financial(request: FinancialRequest) -> dict[str, list[dict[str, Any]]]:
    table = (table_in := (request.table or "")).lower()
    # 优先按 table 映射到对应报表类型; 仅在未指定 table 时才退回到显式 report_type。
    report_type = _TABLE_TO_REPORT.get(table_in) or request.report_type or "zcfzb"
    data: list[dict[str, Any]] = []
    mapping = REPORT_MAPS.get(table)
    failed: list[str] = []
    # 逐 symbol 容错: 单只失败只跳过该票, 不再让整批 50 只返回 502
    # (面板侧任一批失败会丢弃整张表, 历史上一只票失败就导致整表 0 行)。
    for symbol in request.symbols:
        tdx_code = to_tdx_code(symbol)
        try:
            if table == "shares":
                rows = await _share_rows(gateway, tdx_code, symbol)
            elif table == "metrics":
                rows = await _metrics_rows(gateway, tdx_code, symbol)
            elif table == "income":
                rows = await _income_rows(gateway, tdx_code, symbol)
            else:
                result = await gateway.call(
                    "f10.finance_report", {"code": tdx_code, "report_type": report_type}
                )
                raw_rows = result_rows(result)
                rows = (
                    [translate_report_row(row, mapping, symbol) for row in raw_rows]
                    if mapping is not None
                    else [{"symbol": symbol, **row} for row in raw_rows]
                )
        except (AdapterError, ValueError) as exc:
            failed.append(f"{symbol}: {exc}")
            logger.warning("financial %s %s failed: %s", table, symbol, exc)
            continue
        data.extend(rows)
    if failed and not data:
        raise HTTPException(status_code=502, detail=f"financial {table} failed: {failed[0]}")
    if failed:
        logger.warning(
            "financial %s: %d/%d symbols failed", table, len(failed), len(request.symbols)
        )
    return {"data": data}


async def _share_rows(
    gateway: "FallbackGateway", tdx_code: str, symbol: str
) -> list[dict[str, Any]]:
    """股本表: ELTDX helpers.daily_share_capital (总股本/流通股本), 失败降级为空。"""
    try:
        result = await gateway.call("helpers.daily_share_capital", {"codes": [tdx_code]})
    except AdapterError:
        return []
    return [
        {
            "symbol": symbol,
            "period_end": _norm_date(row.get("trade_date")),
            "total_shares": row.get("total_shares"),
            "float_shares": row.get("circulating_shares"),
        }
        for row in result_rows(result)
    ]


async def _income_rows(
    gateway: "FallbackGateway", tdx_code: str, symbol: str
) -> list[dict[str, Any]]:
    """利润表: 优先 ELTDX lyb, 失败或为空时降级 AkShare 新浪。

    旧实现用 report_type='lrb', 该代码只返回表头 (rtype/nhytype/zqname) 无任何
    财务数据, 曾被误判成「网关没有利润表」。真正的利润表是 'lyb'。
    """
    try:
        result = await gateway.call(
            "f10.finance_report", {"code": tdx_code, "report_type": "lyb"}
        )
        rows = [
            translate_report_row(row, INCOME_MAP, symbol) for row in result_rows(result)
        ]
        rows = [row for row in rows if row.get("period_end")]
        if rows:
            for row in rows:
                row["basic_eps"] = row.get("eps_basic")
                row["diluted_eps"] = row.get("eps_diluted")
            return rows
    except AdapterError as exc:
        logger.warning("income lyb failed for %s: %s", symbol, exc)
    if gateway.fallback is None:
        return []
    rows = await gateway.fallback.financial_income(tdx_code, symbol=symbol)
    for row in rows:
        row["basic_eps"] = row.get("eps_basic")
        row["diluted_eps"] = row.get("eps_diluted")
    return rows


async def _metrics_rows(
    gateway: "FallbackGateway", tdx_code: str, symbol: str
) -> list[dict[str, Any]]:
    """核心指标: 优先 ELTDX zyzb(主要财务指标), 失败时降级 AkShare。"""
    rows: list[dict[str, Any]] = []
    try:
        result = await gateway.call(
            "f10.finance_report", {"code": tdx_code, "report_type": "zyzb"}
        )
        # zyzb 返回 5 个 result_set, 只有含 mgsy 的那张是「主要财务指标」;
        # 其余是成长/偿债等分表, 混在一起会造成同一报告期重复多行。
        sets = (result or {}).get("result_sets") or []
        chosen = next(
            (s for s in sets if "mgsy" in (s.get("columns") or [])),
            sets[0] if sets else {},
        )
        seen: dict[str, dict[str, Any]] = {}
        for raw in chosen.get("rows") or []:
            period = _norm_date(raw.get("T002") or raw.get("rq"))
            if not period:
                continue
            rec = seen.setdefault(
                period,
                {"symbol": symbol, "period_end": period, "announce_date": period},
            )
            for src, dst in METRICS_MAP.items():
                value = _as_float(raw.get(src))
                if value is not None or rec.get(dst) is None:
                    rec[dst] = value
        for _rec in seen.values():
            # 面板 metrics 期望 eps_diluted; zyzb 单季度集无 tbmgsy, 用本行 eps_basic 兜底(摊薄≈基本)
            if _rec.get("eps_diluted") is None:
                _rec["eps_diluted"] = _rec.get("eps_basic")
        rows.extend(seen.values())
    except AdapterError as exc:
        logger.warning("metrics zyzb failed for %s: %s", symbol, exc)
    if rows:
        await _enrich_metrics(gateway, tdx_code, rows)
        return rows
    if gateway.fallback is None:
        return []
    rows = await gateway.fallback.financial_metrics(tdx_code, symbol=symbol)
    _add_derived_ratios(await _aux_ratios(gateway, tdx_code), rows)
    for _rec in rows:
        if _rec.get("eps_diluted") is None:
            _rec["eps_diluted"] = _rec.get("eps_basic")
    return rows


async def _enrich_metrics(
    gateway: "FallbackGateway", tdx_code: str, rows: list[dict[str, Any]]
) -> None:
    """用 lyb / zcfzb / xjllb 补齐 zyzb 缺失的规范字段(营收、归母净利、BPS 与各类比率)。"""
    buckets: dict[str, dict[str, dict[str, Any]]] = {"lyb": {}, "zcfzb": {}, "xjllb": {}}
    for report_type, bucket in buckets.items():
        try:
            result = await gateway.call(
                "f10.finance_report", {"code": tdx_code, "report_type": report_type}
            )
        except AdapterError as exc:
            if report_type == "lyb" and gateway.fallback is not None:
                # ELTDX 利润表(lyb)不可用时, 用 AkShare 新浪利润表(标准化字段)补齐 T008/T030
                try:
                    for r in await gateway.fallback.financial_income(tdx_code):
                        p = r.get("period_end")
                        if p:
                            bucket[p] = {
                                "T008": r.get("revenue"),
                                "T030": r.get("net_income_attributable"),
                            }
                    continue
                except AdapterError as exc2:
                    logger.warning(
                        "metrics enrich lyb fallback failed for %s: %s", tdx_code, exc2
                    )
            logger.warning("metrics enrich %s failed for %s: %s", report_type, tdx_code, exc)
            continue
        for raw in result_rows(result):
            period = _norm_date(raw.get("rq"))
            if period:
                bucket[period] = raw

    for row in rows:
        period = row.get("period_end")
        inc = buckets["lyb"].get(period, {})
        bal = buckets["zcfzb"].get(period, {})
        cash = buckets["xjllb"].get(period, {})
        revenue = _as_float(inc.get("T008"))
        attributable = _as_float(inc.get("T030"))
        total_assets = _as_float(bal.get("T039"))
        total_liabilities = _as_float(bal.get("T062"))
        equity_attributable = _as_float(bal.get("T077"))
        total_shares = _as_float(bal.get("T063"))
        inventory = _as_float(bal.get("T016"))
        operating_cf = _as_float(cash.get("T017"))
        if row.get("revenue") is None:
            row["revenue"] = revenue
        if row.get("net_income_attributable") is None:
            row["net_income_attributable"] = attributable
        row["bps"] = _div(equity_attributable, total_shares)
        row["net_margin"] = _div(row.get("net_income"), revenue)
        row["roa"] = _div(attributable, total_assets)
        row["debt_to_asset_ratio"] = _div(total_liabilities, total_assets)
        row["inventory_turnover"] = _div(_as_float(inc.get("T009")), inventory)
        row["operating_cash_to_revenue"] = _div(operating_cf, revenue)


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _div(numerator: Any, denominator: Any) -> float | None:
    if isinstance(numerator, (int, float)) and isinstance(denominator, (int, float)) and denominator:
        return numerator / denominator
    return None


async def _aux_ratios(gateway: "FallbackGateway", tdx_code: str) -> dict[str, dict[str, Any]]:
    """取总资产(T039)与经营现金流净额(T017)按报告期索引, 供 metrics 跨表派生。失败返回空。"""
    aux: dict[str, dict[str, Any]] = {"total_assets": {}, "operating_cf": {}}
    try:
        bs = await gateway.call("f10.finance_report", {"code": tdx_code, "report_type": "zcfzb"})
        for r in result_rows(bs):
            aux["total_assets"][_norm_date(r.get("rq"))] = r.get("T039")
    except AdapterError:
        pass
    try:
        cf = await gateway.call("f10.finance_report", {"code": tdx_code, "report_type": "xjllb"})
        for r in result_rows(cf):
            aux["operating_cf"][_norm_date(r.get("rq"))] = r.get("T017")
    except AdapterError:
        pass
    return aux


def _add_derived_ratios(aux: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    """为 metrics 行补 roa(归母净利/总资产) 与 operating_cash_to_revenue(经营现金流/营收)。"""
    total_assets = aux.get("total_assets", {})
    operating_cf = aux.get("operating_cf", {})
    for row in rows:
        period = row.get("period_end")
        ni = row.get("net_income_attributable")
        ta = total_assets.get(period)
        row["roa"] = (ni / ta) if (isinstance(ni, (int, float)) and isinstance(ta, (int, float)) and ta) else None
        ocf = operating_cf.get(period)
        rev = row.get("revenue")
        row["operating_cash_to_revenue"] = (
            (ocf / rev) if (isinstance(ocf, (int, float)) and isinstance(rev, (int, float)) and rev) else None
        )


@app.post("/depth5")
async def depth5(request: BarsRequest) -> dict[str, list[dict[str, Any]]]:
    try:
        result = await gateway.call("quotes.get_depth", {"codes": [to_tdx_code(symbol) for symbol in request.symbols]})
    except (AdapterError, ValueError) as exc:
        if isinstance(exc, AdapterError):
            raise gateway_error(exc) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Attribute each row by its own code; rows without a resolvable code are
    # dropped instead of being zipped positionally (gateway order is not guaranteed).
    data = [normalized for row in result_rows(result)
            if (normalized := normalize_depth5(row, None))]
    return {"data": data}


@app.post("/minute")
async def minute(request: BarsRequest) -> dict[str, list[dict[str, Any]]]:
    return await bars(request)


@app.post("/full_minute")
async def full_minute(request: BarsRequest) -> dict[str, list[dict[str, Any]]]:
    return await bars(request)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "3021")), reload=False)
