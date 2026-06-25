# -*- coding: utf-8 -*-
"""
A股数据适配器 v8.6
===================
多源数据组装:
  实时行情  → 新浪财经 hq.sinajs.cn
  日K线     → 东方财富 push2his.eastmoney.com
  财报数据  → akshare/新浪
  板块成分  → 东方财富 push2.eastmoney.com

用法:
  from a_share_adapter import get_ashare_quote, get_ashare_financials, get_ashare_board_stocks
"""

import time, json, re
import requests
from datetime import datetime

# ====== 常量 ======
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={}"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_BOARD_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_BOARD_STOCK_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_market_map = {"sh": 1, "sz": 0, "bj": 2}
_market_rev = {"sh": "1", "sz": "0", "bj": "2"}

# v9.1: 东方财富直连API缓存（财报数据缓存24小时，很少变化）
_em_cache = {}
_em_cache_lock = None
try:
    import threading
    _em_cache_lock = threading.Lock()
except:
    pass

def _em_cache_get(key, ttl=86400):
    """24小时缓存"""
    if not _em_cache_lock:
        return None
    with _em_cache_lock:
        e = _em_cache.get(key)
        if e and time.time() < e[1]:
            return e[0]
        return None

def _em_cache_set(key, val, ttl=86400):
    if not _em_cache_lock:
        return
    with _em_cache_lock:
        _em_cache[key] = (val, time.time() + ttl)

# ====== 1. 实时行情 ======

def normalize_ashare_symbol(symbol):
    """
    标准化A股代码:
    600519 -> sh600519
    000001 -> sz000001
    300750 -> sz300750
    688981 -> sh688981
    """
    s = symbol.strip().upper()
    # 已有前缀
    if s.startswith("SH") or s.startswith("SH."):
        return f"sh{s.replace('SH','').replace('.','').strip()}"
    if s.startswith("SZ") or s.startswith("SZ."):
        return f"sz{s.replace('SZ','').replace('.','').strip()}"
    if s.startswith("BJ") or s.startswith("BJ."):
        return f"bj{s.replace('BJ','').replace('.','').strip()}"
    
    # 纯数字判断
    digits = re.sub(r'\D', '', s)
    if len(digits) == 6:
        if digits.startswith(('0', '3')):
            return f"sz{digits}"
        elif digits.startswith('6'):
            return f"sh{digits}"
        elif digits.startswith('4'):
            return f"sh{digits}"
        elif digits.startswith('8'):
            return f"bj{digits}"
    return f"sh{digits}"


def get_ashare_quotes(symbols, timeout=10):
    """
    批量获取A股实时行情（新浪+东方财富双源）
    返回: {symbol: {name, price, change, change_pct, open, high, low, volume, amount, pe, market_cap}}
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    
    nids = []
    codes = {}
    for sym in symbols:
        ns = normalize_ashare_symbol(sym)
        nids.append(ns)
        codes[ns] = sym
    
    # 1. 新浪基础行情
    url = SINA_QUOTE_URL.format(",".join(nids))
    results = {}
    try:
        r = requests.get(url, headers=SINA_HEADERS, timeout=timeout)
        r.encoding = 'gbk'
        
        for line in r.text.strip().split('\n'):
            if '=' not in line:
                continue
            parts = line.split('=')
            if len(parts) != 2:
                continue
            raw_id = parts[0].strip().split('_')[-1].strip('"').strip("'")
            vals = parts[1].strip('"').strip(';').split(',')
            if len(vals) < 30:
                continue
            
            results[raw_id] = {
                "symbol": raw_id,
                "name": vals[0],
                "open": _float(vals[1]),
                "close": _float(vals[2]),
                "price": _float(vals[3]),
                "high": _float(vals[4]),
                "low": _float(vals[5]),
                "volume": _int(vals[8]),
                "amount": _float(vals[9]) * 1e4 if _float(vals[9]) else None,
                "change": 0, "change_pct": 0,
                "pe": None,
                "market_cap": None,
                "timestamp": datetime.now().isoformat(),
            }
            p = results[raw_id]["price"]
            c = results[raw_id]["close"]
            if p and c and c > 0:
                results[raw_id]["change"] = round(p - c, 2)
                results[raw_id]["change_pct"] = round((p - c) / c * 100, 2)
    except Exception as e:
        print(f"[ashare_quote_sina] error: {e}")
    
    # 2. 东方财富补充PE/市值
    for raw_id in list(results.keys()):
        prefix = raw_id[:2]
        code = raw_id[2:]
        market = _market_map.get(prefix, 1)
        try:
            secid = f"{market}.{code}"
            params = {
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbbd4b9c",
                "fields": "f43,f116,f162,f167,f169,f170,f55",
            }
            r2 = requests.get("https://push2.eastmoney.com/api/qt/stock/get",
                            params=params, headers=UA_HEADERS, timeout=5)
            ed = r2.json().get("data", {})
            if ed:
                price_raw = ed.get("f43")
                if price_raw:
                    results[raw_id]["price"] = price_raw / 100.0
                pe_static = ed.get("f162")
                if pe_static and pe_static > 0 and pe_static < 10000:
                    results[raw_id]["pe"] = pe_static / 100.0
                pe_dynamic = ed.get("f167")
                if pe_dynamic and pe_dynamic > 0 and pe_dynamic < 10000:
                    results[raw_id]["pe_dynamic"] = pe_dynamic / 100.0
                mcap = ed.get("f116")
                if mcap:
                    results[raw_id]["market_cap"] = mcap
                change_raw = ed.get("f169")
                if change_raw:
                    results[raw_id]["change"] = round(change_raw / 100.0, 2)
                pct_raw = ed.get("f170")
                if pct_raw:
                    results[raw_id]["change_pct"] = round(pct_raw / 100.0, 2)
                pb_raw = ed.get("f55")
                if pb_raw and 0 < pb_raw < 1000:
                    results[raw_id]["pb"] = round(pb_raw / 100.0, 2)
        except Exception as e:
            pass  # EastMoney作为增强，失败不影响基础数据
    
    return results


def batch_fetch_pe(symbols, timeout=10):
    """
    批量获取A股PE/股价（东方财富ulist接口，一次请求完成）
    返回: {symbol: {pe, price, market_cap}}
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    
    # 构造 secids
    secids = []
    for sym in symbols:
        ns = normalize_ashare_symbol(sym)
        prefix = ns[:2]
        code = ns[2:]
        mkt = _market_rev.get(prefix, "1")
        secids.append(f"{mkt}.{code}")
    
    result = {}
    try:
        params = {
            "fltt": "2",
            "invt": "2",
            "fields": "f58,f162,f167,f116,f43,f170",
            "secids": ",".join(secids)
        }
        r = requests.get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params=params, headers=UA_HEADERS, timeout=timeout
        )
        data = r.json()
        for item in data.get("data", {}).get("diff", []):
            raw_secid = item.get("f12", "")
            # 从secid反推symbol: "1.600519" -> code是600519
            parts = raw_secid.split(".")
            code = parts[-1] if len(parts) > 1 else raw_secid
            entry = {}
            p = item.get("f43") or item.get("f58")
            if p is not None:
                try:
                    pv = float(str(p).replace(",", ""))
                    if pv > 0:
                        entry["price"] = pv
                except: pass
            pe = item.get("f162") or item.get("f167")
            if pe is not None:
                try:
                    pev = float(str(pe).replace(",", ""))
                    if pev > 0 and pev < 10000:
                        entry["pe"] = pev
                except: pass
            mcap = item.get("f116")
            if mcap:
                entry["market_cap"] = mcap
            pct = item.get("f170")
            if pct:
                entry["change_pct"] = pct / 100.0
            if entry:
                result[code] = entry
    except Exception as e:
        print(f"[batch_pe] error: {e}")
    
    return result


def get_ashare_quote(symbol, timeout=10):
    """单只A股行情"""
    result = get_ashare_quotes([symbol], timeout)
    ns = normalize_ashare_symbol(symbol)
    return result.get(ns, result.get(symbol, {}))


# ====== 2. K线数据 ======

_market_map = {"sh": 1, "sz": 0, "bj": 2}
_market_rev = {"sh": "1", "sz": "0", "bj": "2"}

def get_ashare_kline(symbol, period="daily", start_date="", end_date="", limit=100):
    """
    获取A股K线数据
    period: daily/weekly/monthly
    """
    ns = normalize_ashare_symbol(symbol)
    prefix = ns[:2]
    code = ns[2:]
    market = _market_map.get(prefix, 1)
    
    period_map = {"daily": 101, "weekly": 102, "monthly": 103}
    klt = period_map.get(period, 101)
    
    params = {
        "secid": f"{market}.{code}",
        "ut": "fa5fd1943c7b386f172d6893dbbd4b9c",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "klt": klt,
        "fqt": 1,  # 前复权
        "end": end_date or datetime.now().strftime("%Y%m%d"),
        "lmt": limit,
    }
    if start_date:
        params["beg"] = start_date
    
    try:
        r = requests.get(EASTMONEY_KLINE_URL, params=params, headers=UA_HEADERS, timeout=10)
        data = r.json()
        klines = data.get("data", {}).get("klines", [])
        
        result = []
        for k in klines:
            parts = k.split(",")
            if len(parts) >= 6:
                result.append({
                    "date": parts[0],
                    "open": _float(parts[1]),
                    "close": _float(parts[2]),
                    "high": _float(parts[3]),
                    "low": _float(parts[4]),
                    "volume": _int(parts[5]),
                    "amount": _float(parts[6]) if len(parts) > 6 else 0,
                })
        return result
    except Exception as e:
        print(f"[ashare_kline] {symbol}: {e}")
        return []


# ====== 3. 财报数据 ======

def _ashare_to_secucode(ns):
    """将 sh600519 转为 600519.SH"""
    prefix = ns[:2]
    code = ns[2:]
    mkt = prefix.upper()
    if mkt == "SH":
        mkt = "SH"
    elif mkt == "SZ":
        mkt = "SZ"
    elif mkt == "BJ":
        mkt = "BJ"
    return f"{code}.{mkt}"


def get_ashare_financials_eastmoney(symbol):
    """
    v9.1: 东方财富直连API获取财务数据（替代akshare）
    速度更快，海外访问更稳定，24小时缓存
    
    返回: {income: [{}], balance: [{}], cashflow: [{}]}
    """
    ns = normalize_ashare_symbol(symbol)
    secucode = _ashare_to_secucode(ns)
    cache_key = f"em_fin_{secucode}"
    
    cached = _em_cache_get(cache_key, 86400)
    if cached:
        return cached
    
    result = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    ses = requests.Session()
    
    reports = {
        "income": "RPT_DMSK_FN_INCOME",
        "balance": "RPT_DMSK_FN_BALANCE",
        "cashflow": "RPT_DMSK_FN_CASHFLOW"
    }
    
    for key, report_name in reports.items():
        params = {
            "reportName": report_name,
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": 1,
            "pageSize": 5,
            "sortTypes": -1,
            "sortColumns": "REPORT_DATE"
        }
        try:
            r = ses.get(EASTMONEY_DATACENTER_URL, params=params, headers=headers, timeout=15)
            d = r.json()
            if d.get("result") and d["result"].get("data"):
                result[key] = d["result"]["data"]
        except Exception as e:
            print(f"[em_fin_{key}] {symbol}: {e}")
    
    if result:
        _em_cache_set(cache_key, result, 86400)
    return result


def _map_em_income(row):
    """将东方财富利润表字段映射到统一格式"""
    return {
        "营业总收入": row.get("TOTAL_OPERATE_INCOME"),
        "营业毛利": (row.get("TOTAL_OPERATE_INCOME") or 0) - (row.get("OPERATE_COST") or 0) if row.get("TOTAL_OPERATE_INCOME") and row.get("OPERATE_COST") else None,
        "营业总成本": row.get("TOTAL_OPERATE_COST"),
        "营业利润": row.get("OPERATE_PROFIT"),
        "利润总额": row.get("TOTAL_PROFIT"),
        "净利润": row.get("PARENT_NETPROFIT"),
        "扣非净利润": row.get("DEDUCT_PARENT_NETPROFIT"),
        "销售费用": row.get("SALE_EXPENSE"),
        "管理费用": row.get("MANAGE_EXPENSE"),
        "财务费用": row.get("FINANCE_EXPENSE"),
        "研发费用": row.get("MANAGE_EXPENSE"),  # EM无独立研发费用，用管理费用近似
        "所得税": row.get("INCOME_TAX"),
        "基本每股收益": row.get("BASIC_EPS"),
        "稀释每股收益": row.get("DILUTED_EPS"),
        "报告日": str(row.get("REPORT_DATE", ""))[:10],
    }


def _map_em_balance(row):
    """将东方财富资产负债表字段映射到统一格式"""
    # EM的CURRENT_RATIO和DEBT_ASSET_RATIO返回的是百分比值(如706→7.06), 需/100
    cr_raw = row.get("CURRENT_RATIO")
    dar_raw = row.get("DEBT_ASSET_RATIO")
    
    mapped = {
        "资产总计": row.get("TOTAL_ASSETS"),
        "负债合计": row.get("TOTAL_LIABILITIES"),
        "归属于母公司股东权益合计": row.get("TOTAL_EQUITY"),
        "货币资金": row.get("MONETARYFUNDS"),
        "存货": row.get("INVENTORY"),
        "应收账款": row.get("ACCOUNTS_RECE"),
        "固定资产": row.get("FIXED_ASSET"),
        "流动比率": round(cr_raw / 100, 4) if cr_raw else None,
        "资产负债率": round(dar_raw / 100, 4) if dar_raw else None,
        "报告日": str(row.get("REPORT_DATE", ""))[:10],
    }
    return mapped


def _map_em_cashflow(row):
    """将东方财富现金流量表字段映射到统一格式"""
    return {
        "经营活动产生的现金流量净额": row.get("NETCASH_OPERATE"),
        "投资活动产生的现金流量净额": row.get("NETCASH_INVEST"),
        "筹资活动产生的现金流量净额": row.get("NETCASH_FINANCE"),
        "现金及现金等价物净增加额": row.get("CCE_ADD"),
        "购建固定资产、无形资产和其他长期资产支付的现金": row.get("CONSTRUCT_LONG_ASSET"),
        "销售商品、提供劳务收到的现金": row.get("SALES_SERVICES"),
        "报告日": str(row.get("REPORT_DATE", ""))[:10],
    }

def get_ashare_financials(symbol):
    """
    获取A股财务报表（三大表合一）
    返回: {income: [...], balance: [...], cashflow: [...]}
    """
    try:
        import akshare as ak
    except ImportError:
        print("[ashare_fin] akshare not installed")
        return {}
    
    ns = normalize_ashare_symbol(symbol)
    code = ns[2:]
    
    result = {}
    
    # 利润表
    try:
        df = ak.stock_financial_report_sina(stock=code, symbol="利润表")
        if df is not None and len(df):
            result["income"] = df.head(8).to_dict("records")
    except Exception as e:
        print(f"[ashare_fin_income] {code}: {e}")
    
    # 资产负债表
    try:
        df = ak.stock_financial_report_sina(stock=code, symbol="资产负债表")
        if df is not None and len(df):
            result["balance"] = df.head(8).to_dict("records")
    except Exception as e:
        print(f"[ashare_fin_balance] {code}: {e}")
    
    # 现金流量表
    try:
        df = ak.stock_financial_report_sina(stock=code, symbol="现金流量表")
        if df is not None and len(df):
            result["cashflow"] = df.head(8).to_dict("records")
    except Exception as e:
        print(f"[ashare_fin_cashflow] {code}: {e}")
    
    return result


# ====== 4. 板块成分股 ======

def get_ashare_boards():
    """获取所有A股概念板块列表"""
    params = {
        "pn": 1, "pz": 500, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:90+t:3",
        "fields": "f12,f14,f2,f3,f4,f8,f20,f21",
    }
    try:
        r = requests.get(EASTMONEY_BOARD_URL, params=params, headers=UA_HEADERS, timeout=10)
        data = r.json()
        diff = data.get("data", {}).get("diff", [])
        result = {}
        for item in diff:
            result[item["f14"]] = {
                "code": item["f12"],
                "name": item["f14"],
                "price": _float(item.get("f2")),
                "change_pct": _float(item.get("f3")),
            }
        return result
    except Exception as e:
        print(f"[ashare_boards] error: {e}")
        return {}


def get_ashare_board_stocks(board_code, limit=100):
    """
    获取板块成分股
    board_code: BK1717 等东方财富板块代码
    """
    params = {
        "pn": 1, "pz": limit, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "fid": "f3", "fs": f"b:{board_code}+f:!50",
        "fields": "f12,f14,f2,f3,f4,f8,f20,f21,f100",
    }
    try:
        r = requests.get(EASTMONEY_BOARD_STOCK_URL, params=params, headers=UA_HEADERS, timeout=10)
        data = r.json()
        diff = data.get("data", {}).get("diff", [])
        result = []
        for item in diff:
            result.append({
                "symbol": item["f12"],       # 股票代码
                "name": item["f14"],          # 股票名称
                "price": _float(item.get("f2")),
                "change_pct": _float(item.get("f3")),
                "market_cap": _float(item.get("f20")),  # 总市值(亿)
            })
        return result
    except Exception as e:
        print(f"[ashare_board_stocks] {board_code}: {e}")
        return []


# ====== 5. 利润分析 ======

def parse_ashare_financials(fd, code):
    """
    v9.1: 解析A股财报到统一数据格式
    数据源优先级: 东方财富直连API(主) → akshare/新浪(备)
    fd: dict 数据容器，直接修改
    code: A股6位代码
    """
    # === 1. 东方财富直连API（主数据源）===
    em_fin = get_ashare_financials_eastmoney(code)
    if em_fin:
        # 利润表
        if "income" in em_fin and em_fin["income"]:
            row = _map_em_income(em_fin["income"][0])
            fd["revenue"] = _float(row.get("营业总收入"))
            fd["net_income"] = _float(row.get("净利润"))
            fd["gross_profit"] = _float(row.get("营业毛利"))
            fd["operating_income"] = _float(row.get("营业利润"))
            fd["total_cost"] = _float(row.get("营业总成本"))
            fd["rd_expense"] = _float(row.get("研发费用"))
            fd["sga_expense"] = _float(row.get("销售费用"))
            fd["eps"] = _float(row.get("基本每股收益"))
            fd["report_date"] = str(row.get("报告日", ""))[:10]
            
            # 营收增速(YoY): 取最近两期数据
            if len(em_fin["income"]) >= 2:
                row_prev = _map_em_income(em_fin["income"][1])
                rev_curr = _float(row.get("营业总收入"))
                rev_prev = _float(row_prev.get("营业总收入"))
                if rev_curr and rev_prev and rev_prev > 0:
                    fd["revenue_growth"] = round((rev_curr - rev_prev) / rev_prev, 4)
                ni_curr = _float(row.get("净利润"))
                ni_prev = _float(row_prev.get("净利润"))
                if ni_curr and ni_prev and ni_prev > 0:
                    fd["profit_growth"] = round((ni_curr - ni_prev) / ni_prev, 4)
        
        # 资产负债表
        if "balance" in em_fin and em_fin["balance"]:
            row = _map_em_balance(em_fin["balance"][0])
            fd["total_assets"] = _float(row.get("资产总计"))
            fd["total_liabilities"] = _float(row.get("负债合计"))
            fd["equity"] = _float(row.get("归属于母公司股东权益合计"))
            fd["cash_and_equivalents"] = _float(row.get("货币资金"))
            fd["inventory"] = _float(row.get("存货"))
            fd["accounts_receivable"] = _float(row.get("应收账款"))
            fd["current_ratio"] = _float(row.get("流动比率"))
            fd["debt_ratio"] = _float(row.get("资产负债率"))
        
        # 现金流量表
        if "cashflow" in em_fin and em_fin["cashflow"]:
            row = _map_em_cashflow(em_fin["cashflow"][0])
            fd["operating_cashflow"] = _float(row.get("经营活动产生的现金流量净额"))
            fd["free_cashflow"] = _float(row.get("经营活动产生的现金流量净额"))  # FCF近似
            fd["capex"] = _float(row.get("购建固定资产、无形资产和其他长期资产支付的现金"))
        
        fd["_data_source"] = "eastmoney_api"
        return fd
    
    # === 2. akshare/新浪（备胎）===
    fin = get_ashare_financials(code)
    if not fin:
        return fd
    
    # 利润表
    if "income" in fin and fin["income"]:
        row = fin["income"][0]
        fd["revenue"] = _float_str(row.get("营业总收入"))
        fd["net_income"] = _float_str(row.get("净利润"))
        fd["gross_profit"] = _float_str(row.get("营业毛利"))
        fd["operating_income"] = _float_str(row.get("营业利润"))
        fd["total_cost"] = _float_str(row.get("营业总成本"))
        fd["rd_expense"] = _float_str(row.get("研发费用"))
        fd["sga_expense"] = _float_str(row.get("销售费用"))
        fd["report_date"] = str(row.get("报告日", ""))[:10]
    
    # 资产负债表
    if "balance" in fin and fin["balance"]:
        row = fin["balance"][0]
        fd["total_assets"] = _float_str(row.get("资产总计"))
        fd["total_liabilities"] = _float_str(row.get("负债合计"))
        fd["equity"] = _float_str(row.get("归属于母公司股东权益合计"))
        fd["cash_and_equivalents"] = _float_str(row.get("货币资金"))
        fd["inventory"] = _float_str(row.get("存货"))
        fd["accounts_receivable"] = _float_str(row.get("应收账款"))
        fd["book_value_per_share"] = None
        equity_val = fd.get("equity")
        shares = _float_str(row.get("实收资本(或股本)"))
        if equity_val and shares and shares > 0:
            fd["book_value_per_share"] = round(equity_val / shares, 4)
    
    # 现金流量表
    if "cashflow" in fin and fin["cashflow"]:
        row = fin["cashflow"][0]
        fd["operating_cashflow"] = _float_str(row.get("经营活动产生的现金流量净额"))
        fd["free_cashflow"] = _float_str(row.get("经营活动产生的现金流量净额"))  # FCF近似
        fd["capex"] = _float_str(row.get("购建固定资产、无形资产和其他长期资产支付的现金"))
    
    fd["_data_source"] = "ashare_adapter"
    return fd


# ====== 辅助函数 ======

def _float(v):
    try:
        return float(v) if v not in (None, "", "None", "-", "—") else None
    except:
        return None

def _int(v):
    try:
        return int(float(v)) if v not in (None, "", "None", "-", "—") else 0
    except:
        return 0

def _float_str(v):
    try:
        return float(v) if v not in (None, "", "None", "-", "—") else None
    except:
        return None


# ====== 自测 ======
if __name__ == "__main__":
    print("=" * 50)
    print("A股适配器自测")
    print("=" * 50)
    
    # 1. 行情
    print("\n1. 实时行情:")
    quotes = get_ashare_quotes(["600519", "300750", "000001"])
    for sym, q in quotes.items():
        print(f"  {q['name']}({sym}): {q['price']} ({q.get('change_pct', 0):+.2f}%) PE={q.get('pe_dynamic')}")
    
    # 2. K线
    print("\n2. K线(最近3天):")
    klines = get_ashare_kline("600519", limit=3)
    for k in klines:
        print(f"  {k['date']}: 开={k['open']} 收={k['close']} 量={k['volume']}")
    
    # 3. 板块
    print("\n3. 概念板块(前10):")
    boards = get_ashare_boards()
    for name in list(boards.keys())[:10]:
        print(f"  {name}: {boards[name]['code']}")
    
    # 4. 成分股
    print("\n4. 锂电池板块成分股(前5):")
    board_code = None
    for name, info in boards.items():
        if "锂电池" in name:
            board_code = info["code"]
            break
    if board_code:
        stocks = get_ashare_board_stocks(board_code, limit=5)
        for s in stocks:
            print(f"  {s['symbol']} {s['name']}: {s['price']} ({s.get('change_pct', 0):+.2f}%)")
    
    # 5. 财报
    print("\n5. 茅台财报:")
    fin = get_ashare_financials("600519")
    if fin:
        if "income" in fin:
            r = fin["income"][0]
            print(f"  营收={r.get('营业总收入','?')} 净利={r.get('净利润','?')}")
    
    print("\n✅ 自测完成")
