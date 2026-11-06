"""财务报表明细字段映射(严格对齐 tick-stock-panel 59 个规范内部字段)。

- 资产负债表/现金流量表: ELTDX 网关返回通达信 T 科目编码, 此处翻译为标准字段名;
- 利润表: ELTDX 网关 report_type=lyb (lrb 只是表头占位, 无数据), T 编码映射;
- 主要财务指标: ELTDX 网关 report_type=zyzb, 列名已是拼音缩写, 直接映射;
- 映射依据: 多只股票(含高负债/有商誉)与新浪财报按同报告期数值精确匹配投票确认。
只保留面板规范字段, 非规范字段(流动比率/周转天数/资本公积金等)一律不输出。
"""
from __future__ import annotations

from typing import Any

# 资产负债表 (zcfzb) T 编码 -> 标准字段名 (全部为面板规范字段)
BALANCE_SHEET_MAP: dict[str, str] = {
    "T007": "cash_and_equivalents",      # 货币资金
    "T010": "accounts_receivable",       # 应收账款
    "T016": "inventory",                 # 存货
    "T020": "total_current_assets",      # 流动资产合计
    "T026": "fixed_assets",              # 固定资产
    "T032": "intangible_assets",         # 无形资产
    "T034": "goodwill",                  # 商誉
    "T038": "total_non_current_assets",  # 非流动资产合计
    "T039": "total_assets",              # 资产总计
    "T040": "short_term_borrowing",      # 短期借款
    "T043": "accounts_payable",          # 应付账款
    "T053": "total_current_liabilities",     # 流动负债合计
    "T054": "long_term_borrowing",           # 长期借款
    "T061": "total_non_current_liabilities", # 非流动负债合计
    "T062": "total_liabilities",             # 负债合计
    "T063": "total_shares",                  # 实收资本(或股本)
    "T067": "retained_earnings",             # 未分配利润
    "T068": "minority_interest",             # 少数股东权益
    "T071": "total_equity",                  # 所有者权益合计
    "T077": "equity_attributable",           # 归属于母公司股东权益合计
}

# 现金流量表 (xjllb) T 编码 -> 标准字段名
CASH_FLOW_MAP: dict[str, str] = {
    "T017": "net_operating_cash_flow",  # 经营活动产生的现金流量净额
    "T029": "net_investing_cash_flow",  # 投资活动产生的现金流量净额
    "T038": "net_financing_cash_flow",  # 筹资活动产生的现金流量净额
    "T041": "net_cash_change",          # 现金及现金等价物净增加额
    "T024": "capex",                    # 购建固定资产、无形资产等支付的现金
}

# 利润表(新浪中文科目) -> 标准字段名 (仅面板规范字段)
INCOME_ZH_MAP: dict[str, str] = {
    "营业收入": "revenue",
    "营业成本": "operating_cost",
    "营业利润": "operating_profit",
    "营业外收入": "non_operating_income",
    "营业外支出": "non_operating_expense",
    "利润总额": "total_profit",
    "所得税费用": "income_tax",
    "净利润": "net_income",
    "归属于母公司所有者的净利润": "net_income_attributable",
    "销售费用": "selling_expense",
    "管理费用": "admin_expense",
    "研发费用": "rd_expense",
    "财务费用": "financial_expense",
    "基本每股收益": "eps_basic",
    "稀释每股收益": "eps_diluted",
}

# 利润表 (lyb) T 编码 -> 标准字段名
# 依据: 600519.SH 2026-09-30 报告期与 AkShare 新浪利润表逐字段比对, 13/13 完全一致。
INCOME_MAP: dict[str, str] = {
    "T008": "revenue",                  # 营业收入
    "T009": "operating_cost",           # 营业成本
    "T020": "operating_profit",         # 营业利润
    "T022": "non_operating_income",     # 营业外收入
    "T023": "non_operating_expense",    # 营业外支出
    "T026": "total_profit",             # 利润总额
    "T027": "income_tax",               # 所得税费用
    "T029": "net_income",               # 净利润
    "T030": "net_income_attributable",  # 归属于母公司股东的净利润
    "T011": "selling_expense",          # 销售费用
    "T012": "admin_expense",            # 管理费用
    "T061": "rd_expense",               # 研发费用
    "T014": "financial_expense",        # 财务费用
}

# 主要财务指标 (zyzb) 列名 -> 面板 metrics 规范字段
METRICS_MAP: dict[str, str] = {
    "mgsy": "eps_basic",               # 每股收益
    "kfjlr": "net_income_deducted",    # 扣非净利润
    "mgxjll": "ocfps",                 # 每股经营现金流
    "jyr": "net_income",               # 净利润
    "jzzsyl": "roe",                   # 净资产收益率
    "pjjzcsyl": "roe_diluted",         # 平均净资产收益率(近似稀释 ROE)
    "xsmll": "gross_margin",           # 销售毛利率
    "jlrtbzzl": "net_income_yoy",      # 净利润同比增长
    "yysrtb": "revenue_yoy",           # 营业收入同比
}

REPORT_MAPS: dict[str, dict[str, str]] = {
    "balance_sheet": BALANCE_SHEET_MAP,
    "cash_flow": CASH_FLOW_MAP,
    "income": INCOME_MAP,
}

# tick-stock-panel 前端固化财务内部字段(59 个), 用于 field_map 严格对齐。
CANONICAL_FINANCIAL_FIELDS: list[str] = [
    "symbol", "period_end", "announce_date",
    "eps_basic", "eps_diluted", "bps", "ocfps", "roe", "roe_diluted", "roa",
    "gross_margin", "net_margin", "debt_to_asset_ratio",
    "revenue_yoy", "net_income_yoy", "operating_cash_to_revenue", "inventory_turnover",
    "revenue", "operating_cost", "operating_profit", "selling_expense", "admin_expense",
    "rd_expense", "financial_expense", "non_operating_income", "non_operating_expense",
    "total_profit", "income_tax", "net_income", "net_income_attributable", "net_income_deducted",
    "basic_eps", "diluted_eps",
    "total_assets", "total_current_assets", "total_non_current_assets",
    "cash_and_equivalents", "accounts_receivable", "inventory", "fixed_assets",
    "intangible_assets", "goodwill",
    "total_liabilities", "total_current_liabilities", "total_non_current_liabilities",
    "short_term_borrowing", "long_term_borrowing", "accounts_payable",
    "total_equity", "equity_attributable", "retained_earnings", "minority_interest",
    "net_operating_cash_flow", "net_investing_cash_flow", "net_financing_cash_flow",
    "capex", "net_cash_change",
    "total_shares", "float_shares",
]


def _norm_date(value: Any) -> str | None:
    """把 20260930 / 2026-09-30 / 2026-09-30 00:00:00 统一成 YYYY-MM-DD。"""
    if value is None:
        return None
    s = str(value).strip().replace("/", "-").split(" ")[0]
    digits = s.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return s or None


def translate_report_row(row: dict[str, Any], mapping: dict[str, str], symbol: str) -> dict[str, Any]:
    """T 编码行 -> 标准字段名行。period_end 取 rq, announce_date 以 rq 近似。"""
    period_end = _norm_date(row.get("rq"))
    out: dict[str, Any] = {
        "symbol": symbol,
        "period_end": period_end,
        "announce_date": period_end,
    }
    for tcode, std in mapping.items():
        value = row.get(tcode)
        out[std] = float(value) if isinstance(value, (int, float)) else None
    return out
