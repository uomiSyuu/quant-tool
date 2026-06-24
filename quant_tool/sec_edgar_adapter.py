# -*- coding: utf-8 -*-
"""
SEC EDGAR XBRL 数据适配器 v9.0
=================================
数据源: SEC EDGAR XBRL API (data.sec.gov)
完全免费、无Key、限10次/秒

数据流:
  1. CIK 映射: sec.gov/files/company_tickers.json
  2. 公司事实: data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
  3. 解析 XBRL → 标准财务字段
  4. 衍生计算: TTM EPS, ROE, ROIC, FCF Yield 等

US-GAAP 概念映射:
  - 营收: RevenueFromContractWithCustomerExcludingAssessedTax (首选)
          或 Revenues (备选)
  - 净利润: NetIncomeLoss
  - EPS: EarningsPerShareDiluted
  - 资产: Assets
  - 负债: Liabilities
  - 权益: StockholdersEquity
"""

import requests, json, os, time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ====== 常量 ======
SEC_BASE = "https://data.sec.gov"
CIK_URL = "https://www.sec.gov/files/company_tickers.json"
USER_AGENT = "QuantViz/9.0 (quantviz-research@protonmail.com)"

# 缓存目录
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".sec_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CIK_CACHE_FILE = os.path.join(CACHE_DIR, "cik_map.json")
FACTS_CACHE_TTL = 3600 * 24  # 24小时（SEC数据变化不频繁）
LAST_REQUEST_TIME = 0

# ====== XBRL 概念映射表 ======
# 格式: {标准字段: [优先概念名, 备选概念名1, ...]}
CONCEPT_MAP = {
    # === 利润表 ===
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "Revenue"
    ],
    "total_cost": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
    ],
    "gross_profit": ["GrossProfit"],
    "rd_expense": [
        "ResearchAndDevelopmentExpense",
    ],
    "sga_expense": [
        "SellingGeneralAndAdministrativeExpense",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "ebit": ["EarningsBeforeInterestAndTaxes", "OperatingIncomeLoss"],
    "ebitda": ["EBITDA"],
    "net_income": ["NetIncomeLoss"],
    "eps": [
        "EarningsPerShareDiluted",
    ],
    "eps_basic": [
        "EarningsPerShareBasic",
    ],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxExpenseBenefit",
        "IncomeBeforeTax",
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseDebt",
    ],
    # === 资产负债表 ===
    "total_assets": ["Assets"],
    "current_assets": ["AssetsCurrent"],
    "total_liabilities": ["Liabilities"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "equity": ["StockholdersEquity", "Equity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash_short_term": ["CashAndCashEquivalentsAtCarryingValue", "Cash"],
    "inventory": ["InventoryNet", "Inventory"],
    "receivables": ["AccountsReceivableNetCurrent", "AccountsReceivableNet", "Receivables"],
    "ppe_net": ["PropertyPlantAndEquipmentNet", "PropertyPlantAndEquipment"],
    "goodwill": ["Goodwill"],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
    ],
    "short_term_debt": ["DebtCurrent", "ShortTermDebt"],
    "book_value_per_share": ["CommonStockSharesOutstanding"],  # 计算用: equity/shares
    # === 现金流量表 ===
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "CapitalExpenditures"],
    "free_cf": ["FreeCashFlow"],  # 很多公司不直接提供, 需要计算
    # === 补充数据 ===
    "comprehensive_income": ["ComprehensiveIncomeNetOfTax"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
}


def _rate_limit():
    """SEC 限速: 10次/秒"""
    global LAST_REQUEST_TIME
    elapsed = time.time() - LAST_REQUEST_TIME
    if elapsed < 0.12:
        time.sleep(0.12 - elapsed)
    LAST_REQUEST_TIME = time.time()


def _headers():
    return {"User-Agent": USER_AGENT}


# ====== 1. CIK 映射 ======
def load_cik_map(force_refresh=False) -> Dict[str, str]:
    """
    加载 ticker → CIK 映射表。
    返回 {ticker: cik_str} (已10位补零)
    """
    cache_valid = False
    if os.path.exists(CIK_CACHE_FILE) and not force_refresh:
        age = time.time() - os.path.getmtime(CIK_CACHE_FILE)
        if age < 86400 * 7:  # 缓存7天
            cache_valid = True

    if cache_valid:
        try:
            with open(CIK_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            pass

    _rate_limit()
    r = requests.get(CIK_URL, headers=_headers(), timeout=30)
    r.raise_for_status()
    raw = r.json()

    result = {}
    for entry in raw.values():
        ticker = entry["ticker"].upper()
        cik = str(entry["cik_str"]).zfill(10)
        result[ticker] = cik

    with open(CIK_CACHE_FILE, "w") as f:
        json.dump(result, f)
    return result


def get_cik(ticker: str, cik_map=None) -> Optional[str]:
    """获取股票的CIK编号（10位补零）"""
    if cik_map is None:
        cik_map = load_cik_map()
    return cik_map.get(ticker.upper())


# ====== 2. 公司事实数据 ======
def fetch_company_facts(cik: str) -> Optional[dict]:
    """
    获取公司所有XBRL事实数据。
    cik: 10位补零的CIK编号
    
    返回: 完整的CompanyFacts JSON 或 None
    """
    # 检查缓存
    cache_file = os.path.join(CACHE_DIR, f"facts_{cik}.json")
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < FACTS_CACHE_TTL:
            try:
                with open(cache_file, "r") as f:
                    return json.load(f)
            except:
                pass

    _rate_limit()
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()

        # 缓存
        with open(cache_file, "w") as f:
            json.dump(data, f)

        return data
    except Exception as e:
        print(f"[sec_edgar] fetch_company_facts({cik}): {e}")
        # 尝试过期的缓存
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                return json.load(f)
        return None


def _get_concept_data(facts: dict, concept_name: str) -> List[dict]:
    """
    从CompanyFacts中提取指定概念的所有数据点。
    返回按 filed 日期降序排列的数据点列表。
    每个数据点包含: end, val, fy, fp, form, filed, accn
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    # v9.0: 遍历映射表，优先选有近期10-Q数据的，次选10-K
    found_concept = None
    best_score = -1  # 2=有10-Q, 1=只有10-K, 0=只有远古
    
    for name in concept_name:
        if name not in us_gaap:
            continue
        cd = us_gaap[name]
        has_q10 = False
        has_k10 = False
        has_recent = False
        for unit_key, pts in cd.get("units", {}).items():
            for p in pts:
                form = p.get("form", "")
                end = p.get("end", "")
                if form == "10-Q" and end >= "2020-01-01":
                    has_q10 = True
                    has_recent = True
                elif form == "10-K" and end >= "2020-01-01":
                    has_k10 = True
                    has_recent = True
        
        score = 2 if has_q10 else (1 if has_k10 else 0)
        if score > best_score:
            best_score = score
            found_concept = name

    if not found_concept:
        # 回退：只要存在于us-gaap就接受（可能有远古数据但聊胜于无）
        for name in concept_name:
            if name in us_gaap:
                found_concept = name
                break

    if not found_concept:
        return []

    concept_data = us_gaap[found_concept]
    units = concept_data.get("units", {})

    # 查找USD、USD/shares或shares单位
    points = []
    for unit_key in ["USD", "USD/shares", "shares", "pure", "EUR", "USDperShare"]:
        if unit_key in units:
            points = units[unit_key]
            break

    if not points:
        return []

    # 按 form → end(最新优先) → filed 排序
    def sort_key(p):
        form = p.get("form", "")
        form_priority = 0
        if form == "10-Q":
            form_priority = 3
        elif form == "10-K":
            form_priority = 2
        elif form == "8-K":
            form_priority = 1
        return (form_priority, p.get("end", ""), p.get("filed", ""))

    points.sort(key=sort_key, reverse=True)
    # 注意: 同一 end+form 可能有多个数据点(YTD累计+修正单季)
    # 全部保留，YTD检测在 get_latest_quarterly 中处理
    return points


def get_latest_value(facts: dict, concept_names: list,
                     form_filter=None) -> Optional[dict]:
    """
    获取概念的最新数据点。
    
    Args:
        facts: CompanyFacts JSON
        concept_names: 概念名列表（依次尝试）
        form_filter: 过滤表单类型, None=不限, "10-Q"=仅季度, "10-K"=仅年度
    
    Returns:
        {"value": float, "end_date": str, "form": str, "fp": str, "fy": int} 或 None
    """
    points = _get_concept_data(facts, concept_names)
    if not points:
        return None

    for p in points:
        form = p.get("form", "")
        if form_filter and form != form_filter:
            continue
        if form not in ("10-Q", "10-K", "8-K"):
            continue

        val = p.get("val")
        if val is None:
            continue

        return {
            "value": float(val),
            "end_date": p.get("end", ""),
            "form": form,
            "fp": p.get("fp", ""),
            "fy": p.get("fy", 0),
            "filed": p.get("filed", ""),
        }

    return None


def get_latest_quarterly(facts: dict, concept_names: list) -> Optional[dict]:
    """
    获取最新10-Q数据点（单季值）。
    
    处理逻辑：
    1. 按 end 日期分组，取最新的期间
    2. 同一期间可能有多个值（YTD累计 + 修正单季）
    3. 如果有Q1数据，检测YTD并计算单季值
    """
    points = _get_concept_data(facts, concept_names)
    if not points:
        return None

    # 过滤10-Q
    q_points = [p for p in points if p.get("form") == "10-Q"]
    if not q_points:
        return None

    # 按 end 日期分组，取最新期间
    # 同一 end 可能有多个点(原始+YTD / 修正+单季)
    latest_end = q_points[0]["end"]
    same_period_points = [p for p in q_points if p["end"] == latest_end]

    if not same_period_points:
        return None

    latest = same_period_points[0]
    val = float(latest["val"])
    end = latest["end"]
    fp = latest["fp"]

    # 处理YTD vs 单季值问题
    if len(same_period_points) > 1:
        # 同一期间有多个值：取最小的正值（通常是修正后的单季值）
        # 如果最大/最小 > 1.3，说明大的是YTD累计值
        vals = sorted([float(p["val"]) for p in same_period_points if float(p["val"]) > 0])
        if vals:
            if len(vals) >= 2 and vals[-1] / vals[0] >= 1.3:
                val = vals[0]  # 最小的是单季值
            else:
                val = vals[-1]  # 取最新的
    elif fp and fp.startswith("Q") and int(fp[1]) > 1:
        # 只有单一值，但可能是YTD累计
        q_num = int(fp[1])
        # 找前一Q1的值做对比
        for p in q_points:
            p_fp = p.get("fp", "")
            p_end = p.get("end", "")
            if p_fp == "Q1" and p_end < end:
                prev_val = float(p["val"])
                if prev_val > 0 and val > prev_val * 1.5:
                    single_q_val = val - prev_val
                    if single_q_val > 0:
                        val = single_q_val
                break

    return {
        "value": val,
        "end_date": end,
        "form": "10-Q",
        "fp": fp,
        "fy": latest.get("fy", 0),
        "filed": latest.get("filed", ""),
    }


def get_latest_annual(facts: dict, concept_names: list) -> Optional[dict]:
    """获取最新10-K数据点"""
    return get_latest_value(facts, concept_names, form_filter="10-K")


def get_recent_quarterlies(facts: dict, concept_names: list,
                           num_quarters: int = 4) -> List[dict]:
    """
    获取最近N个季度的数据点。
    处理 YTD 累计值 → 用相邻相减法转成单季值。
    
    Returns:
        [{end_date, value, fy, fp, filed}, ...] 按时间升序
    """
    points = _get_concept_data(facts, concept_names)
    if not points:
        return []

    # 筛选10-Q
    q_points = [p for p in points if p.get("form") == "10-Q"]
    if not q_points:
        # 回退到8-K
        q_points = [p for p in points if p.get("form") == "8-K"]

    # 按 end 日期分组，取每组的单季值（非YTD累计）
    groups = {}
    for p in q_points:
        end = p.get("end", "")
        val = float(p.get("val", 0))
        if val <= 0:
            continue
        if end not in groups:
            groups[end] = p
        else:
            # 同一end已有值：取更小的(单季值而非YTD累计)
            existing = float(groups[end]["val"])
            if val < existing and existing / val >= 1.3:
                groups[end] = p

    # 按 end 日期排序
    sorted_groups = sorted(groups.values(), key=lambda x: x.get("end", ""))

    # 检测是单季值还是累计值，转成单季
    result = []
    cumulative_sum = 0
    prev_val = None

    for p in sorted_groups[-num_quarters - 1:]:
        val = float(p.get("val", 0))
        end = p.get("end", "")
        fy = p.get("fy", 0)
        fp = p.get("fp", "")
        filed = p.get("filed", "")

        # 检查是否累计: 如果远大于前一季，可能是累计
        if prev_val is not None and prev_val > 0:
            if val > prev_val * 1.5:
                val = val - prev_val

        result.append({
            "end_date": end,
            "value": val,
            "fy": fy,
            "fp": fp,
            "filed": filed,
        })
        prev_val = val

    return result[-num_quarters:]


# ====== 3. 财务报表解析 ======

def parse_sec_financials(fd: dict, ticker: str) -> dict:
    """
    从SEC EDGAR解析美股财务数据，填充到fd字典。
    作为westock的替代/补充数据源。
    
    Args:
        fd: 已有数据的字典（含westock数据）
        ticker: 美股代码
    
    Returns:
        更新后的fd字典
    """
    cik = get_cik(ticker)
    if not cik:
        print(f"[sec_edgar] Ticker {ticker} not found in CIK map")
        return fd

    facts = fetch_company_facts(cik)
    if not facts:
        print(f"[sec_edgar] No facts for {ticker} (CIK={cik})")
        return fd

    # ====== 利润表 ======
    # 营收 — 最新10-Q单季
    rev_q = get_latest_quarterly(facts, CONCEPT_MAP["revenue"])
    if rev_q:
        fd["revenue"] = rev_q["value"]
        fd["_sec_revenue_q"] = rev_q

    # 营收 — 取最新4季度TTM
    rev_4q = get_recent_quarterlies(facts, CONCEPT_MAP["revenue"], 4)
    if len(rev_4q) >= 4:
        fd["_revenue_ttm"] = sum(q["value"] for q in rev_4q)
    elif len(rev_4q) > 0 and rev_q:
        # 不足4期，用最新季 * 4 估算
        fd["_revenue_ttm"] = rev_q["value"] * 4

    # 净利润
    ni_q = get_latest_quarterly(facts, CONCEPT_MAP["net_income"])
    if ni_q:
        fd["net_income"] = ni_q["value"]

    ni_4q = get_recent_quarterlies(facts, CONCEPT_MAP["net_income"], 4)
    if len(ni_4q) >= 4:
        fd["_net_income_ttm"] = sum(q["value"] for q in ni_4q)
    elif len(ni_4q) > 0 and ni_q:
        fd["_net_income_ttm"] = ni_q["value"] * 4

    # 营业成本
    cost_q = get_latest_quarterly(facts, CONCEPT_MAP["total_cost"])
    if cost_q:
        fd["total_cost"] = cost_q["value"]

    # 毛利润
    gp_q = get_latest_quarterly(facts, CONCEPT_MAP["gross_profit"])
    if gp_q:
        fd["gross_profit"] = gp_q["value"]

    # 营业利润
    oi_q = get_latest_quarterly(facts, CONCEPT_MAP["operating_income"])
    if oi_q:
        fd["operating_income"] = oi_q["value"]
        fd["ebit"] = oi_q["value"]

    # 研发费用
    rd_q = get_latest_quarterly(facts, CONCEPT_MAP["rd_expense"])
    if rd_q:
        fd["rd_expense"] = rd_q["value"]

    # 销售管理费用
    sga_q = get_latest_quarterly(facts, CONCEPT_MAP["sga_expense"])
    if sga_q:
        fd["sga_expense"] = sga_q["value"]

    # 所得税
    tax_q = get_latest_quarterly(facts, CONCEPT_MAP["income_tax"])
    if tax_q:
        fd["_sec_income_tax"] = tax_q["value"]

    # EPS
    eps_q = get_latest_quarterly(facts, CONCEPT_MAP["eps"])
    if eps_q:
        fd["eps"] = eps_q["value"]

    # TTM EPS
    eps_4q = get_recent_quarterlies(facts, CONCEPT_MAP["eps"], 4)
    if len(eps_4q) >= 4:
        fd["eps_ttm"] = round(sum(q["value"] for q in eps_4q), 4)
    elif eps_q:
        fd["eps_ttm"] = eps_q["value"]

    # ====== 资产负债表 ======
    # 使用最新10-K或10-Q数据
    for sec_key, map_keys in [
        ("total_assets", CONCEPT_MAP["total_assets"]),
        ("total_liabilities", CONCEPT_MAP["total_liabilities"]),
        ("equity", CONCEPT_MAP["equity"]),
        ("cash_short_term", CONCEPT_MAP["cash_short_term"]),
        ("inventory", CONCEPT_MAP["inventory"]),
        ("receivables", CONCEPT_MAP["receivables"]),
        ("long_term_debt", CONCEPT_MAP["long_term_debt"]),
        ("short_term_debt", CONCEPT_MAP["short_term_debt"]),
    ]:
        val = get_latest_value(facts, map_keys)
        if val:
            fd[sec_key] = val["value"]
            if sec_key == "equity" and fd.get("equity"):
                fd["_sec_equity"] = val

    # BPS = equity / shares_outstanding
    shares = get_latest_value(facts, CONCEPT_MAP["shares_outstanding"])
    equity = fd.get("equity")
    if shares and shares["value"] > 0 and equity and equity > 0:
        fd["book_value_per_share"] = round(equity / shares["value"], 4)

    # ====== 现金流量表 ======
    cfo_q = get_latest_quarterly(facts, CONCEPT_MAP["operating_cf"])
    if cfo_q:
        fd["operating_cf"] = cfo_q["value"]

    capex_q = get_latest_quarterly(facts, CONCEPT_MAP["capex"])
    if capex_q:
        # Capex在XBRL中是正数(流出)
        fd["capex"] = capex_q["value"]
        if fd.get("operating_cf"):
            fd["free_cf"] = fd["operating_cf"] - abs(fd["capex"])

    # ====== 报告日期 ======
    # 取营收的最新报告日期
    if rev_q:
        fd["report_date"] = rev_q["end_date"]
    elif ni_q:
        fd["report_date"] = ni_q["end_date"]
    elif facts.get("entityName"):
        fd["_name"] = facts["entityName"]

    # ====== 衍生比率（基于最新单季值）=======
    rev = fd.get("revenue") or fd.get("_revenue_ttm")
    if rev and rev > 0:
        if fd.get("gross_profit"):
            fd["gross_margin"] = round(fd["gross_profit"] / rev, 4)
        if fd.get("operating_income"):
            fd["operating_margin"] = round(fd["operating_income"] / rev, 4)
        if fd.get("net_income"):
            fd["net_margin"] = round(fd["net_income"] / rev, 4)

    # ====== TTM衍生（不覆盖单季指标，提供TTM版本仅供参考）======
    rev_ttm = fd.get("_revenue_ttm")
    ni_ttm = fd.get("_net_income_ttm")

    if rev_ttm and rev_ttm > 0:
        # TTM毛利率（单独字段，不覆盖gross_margin）
        gp_ttm = sum(q["value"] for q in get_recent_quarterlies(facts, CONCEPT_MAP["gross_profit"], 4))
        if gp_ttm > 0:
            fd["_gross_margin_ttm"] = round(gp_ttm / rev_ttm, 4)

    # ROE = TTM净利润 / 权益
    if ni_ttm and fd.get("equity") and fd["equity"] > 0:
        fd["roe"] = round(ni_ttm / fd["equity"], 4)

    # ROA = TTM净利润 / 总资产
    if ni_ttm and fd.get("total_assets") and fd["total_assets"] > 0:
        fd["roa"] = round(ni_ttm / fd["total_assets"], 4)

    # PE = 股价 / TTM EPS
    if fd.get("eps_ttm") and fd.get("price") and fd["eps_ttm"] > 0:
        fd["pe"] = round(fd["price"] / fd["eps_ttm"], 2)

    # PS = 市值 / TTM营收
    if fd.get("market_cap") and rev_ttm and rev_ttm > 0:
        fd["ps"] = round(fd["market_cap"] / rev_ttm, 2)

    # PB = 股价 / BPS
    if fd.get("price") and fd.get("book_value_per_share") and fd["book_value_per_share"] > 0:
        fd["pb"] = round(fd["price"] / fd["book_value_per_share"], 2)

    # 负债率
    if fd.get("total_liabilities") and fd.get("total_assets") and fd["total_assets"] > 0:
        fd["debt_ratio"] = round(fd["total_liabilities"] / fd["total_assets"], 4)

    fd["_data_source_sec"] = True
    return fd


# ====== 测试 ======
if __name__ == "__main__":
    for sym in ["MU", "NVDA", "AAPL", "MSFT"]:
        print(f"\n{'='*50}")
        print(f"  {sym}")
        print('='*50)
        fd = {}
        fd = parse_sec_financials(fd, sym)

        for k in ["revenue", "net_income", "gross_margin", "net_margin",
                   "roe", "eps_ttm", "pe", "pb", "total_assets", "equity",
                   "report_date", "operating_cf", "free_cf"]:
            v = fd.get(k)
            if v is not None:
                if isinstance(v, float):
                    print(f"  {k}: {v:,.4f}")
                else:
                    print(f"  {k}: {v}")

        # 强制限速
        time.sleep(0.5)
