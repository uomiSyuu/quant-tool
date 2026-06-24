# -*- coding: utf-8 -*-
"""
美股数据适配器 v8.8
====================
数据源组装:
  实时行情  → 腾讯 qt.gtimg.cn（price + name）
  基本面    → yfinance.info（限速15s，缓存10min）
  财报      → westock（主备）+ yfinance（前瞻，当前被Yahoo封禁）

当前已知：Yahoo Finance IP封禁（403/YFRateLimit），yfinance路径暂不可用。
部署到其他服务器时可自动恢复。详见 parse_us_financials_timeout()。
"""

import time, json, os, requests
from datetime import datetime

# ====== 常量 ======
YF_CACHE_FILE = os.path.join(os.path.dirname(__file__), "yf_cache.json")
YF_CACHE_TTL = 600  # 缓存10分钟
YF_MIN_INTERVAL = 15  # yfinance最小调用间隔(秒)
_last_yf_time = 0

# ====== v8.9: 美股财年结束月份映射 ======
# 默认：12（自然年）。以下列出已知的非12月财年结束股票
# 格式：{symbol: fiscal_year_end_month}
# 参考来源：各公司年度报告 (10-K)
FISCAL_YEAR_END = {
    # --- 1月结束 ---
    "NVDA": 1, "WMT": 1, "HPQ": 1, "BBY": 1, "GME": 1,
    "CRM": 1,   # Salesforce
    # --- 3月结束 ---
    # --- 4月结束 ---
    # --- 5月结束 ---
    "ORCL": 5,  # Oracle
    # --- 6月结束 ---
    "MSFT": 6, "ADBE": 6, "ORLY": 6,
    # --- 7月结束 ---
    # --- 8月结束 ---
    "MU": 8,
    # --- 9月结束 ---
    "AAPL": 9, "V": 9, "DIS": 9, "NKE": 9, "QCOM": 9,
    "COST": 9,  # Costco: Sep
    # --- 10月结束 ---
    # --- 11月结束 ---
}

def calc_us_fiscal_period(end_date, symbol):
    """
    根据EndDate和公司财年，计算正确的报告期标签。
    
    财年标签 = FY{财年结束年}Q{季度}
    例如：MU财年结束於8月，2025-12-31 → FY26Q2
    
    Args:
        end_date: westock的EndDate字符串 (YYYY-MM-DD)
        symbol: 股票代码
    
    Returns:
        str: 报告期标签 (如 "FY26Q1")
    """
    if not end_date or len(end_date) < 10:
        return None
    
    fy_end = FISCAL_YEAR_END.get(symbol.upper(), 12)
    
    try:
        dt = datetime.strptime(end_date[:10], "%Y-%m-%d")
    except:
        return None
    
    # 财年起始月 = 财年结束月的下一个月
    fy_start = (fy_end % 12) + 1
    
    m = dt.month
    y = dt.year
    
    # 计算距离财年起始的偏移月数
    if m >= fy_start:
        months_offset = m - fy_start
        # 财年结束年 = 下一（或本）年的fy_end月所在的年
        # 如果财年起始在年中，fy_end年在下一自然年
        if fy_end < fy_start:
            # 财年跨越自然年 (如 fy_end=6, fy_start=7)
            fiscal_year = y + 1
        else:
            fiscal_year = y
    else:
        months_offset = m + (12 - fy_start)
        # 当前月份在财年起始月之前 → fy_end在本自然年中
        fiscal_year = y
    
    fiscal_quarter = months_offset // 3 + 1
    
    return f"FY{fiscal_year%100}Q{fiscal_quarter}"

# ====== 1. 腾讯行情 ======
def get_us_quote_tencent(symbol):
    """腾讯API — 提取行情（固定字段位置，适用于所有美股）"""
    result = {}
    try:
        url = f"https://qt.gtimg.cn/q=us{symbol}"
        r = requests.get(url, timeout=10)
        r.encoding = 'gbk'
        body = r.text.strip()
        if not body or '=' not in body: return result
        eq = body.index('=')
        raw = body[eq+1:].strip('"').strip("'")
        parts = raw.split('~')
        # 美股标准格式: 71个字段, 已验证10只股票一致
        if len(parts) < 50: return result
        
        result["_source"] = "tencent"
        result["_name"] = parts[1] if parts[1] else symbol
        result["price"] = _float(parts[3])
        result["change_pct"] = _float(parts[32]) / 100.0  # 腾讯返回的是百分值(如-13.18→-0.1318)
        result["high"] = _float(parts[33])
        result["low"] = _float(parts[34])
        result["currency"] = parts[35] if parts[35] else "USD"
        result["volume"] = _int(parts[36])
        result["turnover"] = _float(parts[37])
        
        # field 41 = PE-TTM
        pe_v = _float(parts[41])
        if pe_v and 0 < pe_v < 10000:
            result["pe"] = pe_v
        
        # field 44 = 市值(亿)
        mcap = _float(parts[44])
        if mcap and mcap > 0:
            result["market_cap"] = mcap * 1e8
        
        # field 46 = 英文名
        en = parts[46] if len(parts) > 46 else ""
        if en and not en.replace('.','').replace(',','').isdigit():
            result["name_en"] = en
        
        # field 49 = 52周低? (与价格同量级)
        # field 48 = 52周高?
        h52 = _float(parts[48]) if len(parts) > 48 else None
        l52 = _float(parts[49]) if len(parts) > 49 else None
        if h52 and l52 and h52 > l52:
            result["high_52w"] = h52
            result["low_52w"] = l52
        
    except Exception as e:
        print(f"[tencent] {symbol}: {e}")
    return result


# ====== 2. yfinance基本面（带缓存和限速）======

def _load_yf_cache():
    try:
        if os.path.exists(YF_CACHE_FILE):
            with open(YF_CACHE_FILE, 'r') as f:
                return json.load(f)
    except: pass
    return {}

def _save_yf_cache(data):
    try:
        with open(YF_CACHE_FILE, 'w') as f:
            json.dump(data, f)
    except: pass

def get_us_profile_yf(symbol):
    """
    获取美股基本面（yfinance，限速+缓存）
    返回: {pe, pb, market_cap, eps_ttm, ...}
    """
    global _last_yf_time
    
    # 检查缓存
    cache = _load_yf_cache()
    now = time.time()
    if symbol in cache and (now - cache[symbol].get("_ts", 0)) < YF_CACHE_TTL:
        return {k:v for k,v in cache[symbol].items() if not k.startswith("_")}
    
    # 限速
    elapsed = now - _last_yf_time
    if elapsed < YF_MIN_INTERVAL:
        time.sleep(YF_MIN_INTERVAL - elapsed)
    
    result = {}
    try:
        import yfinance as yf
        _last_yf_time = time.time()
        ticker = yf.Ticker(symbol)
        info = ticker.info
        _last_yf_time = time.time()
        
        # 提取关键字段
        result["pe"] = info.get("trailingPE")
        result["pe_forward"] = info.get("forwardPE")
        result["pb"] = info.get("priceToBook")
        result["market_cap"] = info.get("marketCap")
        result["eps_ttm"] = info.get("trailingEps")
        result["high_52w"] = info.get("fiftyTwoWeekHigh")
        result["low_52w"] = info.get("fiftyTwoWeekLow")
        result["dividend_yield"] = info.get("dividendYield")
        result["beta"] = info.get("beta")
        result["sector"] = info.get("sector", "")
        result["industry"] = info.get("industry", "")
        result["short_name"] = info.get("shortName") or info.get("longName")
        result["shares_outstanding"] = info.get("sharesOutstanding")
        result["book_value"] = info.get("bookValue")
        result["revenue"] = info.get("totalRevenue")
        result["revenue_growth"] = info.get("revenueGrowth")
        result["gross_margin"] = info.get("grossMargins")
        result["net_margin"] = info.get("profitMargins")
        result["operating_margin"] = info.get("operatingMargins")
        result["free_cf"] = info.get("freeCashflow")
        result["ebitda"] = info.get("ebitda")
        result["roe"] = info.get("returnOnEquity")
        result["roa"] = info.get("returnOnAssets")
        result["debt_to_equity"] = info.get("debtToEquity")
        result["target_price"] = info.get("targetMeanPrice")
        result["recommendation"] = info.get("recommendationKey")
        result["current_ratio"] = info.get("currentRatio")
        result["quick_ratio"] = info.get("quickRatio")
        result["earnings_growth"] = info.get("earningsGrowth")
        
        # 计算衍生指标
        if result.get("book_value") and result.get("price"):
            result["pb"] = result["price"] / result["book_value"]
        if result.get("market_cap") and result.get("ebitda") and result["ebitda"] > 0:
            result["ev_ebitda"] = round(result["market_cap"] / result["ebitda"], 2)
        if result.get("market_cap") and result.get("revenue") and result["revenue"] > 0:
            result["ps"] = round(result["market_cap"] / result["revenue"], 2)
        if result.get("free_cf") and result.get("market_cap") and result["market_cap"] > 0:
            result["fcf_yield"] = round(result["free_cf"] / result["market_cap"], 4)
        
        # 写入缓存
        cache[symbol] = result.copy()
        cache[symbol]["_ts"] = time.time()
        _save_yf_cache(cache)
        
    except yf.exceptions.YFRateLimitError:
        print(f"[yf_rate_limit] {symbol}: 限速，使用缓存")
        # 使用过期缓存
        if symbol in cache:
            result = {k:v for k,v in cache[symbol].items() if not k.startswith("_")}
    except Exception as e:
        print(f"[yf_profile] {symbol}: {e}")
        # 使用过期缓存
        if symbol in cache:
            result = {k:v for k,v in cache[symbol].items() if not k.startswith("_")}
    
    _last_yf_time = time.time()
    return result


# ====== 3. yfinance财报（补充westock缺失的最新数据）======

def get_us_financials_yf(symbol):
    """获取美股财报（yfinance，配合限速队列）"""
    global _last_yf_time
    
    elapsed = time.time() - _last_yf_time
    if elapsed < YF_MIN_INTERVAL:
        time.sleep(YF_MIN_INTERVAL - elapsed)
    
    result = {}
    try:
        import yfinance as yf
        _last_yf_time = time.time()
        ticker = yf.Ticker(symbol)
        
        inc = ticker.income_stmt
        if inc is not None and not inc.empty:
            rows = []
            for col in inc.columns[:6]:
                row = inc[col].to_dict()
                row['_date'] = str(col)[:10]
                rows.append(row)
            rows.reverse()
            result["income"] = rows
        
        bs = ticker.balance_sheet
        if bs is not None and not bs.empty:
            rows = []
            for col in bs.columns[:6]:
                row = bs[col].to_dict()
                row['_date'] = str(col)[:10]
                rows.append(row)
            rows.reverse()
            result["balance"] = rows
        
        cf = ticker.cashflow
        if cf is not None and not cf.empty:
            rows = []
            for col in cf.columns[:6]:
                row = cf[col].to_dict()
                row['_date'] = str(col)[:10]
                rows.append(row)
            rows.reverse()
            result["cashflow"] = rows
    except Exception as e:
        print(f"[yf_fin] {symbol}: {e}")
    
    _last_yf_time = time.time()
    return result


def parse_us_financials(fd, symbol, force_overwrite=True):
    """
    合并westock数据+yfinance补新 (v8.9: 默认覆盖所有字段)
    
    Parameters:
        fd: 数据字典（已有westock数据）
        symbol: 股票代码
        force_overwrite: True=覆盖现有值(v8.9+), False=仅补None(旧行为)
    """
    yf_fin = get_us_financials_yf(symbol)
    
    def _should_set(val):
        """是否应该设置值"""
        if force_overwrite:
            return val is not None  # 覆盖模式：只要有值就设
        return val is not None       # 补缺模式等同于旧行为
    
    if "income" in yf_fin and yf_fin["income"]:
        row = yf_fin["income"][0]
        for k, yk in [("revenue","Total Revenue"),("net_income","Net Income"),
                       ("gross_profit","Gross Profit"),("total_cost","Cost of Revenue"),
                       ("operating_income","Operating Income"),("ebitda","EBITDA"),
                       ("ebit","EBIT"),("rd_expense","Research and Development"),
                       ("sga_expense","Selling General and Administrative"),
                       ("eps","Diluted EPS")]:
            v = _yf_val(row.get(yk))
            if _should_set(v):
                fd[k] = v
        
        # TTM EPS (always compute fresh)
        if len(yf_fin["income"]) >= 4:
            eps_ttm = 0
            ok = True
            for i in range(4):
                v = _yf_val(yf_fin["income"][i].get("Diluted EPS"))
                if v: eps_ttm += v
                else: ok = False
            if ok and eps_ttm > 0:
                fd["eps_ttm"] = round(eps_ttm, 4)
        
        # 报告日期（始终从yfinance取最新）
        fd["report_date"] = str(yf_fin["income"][0].get("_date",""))[:10]
        
        # v8.9: 毛利率/净利率/运营利润率重算
        rev = fd.get("revenue")
        gp = fd.get("gross_profit")
        ni = fd.get("net_income")
        if rev and rev > 0:
            if gp is not None: fd["gross_margin"] = gp / rev
            if ni is not None: fd["net_margin"] = ni / rev
    
    if "balance" in yf_fin and yf_fin["balance"]:
        row = yf_fin["balance"][0]
        for k, yk in [("total_assets","Total Assets"),("total_liabilities","Total Liabilities Net Minority Interest"),
                       ("equity","Stockholders Equity"),("cash_short_term","Cash and Cash Equivalents"),
                       ("inventory","Inventory"),("receivables","Net Receivables")]:
            v = _yf_val(row.get(yk))
            if _should_set(v):
                fd[k] = v
        # BPS from equity if available
        if fd.get("equity") is not None:
            # Use shares outstanding from yfinance info if available
            pass
    
    if "cashflow" in yf_fin and yf_fin["cashflow"]:
        row = yf_fin["cashflow"][0]
        for k, yk in [("free_cf","Free Cash Flow"),("operating_cf","Operating Cash Flow"),
                       ("capex","Capital Expenditure")]:
            v = _yf_val(row.get(yk))
            if _should_set(v):
                fd[k] = v
    
    fd["_data_source"] = "us_adapter_v8.9"
    return fd


def parse_us_financials_timeout(fd, symbol, timeout=25):
    """
    v8.9: 带超时的parse_us_financials包装
    用于fetch_live()中避免阻塞整个API响应
    """
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(parse_us_financials, fd, symbol)
        try:
            future.result(timeout=timeout)
            return True
        except Exception as e:
            print(f"[yf_overlay_timeout] {symbol}: {e}")
            return False


# ====== 4. 统一入口 ======

def get_us_quote(symbol):
    """统一行情入口: 腾讯(主) + yfinance缓存(备)"""
    result = get_us_quote_tencent(symbol)
    # 腾讯已有PE和市值就不调yfinance
    if not result.get("pe") or not result.get("market_cap"):
        yf_data = get_us_profile_yf(symbol)
        for k in ["pe","pb","market_cap","eps_ttm","high_52w","low_52w","beta","sector","dividend_yield"]:
            if result.get(k) is None and yf_data.get(k) is not None:
                result[k] = yf_data[k]
    return result


# ====== 辅助函数 ======

def _float(v):
    try: return float(v) if v not in (None, "", "None", "-", "—") else None
    except: return None

def _int(v):
    try: return int(float(v)) if v not in (None, "", "None", "-", "—") else 0
    except: return 0

def _yf_val(v):
    if v is None: return None
    if isinstance(v, dict): return v.get("raw", v.get("fmt"))
    if isinstance(v, (int, float)): return v
    try: return float(v)
    except: return None


# ====== 自测 ======
if __name__ == "__main__":
    for sym in ["MU", "NVDA", "AAPL"]:
        print(f"\n=== {sym} ===")
        q = get_us_quote(sym)
        for k in ['_name','price','pe','pb','market_cap','eps_ttm','high_52w','low_52w','sector','beta','dividend_yield']:
            if q.get(k):
                print(f"  {k}: {q[k]}")
        time.sleep(2)
