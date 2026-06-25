# -*- coding: utf-8 -*-
"""数据代理 v8.6 — 三大系统集成：行业分位 + 稳定性 + 风险因子"""
import json, os, re, subprocess, sys, math, statistics
import time as _time
import threading as _threading
from flask import Flask, jsonify, send_from_directory, request, redirect

# ====== 修复: JSON输出处理 NaN/Infinity (JS JSON.parse 不识别 NaN) ======
import math as _math
_orig_json_dumps = json.dumps
def _safe_json_dumps(obj, **kwargs):
    """全局替换NaN/Infinity为null，确保JSON合规"""
    def _sanitize(o):
        if isinstance(o, float):
            if _math.isnan(o) or _math.isinf(o):
                return None
            return o
        if isinstance(o, dict):
            return {k: _sanitize(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_sanitize(i) for i in o]
        return o
    return _orig_json_dumps(_sanitize(obj), **kwargs)
json.dumps = _safe_json_dumps

app = Flask(__name__, static_folder=None)
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = DATA_DIR  # Docker中所有文件在同一目录，本地也在同一目录

# 将适配器目录加入Python路径（quant_tool/ 子目录）
_qt_dir = os.path.join(DATA_DIR, "quant_tool")
if os.path.isdir(_qt_dir) and _qt_dir not in sys.path:
    sys.path.insert(0, _qt_dir)

# v9.0+: 模块级导入SEC EDGAR适配器（确保全局可用，避免运行时导入失败）
_SEC_AVAILABLE = False
try:
    from sec_edgar_adapter import parse_sec_financials
    from us_stock_adapter import calc_us_fiscal_period, get_us_quote
    _SEC_AVAILABLE = True
except Exception as _sec_import_err:
    print(f"[startup] sec_edgar import: {_sec_import_err}")

# ====== 访问密码保护（可选）=====
# 设置 ACCESS_KEY 环境变量后，外部访问需要 ?key=XXX 参数
# 本地访问（127.0.0.1）不需要密码
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
LOCAL_IPS = {"127.0.0.1", "::1", "localhost"}

@app.before_request
def check_access():
    """外部访问需验证密码（如果有设置）"""
    if not ACCESS_KEY:
        return  # 没设密码不拦截
    if request.remote_addr in LOCAL_IPS:
        return  # 本机不拦截
    # 检查URL参数或Cookie中的key
    if request.args.get("key") == ACCESS_KEY:
        return
    if request.cookies.get("quant_key") == ACCESS_KEY:
        return
    # API请求返回JSON错误
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "msg": "需要访问密钥，请在URL加 ?key=你的密钥"})
    # 页面请求重定向到密码页
    return f"""<html><body style="background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:40px;text-align:center">
    <h2>🔒 量化分析 - 需要访问密钥</h2>
    <form method="get">
      <input name="key" placeholder="输入密钥" style="padding:8px;width:200px;border-radius:4px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9">
      <button type="submit" style="padding:8px 16px;background:#58a6ff;border:none;border-radius:4px;color:white;cursor:pointer">验证</button>
    </form>
    <p class="s">提示：找管理员获取访问密钥</p></body></html>"""

# ====== 速率限制 (v9.0+) ======
# 每IP每分钟最多N次请求，超限返回429
RATE_LIMIT_PER_MIN = 60
_rl_store = {}
_rl_lock = _threading.Lock()

def _rl_check(ip):
    now = _time.time()
    with _rl_lock:
        ts = _rl_store.get(ip, [])
        ts = [t for t in ts if now - t < 60]
        if len(ts) >= RATE_LIMIT_PER_MIN:
            _rl_store[ip] = ts
            return False
        ts.append(now)
        _rl_store[ip] = ts
        return True

# ====== 内存缓存 (v9.0+) ======
_cache_store = {}
_cache_lock = _threading.Lock()
CACHE_TTL = {"stock": 300, "news": 1800}

def _cache_get(key):
    with _cache_lock:
        e = _cache_store.get(key)
        if e and _time.time() < e[1]:
            return e[0]
        if e:
            del _cache_store[key]
    return None

def _cache_set(key, data, ttl):
    with _cache_lock:
        _cache_store[key] = (data, _time.time() + ttl)

# ====== 数据源路径 ======
# v9.0+: 跨平台Node路径 — Windows用托管路径，Docker/Linux用系统PATH
if os.name == "nt":
    NODE = r"C:\Users\ASUS\.workbuddy\binaries\node\versions\22.22.2\node.exe"
    NPX = r"C:\Users\ASUS\.workbuddy\binaries\node\versions\22.22.2\npx.cmd"
else:
    NODE = "node"
    NPX = "npx"
# v7.5: westock-data-clawhub 是npm包，通过 npx -y 调用
WESTOCK_PKG = "westock-data-clawhub@1.0.4"
WESTOCK_CMD = [NPX, "-y", WESTOCK_PKG]

# ====== 行业数据库 ======
# v7.5: 中文行业名→内部key映射（替代硬编码SECTOR_MAP）
SECTOR_CN_MAP = {
    # 半导体/电子
    "半导体": "semicon", "电子技术": "semicon",
    "电子元器件": "semicon", "信息技术": "semicon",
    "光学光电子": "semicon", "消费电子": "tech_hardware",
    "技术硬件": "tech_hardware", "电脑周边": "tech_hardware",
    "计算机设备": "tech_hardware", "自动化设备": "tech_hardware",
    # 软件/互联网/传媒
    "软件服务": "saas", "计算机软件": "saas",
    "软件开发": "saas", "技术服务": "saas",
    "互联网": "internet", "互联网服务": "internet",
    "通信服务": "internet", "网络": "internet",
    "社交平台": "internet", "电子商务": "internet",
    "电视广播": "internet", "广告传媒": "internet",
    # 金融
    "金融": "finance", "银行": "finance", "保险": "finance",
    "证券": "finance", "多元金融": "finance", "投资": "finance",
    "资产管理": "finance", "支付": "finance",
    # 医疗健康
    "健康技术": "health", "健康服务": "health",
    "医药生物": "health", "医疗": "health", "医疗器械": "health",
    "制药": "health", "生物科技": "health",
    "医疗保健": "health", "大型药物": "health",
    "中药": "health", "化学制药": "health", "医疗服务": "health",
    # 消费(食品饮料/白酒/家电/汽车)
    "非耐用消费品": "consumer", "耐用消费品": "consumer",
    "食品饮料": "consumer", "消费品": "consumer", "家用电器": "consumer",
    "消费者服务": "consumer",
    "白酒": "consumer", "调味发酵品": "consumer", "饮料乳品": "consumer",
    "白色家电": "consumer", "乘用车": "auto",
    "汽车": "auto", "汽车零部件": "auto", "汽车制造": "auto",
    "零售": "retail", "商业贸易": "retail",
    "服装": "consumer", "奢侈品": "consumer", "软饮料": "consumer",
    "家庭": "consumer", "个人护理": "consumer",
    # 能源/资源
    "能源矿产": "energy", "石油化工": "energy", "采掘": "energy",
    "石油天然气": "energy", "新能源": "energy",
    "能源": "energy", "综合性石油": "energy",
    "煤炭开采": "energy", "炼化及贸易": "energy",
    "化学制品": "energy", "化学原料": "energy", "化工": "energy",
    # 工业/制造/建设
    "机械设备": "industrial", "工业": "industrial", "电气设备": "industrial",
    "工业机械": "industrial", "生产制造": "industrial",
    "工程机械": "industrial", "电网设备": "industrial",
    "光伏设备": "industrial", "电池": "industrial",
    "专业工程": "industrial", "基础建设": "industrial",
    "房屋建设": "industrial", "水泥": "industrial",
    "建筑材料": "materials", "有色金属": "materials",
    "钢铁": "materials", "金属": "materials", "工业金属": "materials",
    # 军工/国防
    "航空航天": "defense", "军工": "defense",
    "国防": "defense", "航空装备": "defense", "航海装备": "defense",
    # 公用事业
    "公用事业": "utility", "电力": "utility",
    # 房地产
    "房地产": "real_estate",
    # 其他
    "农林牧渔": "agriculture",
    "交通运输": "transport", "物流": "transport",
}
# 少量已知有争议的股票手动覆盖（只保留真正有歧义的）
SECTOR_OVERRIDE = {
    # 生产制造→但实际是半导体设备
    'AMAT': 'semicon', 'LRCX': 'semicon', 'KLAC': 'semicon',
    'LITE': 'semicon', 'COHR': 'semicon', 'AEHR': 'semicon',
    'ENTG': 'semicon', 'ONTO': 'semicon', 'VRT': 'semicon',
    'CEG': 'utility', 'VST': 'utility',
    # 技术服务→但实际是不同行业
    'CDNS': 'semicon', 'SNPS': 'semicon',  # EDA→半导体
    'GOOG': 'internet', 'GOOGL': 'internet',
    'CRM': 'saas', 'NOW': 'saas', 'ZS': 'saas',
    # 其他覆盖
    'AAPL': 'tech_hardware', 'MSFT': 'saas',
    'META': 'internet', 'AMZN': 'internet',
    'NVDA': 'semicon', 'AMD': 'semicon', 'TSLA': 'auto',
    'WMT': 'retail',
    'GE': 'defense', 'RTX': 'defense',  # 航空发动机/军火
    'RKLB': 'defense',  # 航天
}

INDUSTRY_BENCHMARKS = {
    "semicon": {"n":"半导体","rg":{"p25":0.10,"p50":0.20,"p75":0.35},"gm":{"p25":0.45,"p50":0.55,"p75":0.65},"nm":{"p25":0.15,"p50":0.25,"p75":0.35},"roe":{"p25":0.12,"p50":0.20,"p75":0.30},"roic":{"p25":0.10,"p50":0.18,"p75":0.28},"pe":{"p25":18,"p50":28,"p75":45},"ps":{"p25":5,"p50":10,"p75":18}},
    "saas":{"n":"SaaS","rg":{"p25":0.20,"p50":0.35,"p75":0.60},"gm":{"p25":0.65,"p50":0.75,"p75":0.85},"nm":{"p25":-0.05,"p50":0.08,"p75":0.20},"roe":{"p25":0.08,"p50":0.18,"p75":0.30},"roic":{"p25":0.10,"p50":0.20,"p75":0.35},"pe":{"p25":30,"p50":55,"p75":100},"ps":{"p25":8,"p50":15,"p75":25}},
    "internet":{"n":"互联网","rg":{"p25":0.12,"p50":0.25,"p75":0.45},"gm":{"p25":0.50,"p50":0.65,"p75":0.80},"nm":{"p25":0.10,"p50":0.20,"p75":0.35},"roe":{"p25":0.12,"p50":0.22,"p75":0.35},"pe":{"p25":20,"p50":32,"p75":55},"ps":{"p25":4,"p50":8,"p75":15}},
    "tech_hardware":{"n":"科技硬件","rg":{"p25":0.05,"p50":0.12,"p75":0.25},"gm":{"p25":0.35,"p50":0.45,"p75":0.55},"nm":{"p25":0.10,"p50":0.18,"p75":0.28},"roe":{"p25":0.20,"p50":0.40,"p75":0.60},"pe":{"p25":15,"p50":25,"p75":38},"ps":{"p25":2,"p50":5,"p75":10}},
    "retail":{"n":"零售","rg":{"p25":0.03,"p50":0.06,"p75":0.12},"gm":{"p25":0.22,"p50":0.30,"p75":0.40},"nm":{"p25":0.02,"p50":0.04,"p75":0.08},"roe":{"p25":0.10,"p50":0.18,"p75":0.28},"pe":{"p25":14,"p50":20,"p75":30},"ps":{"p25":0.4,"p50":0.7,"p75":1.5}},
    "consumer":{"n":"消费品","rg":{"p25":0.02,"p50":0.05,"p75":0.10},"gm":{"p25":0.35,"p50":0.50,"p75":0.65},"nm":{"p25":0.10,"p50":0.18,"p75":0.28},"roe":{"p25":0.20,"p50":0.35,"p75":0.50},"pe":{"p25":18,"p50":25,"p75":35},"ps":{"p25":2,"p50":4,"p75":7}},
    "finance":{"n":"金融","rg":{"p25":0.03,"p50":0.06,"p75":0.12},"gm":{"p25":0.20,"p50":0.35,"p75":0.55},"nm":{"p25":0.15,"p50":0.25,"p75":0.35},"roe":{"p25":0.08,"p50":0.12,"p75":0.18},"pe":{"p25":8,"p50":12,"p75":18},"pb":{"p25":0.8,"p50":1.2,"p75":2.0}},
    "health":{"n":"医疗","rg":{"p25":0.04,"p50":0.08,"p75":0.18},"gm":{"p25":0.55,"p50":0.68,"p75":0.80},"nm":{"p25":0.12,"p50":0.20,"p75":0.30},"roe":{"p25":0.12,"p50":0.20,"p75":0.30},"pe":{"p25":14,"p50":22,"p75":35},"ps":{"p25":3,"p50":5,"p75":10}},
    "energy":{"n":"能源","rg":{"p25":-0.05,"p50":0.05,"p75":0.18},"gm":{"p25":0.15,"p50":0.25,"p75":0.40},"nm":{"p25":0.05,"p50":0.10,"p75":0.20},"roe":{"p25":0.06,"p50":0.12,"p75":0.22},"pe":{"p25":8,"p50":12,"p75":20},"ps":{"p25":0.5,"p50":1.0,"p75":2.0}},
    "auto":{"n":"汽车","rg":{"p25":0.02,"p50":0.08,"p75":0.20},"gm":{"p25":0.10,"p50":0.18,"p75":0.28},"nm":{"p25":0.03,"p50":0.06,"p75":0.12},"roe":{"p25":0.06,"p50":0.12,"p75":0.20},"pe":{"p25":8,"p50":15,"p75":25},"ps":{"p25":0.3,"p50":0.6,"p75":1.2}},
    # v7.5: 通用后备（行业识别失败时使用，基于全市场中位数估算）
    "general":{"n":"通用","rg":{"p25":0.02,"p50":0.08,"p75":0.20},"gm":{"p25":0.30,"p50":0.45,"p75":0.60},"nm":{"p25":0.05,"p50":0.12,"p75":0.22},"roe":{"p25":0.05,"p50":0.12,"p75":0.25},"roic":{"p25":0.05,"p50":0.10,"p75":0.20},"pe":{"p25":15,"p50":22,"p75":35},"ps":{"p25":2,"p50":5,"p75":12}},
}

# ====== 工具函数 ======
def westock(args, timeout=15):
    """调用 westock-data-clawhub npm 包获取数据"""
    try:
        cmd = WESTOCK_CMD + args
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        try: out = r.stdout.decode("utf-8").strip()
        except: out = r.stdout.decode("gbk", errors="replace").strip()
        return out
    except Exception as e:
        print(f"[westock] error: {e}")
        return ""

def parse_md_table(md):
    lines = [l.strip() for l in md.split("\n") if l.strip()]
    if len(lines) < 3: return []
    header_idx = -1
    for i, l in enumerate(lines):
        if l.startswith("|") and "---" not in l: header_idx = i; break
    if header_idx < 0: return []
    headers = [h.strip() for h in lines[header_idx].split("|") if h.strip()]
    results = []
    for i in range(header_idx + 2, len(lines)):
        l = lines[i]
        if not l.startswith("|"): continue
        cells = [c.strip() for c in l.split("|") if c.strip()]
        if len(cells) >= len(headers): cells = cells[:len(headers)]
        elif len(cells) < len(headers): cells += [""] * (len(headers) - len(cells))
        row = {}
        for j, h in enumerate(headers): row[h] = cells[j]
        results.append(row)
    return results

def _fmt_num(s):
    if not s or s in ("-", "—", ""): return None
    s = s.replace(",", "").replace(" ", "")
    try: return float(s)
    except: return None

# ====== 行业分位数系统 ======
def calc_percentile(value, sector, metric):
    """计算指标在行业中的分位数 (0-100)"""
    if value is None or sector not in INDUSTRY_BENCHMARKS: return None
    ind = INDUSTRY_BENCHMARKS[sector]
    if metric not in ind: return None
    bm = ind[metric]
    p25, p50, p75 = bm["p25"], bm["p50"], bm["p75"]
    # 线性插值
    if value <= p25:
        pct = 25 * (value / p25) if p25 != 0 else 25
    elif value <= p50:
        pct = 25 + 25 * ((value - p25) / (p50 - p25)) if (p50 - p25) > 0 else 50
    elif value <= p75:
        pct = 50 + 25 * ((value - p50) / (p75 - p50)) if (p75 - p50) > 0 else 75
    else:
        pct = 75 + 25 * min((value - p75) / p75, 1) if p75 != 0 else 100
    pct = max(0, min(100, pct))
    if pct >= 75: rating = "优秀"
    elif pct >= 50: rating = "良好"
    elif pct >= 25: rating = "中等"
    else: rating = "偏弱"
    return {"pct": round(pct, 1), "rating": rating, "p50": p50, "p25": p25, "p75": p75}

# ====== 稳定性评分系统 ======
def calc_stability(series, metric_name):
    """计算多期数据稳定性"""
    clean = [x for x in series if x is not None]
    if len(clean) < 4: return None
    n = len(clean)
    mean = sum(clean) / n
    variance = sum((x - mean) ** 2 for x in clean) / n
    std = math.sqrt(variance)
    cv = abs(std / mean) if mean != 0 else 0

    # 趋势（线性回归斜率）
    sum_xy = sum(i * clean[i] for i in range(n))
    sum_x = sum(range(n))
    sum_y = sum(clean)
    sum_x2 = sum(i * i for i in range(n))
    slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x) if (n * sum_x2 - sum_x * sum_x) != 0 else 0

    # 趋势判断
    threshold = abs(mean) * 0.05 if abs(mean) > 0.001 else 0.001
    if slope > threshold: trend = "上升"
    elif slope < -threshold: trend = "下降"
    else: trend = "平稳"

    # 稳定性分数 0-100
    if cv < 0.05: s = 95
    elif cv < 0.10: s = 82
    elif cv < 0.20: s = 65
    elif cv < 0.35: s = 40
    else: s = 15

    return {"std": round(std, 4), "cv": round(cv, 4), "score": s, "trend": trend, "mean": round(mean, 4)}

# ====== 周期位置系统 ======
def calc_cycle_position(fd, multi):
    signals = []
    score = 0

    # 1. PE陷阱检测
    pe = fd.get("pe")
    if pe and pe > 0:
        if pe < 8:
            signals.append(f"PE={pe}<8(周期顶陷阱)")
            score += 15
        elif pe > 25:
            signals.append(f"PE={pe}>25(底部反转)")
            score -= 10

    # 2. 毛利率趋势 (最新vs4季前)
    gm_series = multi.get("gross_margin", [])
    if len(gm_series) >= 5:
        gm_chg = (gm_series[-1] - gm_series[-5]) / abs(gm_series[-5]) if gm_series[-5] != 0 else 0
        if gm_chg > 0.10:
            signals.append(f"毛利率升{gm_chg*100:.0f}%(涨价)")
            score -= 8
        elif gm_chg < -0.10:
            signals.append(f"毛利率降{abs(gm_chg)*100:.0f}%(降价)")
            score += 8

    # 3. EBITDA历史位置 (最新值 vs 均值)
    ebitda_series = multi.get("ebitda", [])
    if len(ebitda_series) >= 4:
        cur = ebitda_series[-1]  # 最新
        avg = sum(ebitda_series) / len(ebitda_series)
        if avg > 0:
            ratio = cur / avg
            if ratio > 1.3:
                signals.append(f"EBITDA高位(均值{ratio:.1f}x)")
                score += 10
            elif ratio < 0.7:
                signals.append(f"EBITDA低位(均值{ratio:.1f}x)")
                score -= 10

    # 判断位置
    if score < -12: pos = "周期底部"; conf = 85
    elif score < 0: pos = "周期上升"; conf = 65
    elif score < 8: pos = "周期中性"; conf = 50
    elif score < 18: pos = "周期下降"; conf = 65
    else: pos = "周期顶部"; conf = 85

    return {"position": pos, "confidence": conf, "signals": signals} if signals else None

# ====== 主获取函数 ======
def search_stock(name):
    """通过westock搜索中文股票名称，返回(westock_code, display_name)或(None,None)"""
    try:
        md = westock(["search", name, "--stock"], 15)
        rows = parse_md_table(md)
        if not rows:
            return None, None
        # 按市场优先级排序: A股(sh/sz) > 港股(hk) > 美股(us)
        def priority(row):
            c = row.get("code", "")
            if c.startswith("sh") or c.startswith("sz"): return 0
            if c.startswith("hk"): return 1
            return 2
        rows.sort(key=priority)
        best = rows[0]
        code = best.get("code", "").strip()
        disp = best.get("name", name)
        if code:
            return code, disp
    except Exception:
        pass
    return None, None

def _normalize_symbol(symbol):
    """自动识别股票市场并转换为westock格式，支持中文名称"""
    s = symbol.strip()
    # 检查是否包含中文（非ASCII字符）
    if any(ord(c) > 127 for c in s):
        code, _ = search_stock(s)
        if code:
            # code已是westock格式，直接返回
            return code.lower()
        return s.lower()  # fallback
    s = s.upper()
    # 已有市场前缀
    for prefix in ("US", "SH", "SZ", "HK", "SG"):
        if s.startswith(prefix):
            return s.lower() if prefix != "US" else f"us{s[2:]}"
    # 纯数字
    if s.isdigit():
        if len(s) == 6:
            if s[0] in ("0", "3"):
                return f"sz{s}"  # 深圳主板/创业板
            elif s[0] == "6":
                return f"sh{s}"  # 上海主板
            elif s[0] == "4":
                return f"sh{s}"  # 新三板
            elif s[0] == "8":
                return f"bj{s}"  # 北交所
        elif len(s) == 5:
            return f"hk{s}"  # 港股
    # 默认美股
    return f"us{s}"

# ====== 备用数据源: westock缺失时自动补数 ======
def _fetch_fallback(fd, stock_code, prefix, sym):
    """
    从 yfinance(美股/港股) 或 akshare(A股) 补充缺失数据。
    
    两种场景：
    1. westock 完全没数据 → 强依赖 fallback 获取全部财务+市场数据
    2. westock 有财务数据但缺少市场数据(股息率/PE等) → 仅补充市场数据
    """
    import time as _t, random as _r
    has_income = fd.get("revenue") is not None or fd.get("net_income") is not None
    
    if not has_income:
        # 场景1: 完全无数据 → 强依赖 fallback
        _t.sleep(0.2 + _r.random()*0.5)
        if prefix == "us":
            _fallback_yfinance_timeout(fd, sym, max_wait=10)
            if fd.get("revenue") is None:
                _fallback_akshare_us(fd, sym)
        elif prefix == "hk":
            _fallback_yfinance_hk(fd, sym)
        elif prefix in ("sh", "sz"):
            _fallback_akshare(fd, stock_code, prefix, sym)
    # else: westock有数据时不再yfinance补数据（yfinance限流严重，反而造成超时）
    #      PE/PB/PS/市值已在衍生计算中自动完成

def _fallback_yfinance_timeout(fd, sym, max_wait=10):
    """yfinance 带超时包装：用线程池实现，max_wait秒内未完成则放弃"""
    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=1) as ex:
        f = ex.submit(_fallback_yfinance, fd, sym)
        try:
            f.result(timeout=max_wait)
        except:
            pass  # 超时或失败都继续

def _fallback_yfinance(fd, sym):
    """美股: yfinance 季度财报补数（含自动重试+防限流）"""
    import yfinance as yf, time as _t, random as _r
    # 自定义session避免被限流
    import requests as _req
    _s = _req.Session()
    _s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    yf_data = yf.Ticker(sym, session=_s)
    
    # 补行情数据（带重试，快速失败）
    for attempt in range(2):
        try:
            info = yf_data.info
            if info:
                for k, yk in [("price","currentPrice"),("price","regularMarketPrice"),
                              ("price","previousClose"),("market_cap","marketCap"),
                              ("pe","trailingPE"),("pe_forward","forwardPE"),
                              ("pb","priceToBook"),("dividend_yield","dividendYield"),
                              ("high_52w","fiftyTwoWeekHigh"),("low_52w","fiftyTwoWeekLow"),
                              ("volume","volume")]:
                    val = info.get(yk)
                    if val is not None and fd.get(k) is None:
                        if k in ("dividend_yield",):
                            fd[k] = val / 100.0 if val > 1 else val
                        else:
                            fd[k] = val
                # v7.5: 从yfinance info补充增长数据
                if fd.get("revenue_growth") is None:
                    rg = info.get("revenueGrowth")
                    if rg is not None: fd["revenue_growth"] = float(rg)
                if fd.get("profit_growth") is None:
                    eg = info.get("earningsGrowth")
                    if eg is not None: fd["profit_growth"] = float(eg)
                if fd.get("roe") is None:
                    roe_v = info.get("returnOnEquity")
                    if roe_v is not None: fd["roe"] = float(roe_v)
                if fd.get("beta") is None:
                    bv = info.get("beta")
                    if bv is not None: fd["beta"] = float(bv)
                name = info.get("longName") or info.get("shortName")
                if name and fd.get("_name") is None: fd["_name"] = name
                if "sector" in info: fd["_yf_sector"] = info.get("sector","")
            break  # 成功
        except yf.exceptions.YFRateLimitError:
            if attempt < 1:
                _t.sleep(1.0 + _r.random())  # 1~2秒延迟重试
            else:
                break  # 限流就跳过
        except:
            if attempt < 1: _t.sleep(0.5)
            else: break
    
    # 财务报表（带重试，快速失败）
    for attempt in range(2):
        try:
            q = yf_data.quarterly_financials
            bs = yf_data.quarterly_balance_sheet
            cf = yf_data.quarterly_cashflow
            if q is not None and len(q.columns) > 0:
                _apply_yfinance_income(fd, q[q.columns[0]].to_dict())
                # v7.5: 用yfinance多期季度数据计算YoY营收/利润增长
                if fd.get("revenue_growth") is None and len(q.columns) >= 5:
                    r0 = q[q.columns[0]].to_dict()
                    r4 = q[q.columns[4]].to_dict()
                    rev0 = r0.get("Total Revenue")
                    rev4 = r4.get("Total Revenue")
                    ni0 = r0.get("Net Income")
                    ni4 = r4.get("Net Income")
                    if rev0 and rev4 and rev4 > 0:
                        fd["revenue_growth"] = float((rev0 - rev4) / rev4)
                    if ni0 and ni4 and ni4 > 0:
                        fd["profit_growth"] = float((ni0 - ni4) / ni4)
            else:
                _apply_yfinance_income(fd, {})
            _apply_yfinance_bs(fd, bs)
            _apply_yfinance_cf(fd, cf)
            if fd.get("revenue"):
                fd["_fallback_source"] = "yfinance"
            break
        except yf.exceptions.YFRateLimitError:
            if attempt < 1:
                _t.sleep(2.0 + _r.random())
            else:
                break
        except:
            if attempt < 1: _t.sleep(0.5)
            else: break

def _fallback_yfinance_hk(fd, sym):
    """港股: yfinance 年报补数（季度数据不全）"""
    import yfinance as yf
    try:
        t = yf.Ticker(f"{sym}.HK")
        # 先用季度
        q = t.quarterly_financials
        q_good = False
        if q is not None and len(q.columns) > 0:
            r = q[q.columns[0]].to_dict()
            for idx in ["Total Revenue", "Net Income", "Gross Profit", "EBIT", "EBITDA"]:
                if idx in r and r[idx] == r[idx] and r[idx] is not None:
                    q_good = True
                    break
            else:
                q_good = False
            if q_good:
                # 季报有完整数据
                _apply_yfinance_income(fd, r)
            else:
                # 季报不全, 用年报
                fin = t.financials
                if fin is not None and len(fin.columns) > 0:
                    r_ann = fin[fin.columns[0]].to_dict()
                    _apply_yfinance_income(fd, r_ann)
        # 资产负债表和现金流用季报（通常都有）
        bs = t.quarterly_balance_sheet
        cf = t.quarterly_cashflow
        _apply_yfinance_bs(fd, bs)
        _apply_yfinance_cf(fd, cf)
    except Exception as e:
        print(f"[yfinance HK fallback] {sym}: {e}")

def _apply_yfinance_income(fd, r):
    """从 yfinance 利润表字典中提取通用字段"""
    if "Total Revenue" in r and r["Total Revenue"] == r["Total Revenue"]:
        if fd.get("revenue") is None: fd["revenue"] = float(r["Total Revenue"])
    if "Net Income" in r and r["Net Income"] == r["Net Income"]:
        if fd.get("net_income") is None: fd["net_income"] = float(r["Net Income"])
    if "Gross Profit" in r and r["Gross Profit"] == r["Gross Profit"]:
        if fd.get("gross_profit") is None: fd["gross_profit"] = float(r["Gross Profit"])
        if fd.get("revenue") and fd["revenue"] > 0 and fd.get("gross_margin") is None:
            fd["gross_margin"] = fd["gross_profit"] / fd["revenue"]
    if "Cost Of Revenue" in r and r["Cost Of Revenue"] == r["Cost Of Revenue"]:
        if fd.get("cogs") is None: fd["cogs"] = float(r["Cost Of Revenue"])
    if "EBIT" in r and r["EBIT"] == r["EBIT"]:
        if fd.get("ebit") is None: fd["ebit"] = float(r["EBIT"])
    if "EBITDA" in r and r["EBITDA"] == r["EBITDA"]:
        if fd.get("ebitda") is None: fd["ebitda"] = float(r["EBITDA"])
    if "Diluted EPS" in r and r["Diluted EPS"] == r["Diluted EPS"]:
        if fd.get("eps") is None: fd["eps"] = float(r["Diluted EPS"])
    if fd.get("net_income") and fd.get("revenue") and fd["revenue"] > 0 and fd.get("net_margin") is None:
        fd["net_margin"] = fd["net_income"] / fd["revenue"]

def _apply_yfinance_bs(fd, bs):
    """从 yfinance 资产负债表中提取通用字段"""
    if bs is None or len(bs.columns) == 0:
        return
    r = bs[bs.columns[0]].to_dict()
    if "Total Assets" in r and r["Total Assets"] == r["Total Assets"]:
        if fd.get("total_assets") is None: fd["total_assets"] = float(r["Total Assets"])
    if "Stockholders Equity" in r and r["Stockholders Equity"] == r["Stockholders Equity"]:
        if fd.get("equity") is None: fd["equity"] = float(r["Stockholders Equity"])
    if "Cash Cash Equivalents And Short Term Investments" in r:
        if fd.get("cash_short_term") is None: fd["cash_short_term"] = float(r["Cash Cash Equivalents And Short Term Investments"])
    elif "Cash And Cash Equivalents" in r:
        if fd.get("cash_short_term") is None: fd["cash_short_term"] = float(r["Cash And Cash Equivalents"])
    if "Receivables" in r:
        if fd.get("receivables") is None: fd["receivables"] = float(r["Receivables"])
    elif "Accounts Receivable" in r:
        if fd.get("receivables") is None: fd["receivables"] = float(r["Accounts Receivable"])
    if "Inventory" in r and r["Inventory"] == r["Inventory"]:
        if fd.get("inventory") is None: fd["inventory"] = float(r["Inventory"])
    if "Current Assets" in r and "Current Liabilities" in r:
        ca, cl = float(r["Current Assets"]), float(r["Current Liabilities"])
        if cl > 0 and fd.get("current_ratio") is None:
            fd["current_ratio"] = ca / cl
    if "Total Debt" in r and "Long Term Debt" in r:
        if fd.get("_long_term_debt") is None: fd["_long_term_debt"] = float(r["Long Term Debt"])
        if fd.get("_short_term_debt") is None: fd["_short_term_debt"] = float(r["Total Debt"]) - float(r["Long Term Debt"])

def _apply_yfinance_cf(fd, cf):
    """从 yfinance 现金流量表中提取通用字段"""
    if cf is None or len(cf.columns) == 0:
        return
    r = cf[cf.columns[0]].to_dict()
    op_cf_key = "Operating Cash Flow" if "Operating Cash Flow" in r else "Cash Flow From Continuing Operating Activities"
    if op_cf_key in r and r[op_cf_key] == r[op_cf_key]:
        if fd.get("operating_cf") is None: fd["operating_cf"] = float(r[op_cf_key])
    if "Capital Expenditure" in r and r["Capital Expenditure"] == r["Capital Expenditure"]:
        capex = abs(float(r["Capital Expenditure"]))
        if fd.get("capex") is None: fd["capex"] = capex
        if fd.get("operating_cf") and fd.get("free_cf") is None:
            fd["free_cf"] = fd["operating_cf"] - capex
    if "Free Cash Flow" in r and r["Free Cash Flow"] == r["Free Cash Flow"]:
        if fd.get("free_cf") is None: fd["free_cf"] = float(r["Free Cash Flow"])

def _fallback_akshare(fd, stock_code, prefix, sym):
    """A股: akshare/新浪财报补数"""
    import akshare as ak
    try:
        a_code = f"{prefix}{sym}"
        # 利润表
        try:
            df = ak.stock_financial_report_sina(stock=a_code, symbol='利润表')
            if df is not None and len(df) > 0:
                r = df.iloc[0].to_dict()
                if "营业收入" in r:
                    rev = float(r["营业收入"])
                    if rev > 0 and fd.get("revenue") is None:
                        fd["revenue"] = rev
                if "营业总收入" in r and fd.get("revenue") is None:
                    rev = float(r["营业总收入"])
                    if rev > 0: fd["revenue"] = rev
                if "净利润" in r:
                    ni = float(r["净利润"])
                    if fd.get("net_income") is None: fd["net_income"] = ni
                if "营业成本" in r:
                    cogs = float(r["营业成本"])
                    if fd.get("cogs") is None: fd["cogs"] = cogs
                    if fd.get("revenue") and fd["revenue"] > 0 and fd.get("gross_margin") is None:
                        fd["gross_margin"] = (fd["revenue"] - cogs) / fd["revenue"]
                if "营业利润" in r:
                    if fd.get("ebit") is None: fd["ebit"] = float(r["营业利润"])
                if "稀释每股收益" in r:
                    if fd.get("eps") is None: fd["eps"] = float(r["稀释每股收益"])
                elif "基本每股收益" in r:
                    if fd.get("eps") is None: fd["eps"] = float(r["基本每股收益"])
                if fd.get("net_income") and fd.get("revenue") and fd["revenue"] > 0 and fd.get("net_margin") is None:
                    fd["net_margin"] = fd["net_income"] / fd["revenue"]
                fd["_fallback_source"] = "akshare"
        except Exception as e:
            print(f"[akshare income fallback] {sym}: {e}")
        # 资产负债表
        try:
            df_bs = ak.stock_financial_report_sina(stock=a_code, symbol='资产负债表')
            if df_bs is not None and len(df_bs) > 0:
                r_bs = df_bs.iloc[0].to_dict()
                if "资产总计" in r_bs:
                    if fd.get("total_assets") is None: fd["total_assets"] = float(r_bs["资产总计"])
                if "归属于母公司股东权益合计" in r_bs:
                    if fd.get("equity") is None: fd["equity"] = float(r_bs["归属于母公司股东权益合计"])
                if "货币资金" in r_bs:
                    if fd.get("cash_short_term") is None: fd["cash_short_term"] = float(r_bs["货币资金"])
                if "应收账款" in r_bs:
                    if fd.get("receivables") is None: fd["receivables"] = float(r_bs["应收账款"])
                if "存货" in r_bs:
                    if fd.get("inventory") is None: fd["inventory"] = float(r_bs["存货"])
                if "负债合计" in r_bs and "资产总计" in r_bs:
                    tl = float(r_bs["负债合计"])
                    ta = float(r_bs["资产总计"])
                    if ta > 0 and fd.get("debt_ratio") is None:
                        fd["debt_ratio"] = tl / ta
                if "流动资产" in r_bs and "流动负债" in r_bs:
                    ca = float(r_bs["流动资产"])
                    cl = float(r_bs["流动负债"])
                    if cl > 0 and fd.get("current_ratio") is None:
                        fd["current_ratio"] = ca / cl
        except Exception as e:
            print(f"[akshare bs fallback] {sym}: {e}")
    except Exception as e:
        print(f"[akshare fallback] {sym}: {e}")

def _fallback_akshare_us(fd, sym):
    """美股: akshare 东方财富+新浪 行情与财报补数"""
    import akshare as ak
    
    # ── 行情数据（新浪美股，0.2s快速返回） ──
    try:
        df = ak.stock_us_daily(symbol=sym)
        if df is not None and len(df) > 0:
            df = df.sort_values('date')
            latest = df.iloc[-1]
            if fd.get("price") is None and latest.get("close"):
                fd["price"] = float(latest["close"])
            if fd.get("volume") is None and latest.get("volume"):
                fd["volume"] = float(latest["volume"])
            # 52周高低
            n = min(len(df), 252)
            yearly = df.tail(n)
            if fd.get("high_52w") is None:
                fd["high_52w"] = float(yearly['high'].max())
            if fd.get("low_52w") is None:
                fd["low_52w"] = float(yearly['low'].min())
    except Exception as e:
        print(f"[akshare us quote] {sym}: {e}")
    
    try:

        # ── 利润表（综合损益表） ──
        try:
            df = ak.stock_financial_us_report_em(stock=sym, symbol='综合损益表', indicator='年报')
            if df is not None and len(df) > 0:
                latest = df[df['REPORT']==df['REPORT'].max()]
                if len(latest) > 0:
                    items = dict(zip(latest['STD_ITEM_CODE'], latest['AMOUNT']))
                    if "004001999" in items and items["004001999"] > 0:
                        if fd.get("revenue") is None: fd["revenue"] = float(items["004001999"])
                        fd["_fallback_source"] = "akshare_us"
                    elif "004001001" in items and items["004001001"] > 0:
                        if fd.get("revenue") is None: fd["revenue"] = float(items["004001001"])
                        fd["_fallback_source"] = "akshare_us"
                    if "004003001" in items and "004001999" in items:
                        rev = items.get("004001999", 0) or items.get("004001001", 0)
                        cogs = items.get("004003001", 0)
                        if rev > 0 and cogs > 0 and fd.get("gross_margin") is None and fd.get("gross_profit") is None:
                            fd["gross_profit"] = rev - cogs
                            fd["gross_margin"] = (rev - cogs) / rev
                    elif "004005999" in items and items["004005999"] > 0 and fd.get("gross_profit") is None:
                        fd["gross_profit"] = float(items["004005999"])
                        if fd.get("revenue") and fd["revenue"] > 0:
                            fd["gross_margin"] = fd["gross_profit"] / fd["revenue"]
                    if "004009999" in items and items["004009999"] != 0 and fd.get("ebit") is None:
                        fd["ebit"] = float(items["004009999"])
                    if "004013999" in items and items["004013999"] != 0 and fd.get("net_income") is None:
                        fd["net_income"] = float(items["004013999"])
                    if "004017003" in items and items["004017003"] > 0 and fd.get("eps") is None:
                        fd["eps"] = float(items["004017003"])
                    elif "004017004" in items and items["004017004"] > 0 and fd.get("eps") is None:
                        fd["eps"] = float(items["004017004"])
                    if fd.get("revenue") and fd.get("net_income") and fd["revenue"] > 0 and fd.get("net_margin") is None:
                        fd["net_margin"] = fd["net_income"] / fd["revenue"]
        except: pass

        # ── 资产负债表 ──
        try:
            df_bs = ak.stock_financial_us_report_em(stock=sym, symbol='资产负债表', indicator='年报')
            if df_bs is not None and len(df_bs) > 0:
                latest = df_bs[df_bs['REPORT']==df_bs['REPORT'].max()]
                if len(latest) > 0:
                    items = dict(zip(latest['STD_ITEM_CODE'], latest['AMOUNT']))
                    if "004005999" in items and items["004005999"] > 0 and fd.get("total_assets") is None:
                        fd["total_assets"] = float(items["004005999"])
                    if "004011999" in items and items["004011999"] > 0:
                        tl = float(items["004011999"])
                        if fd.get("total_liabilities") is None: fd["total_liabilities"] = tl
                        if fd.get("total_assets") and fd["total_assets"] > 0 and fd.get("debt_ratio") is None:
                            fd["debt_ratio"] = tl / fd["total_assets"]
                    if "004017999" in items and items["004017999"] > 0 and fd.get("equity") is None:
                        fd["equity"] = float(items["004017999"])
                    if "004001001" in items and items["004001001"] > 0 and fd.get("cash_short_term") is None:
                        fd["cash_short_term"] = float(items["004001001"])
                    if "004001004" in items and items["004001004"] > 0 and fd.get("receivables") is None:
                        fd["receivables"] = float(items["004001004"])
                    if "004001008" in items and items["004001008"] > 0 and fd.get("inventory") is None:
                        fd["inventory"] = float(items["004001008"])
        except: pass

        # ── 现金流量表 ──
        try:
            df_cf = ak.stock_financial_us_report_em(stock=sym, symbol='现金流量表', indicator='年报')
            if df_cf is not None and len(df_cf) > 0:
                latest = df_cf[df_cf['REPORT']==df_cf['REPORT'].max()]
                if len(latest) > 0:
                    items = dict(zip(latest['STD_ITEM_CODE'], latest['AMOUNT']))
                    if "003999" in items and items["003999"] != 0 and fd.get("operating_cf") is None:
                        fd["operating_cf"] = float(items["003999"])
                    if "005002" in items and items["005002"] != 0:
                        capex = abs(float(items["005002"]))
                        if fd.get("capex") is None: fd["capex"] = capex
                        if fd.get("operating_cf") and fd.get("free_cf") is None:
                            fd["free_cf"] = fd["operating_cf"] - capex
        except: pass
        
        # ── 衍生计算：PE/PS/PB/市值 ──
        price = fd.get("price")
        eps = fd.get("eps")
        rev = fd.get("revenue")
        ni = fd.get("net_income")
        bv = fd.get("equity")  # book value for PB
        if price and price > 0:
            # PE = 股价 / EPS
            if eps and eps > 0 and fd.get("pe") is None:
                fd["pe"] = round(price / eps, 2)
            # 市值 ≈ 股价 × (净利润/EPS)  [share count = net_income / EPS... actually no]
            # 更简单：如果已有market_cap就用，没有就从pe估算
            # 但最简单的做法是直接用 EPS 算 PE
            if ni and ni > 0 and eps and eps > 0:
                shares = ni / eps
                mc = price * shares
                if fd.get("market_cap") is None:
                    fd["market_cap"] = mc
                # PS = 市值 / 营收(TTM)
                if rev and rev > 0 and fd.get("ps") is None:
                    fd["ps"] = round(mc / rev, 2)
                # PB = 市值 / 净资产
                if bv and bv > 0 and fd.get("pb") is None:
                    fd["pb"] = round(mc / bv, 2)
        # 有股价但没有PE/PB时，如果已有equity可以直接算PB
        if price and price > 0 and bv and bv > 0 and fd.get("pb") is None and fd.get("market_cap"):
            fd["pb"] = round(fd["market_cap"] / bv, 2)
    except Exception as e:
        print(f"[akshare US fallback] {sym}: {e}")

def fetch_live(symbol):
    raw_sym = symbol.strip()
    stock_code = _normalize_symbol(raw_sym)  # 如 usMSFT / sh600519 / sz000001 / hk00700
    sym = stock_code[2:].upper()  # 纯代码部分用于显示
    prefix = stock_code[:2]       # us/sh/sz/hk
    fd = {}
    multi = {}

    # v8.4+: A股 → 使用 a_share_adapter + Tencent API fallback
    if prefix in ("sh", "sz", "bj"):
        try:
            from a_share_adapter import get_ashare_quote, parse_ashare_financials
            code = sym
            q = get_ashare_quote(stock_code)
            if q:
                fd["_name"] = q.get("name", sym)
                fd["price"] = q.get("price")
                fd["pe"] = q.get("pe") or q.get("pe_dynamic")
                fd["market_cap"] = q.get("market_cap")
                fd["pb"] = q.get("pb")
                fd["_cn_sector"] = fd.get("_cn_sector") or "A股"
            
            # v8.7: Tencent API 补充PE/PB/市值（EastMoney挂掉时的备胎）
            if not fd.get("pe") or not fd.get("pb") or not fd.get("market_cap"):
                try:
                    import requests
                    tencent_url = f"https://qt.gtimg.cn/q={stock_code}"
                    tr = requests.get(tencent_url, timeout=5)
                    tr.encoding = 'gbk'
                    body = tr.text.strip()
                    if '=' in body and body.count('~') > 30:
                        parts = body.split('~')
                        # PE-TTM通常在位置53(0-indexed)
                        if not fd.get("pe") and len(parts) > 54:
                            pe_v = _fmt_num(parts[53])
                            if pe_v and 0 < pe_v < 10000:
                                fd["pe"] = pe_v
                        # 市值在位置45-46
                        if not fd.get("market_cap") and len(parts) > 46:
                            mcap_v = _fmt_num(parts[45])
                            if mcap_v and mcap_v > 0:
                                fd["market_cap"] = mcap_v * 1e8  # Tencent返回亿
                        # PB在位置?
                        if not fd.get("pb") and len(parts) > 51:
                            # Tencent PB字段不稳定，用price/(equity/shares)估算
                            pass
                except Exception as te:
                    print(f"[tencent_fallback] {symbol}: {te}")
            
            # 财报
            parse_ashare_financials(fd, code)
            
            # v8.7: A股的报告期转换(YYYYMMDD → FY25Q1格式)
            rd = fd.get("report_date", "")
            if rd and len(rd) >= 8:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(rd[:8], "%Y%m%d")
                    q = (dt.month - 1) // 3 + 1
                    fd["_report_period"] = f"FY{dt.year%100}Q{q}"
                    fd["_report_date"] = rd[:10]
                except:
                    fd["_report_period"] = rd[:8]
            
            # v8.7+: 补充衍生指标（A股数据丰富度提升至美股水平）
            # EPS = price / pe (pe是TTM PE，所以eps也是TTM)
            if fd.get("eps") is None and fd.get("price") and fd.get("pe") and fd["pe"] > 0:
                fd["eps"] = round(fd["price"] / fd["pe"], 4)
            # 毛利率
            if fd.get("gross_margin") is None and fd.get("gross_profit") and fd.get("revenue") and fd["revenue"] > 0:
                fd["gross_margin"] = round(fd["gross_profit"] / fd["revenue"], 4)
            if fd.get("gross_margin") is None and fd.get("revenue") and fd.get("total_cost") and fd["revenue"] > 0:
                est_gp = fd["revenue"] - fd["total_cost"]
                if est_gp > 0:
                    fd["gross_margin"] = round(est_gp / fd["revenue"], 4)
            # 净利率
            if fd.get("net_margin") is None and fd.get("net_income") and fd.get("revenue") and fd["revenue"] > 0:
                fd["net_margin"] = round(fd["net_income"] / fd["revenue"], 4)
            # 营业利润率
            if fd.get("operating_margin") is None and fd.get("operating_income") and fd.get("revenue") and fd["revenue"] > 0:
                fd["operating_margin"] = round(fd["operating_income"] / fd["revenue"], 4)
            # ROE
            if fd.get("roe") is None and fd.get("net_income") and fd.get("equity") and fd["equity"] > 0:
                fd["roe"] = round(fd["net_income"] * 4 / fd["equity"], 4)
            # ROIC
            if fd.get("roic") is None and fd.get("net_income") and fd.get("equity") and fd.get("total_liabilities"):
                tc = fd["equity"] + fd["total_liabilities"]
                if tc > 0:
                    fd["roic"] = round(fd["net_income"] * 4 / tc, 4)
            # ROA = net_income / total_assets
            if fd.get("roa") is None and fd.get("net_income") and fd.get("total_assets") and fd["total_assets"] > 0:
                fd["roa"] = round(fd["net_income"] * 4 / fd["total_assets"], 4)
            # FCF Yield
            if fd.get("fcf_yield") is None and fd.get("free_cashflow") and fd.get("market_cap") and fd["market_cap"] > 0:
                fcf_annual = fd["free_cashflow"] * 4 if fd["free_cashflow"] < fd["market_cap"] * 0.5 else fd["free_cashflow"]
                fd["fcf_yield"] = round(fcf_annual / fd["market_cap"], 4)
            # FCF Margin
            if fd.get("fcf_margin") is None and fd.get("free_cashflow") and fd.get("revenue") and fd["revenue"] > 0:
                fd["fcf_margin"] = round(fd["free_cashflow"] * 4 / fd["revenue"], 4)
            # EV/EBITDA = (市值 + 总负债 - 现金) / EBITDA
            if fd.get("ev_ebitda") is None and fd.get("market_cap") and fd["market_cap"] > 0:
                eb = fd.get("ebitda") or fd.get("operating_income")
                if eb and eb > 0:
                    tl = fd.get("total_liabilities") or 0
                    cs = fd.get("cash_and_equivalents") or fd.get("cash_short_term") or 0
                    mc = fd["market_cap"]
                    ev = mc + tl - cs
                    fd["ev_ebitda"] = round(ev / (eb * 4), 2)
            # 负债率（如果EastMoney没返回，手动计算）
            if fd.get("debt_ratio") is None and fd.get("total_liabilities") and fd.get("total_assets") and fd["total_assets"] > 0:
                fd["debt_ratio"] = round(fd["total_liabilities"] / fd["total_assets"], 4)
            # PS = 市值 / 年化营收
            if fd.get("ps") is None and fd.get("market_cap") and fd.get("revenue") and fd["revenue"] > 0 and fd["market_cap"] > 0:
                fd["ps"] = round(fd["market_cap"] / (fd["revenue"] * 4), 2)
            # PEG = PE / 营收增速
            if fd.get("peg") is None and fd.get("pe") and fd.get("pe") > 0:
                eg_rate = fd.get("revenue_growth") or fd.get("profit_growth")
                if eg_rate and eg_rate > 0:
                    fd["peg"] = round(fd["pe"] / (eg_rate * 100), 2)
            if fd.get("pb") is None and fd.get("price") and fd.get("book_value_per_share") and fd["book_value_per_share"] > 0:
                fd["pb"] = round(fd["price"] / fd["book_value_per_share"], 2)
            if fd.get("pb") is None and fd.get("market_cap") and fd.get("equity") and fd["equity"] > 0:
                fd["pb"] = round(fd["market_cap"] / fd["equity"], 2)
            
            # 继续到衍生计算步骤（不能return，否则会跳过百分位/稳定性/周期/风险评估）
        except Exception as e:
            print(f"[fetch_ashare] {symbol}: {e}")
            # fallback到akshare旧逻辑
            pass

    # ----- 1. 行情 -----

    # ----- 1. 行情(美股: 腾讯API实时数据，westock+yfinance补充) -----
    if prefix == "us":
        try:
            from us_stock_adapter import get_us_quote
            usq = get_us_quote(sym)
            if usq:
                fd["_name"] = usq.get("_name") or usq.get("name_en", sym)
                fd["price"] = usq.get("price") or fd.get("price")
                fd["pe"] = usq.get("pe") or fd.get("pe")
                fd["market_cap"] = usq.get("market_cap") or fd.get("market_cap")
                if usq.get("high_52w"): fd["high_52w"] = usq["high_52w"]
                if usq.get("low_52w"): fd["low_52w"] = usq["low_52w"]
                if usq.get("change_pct"): fd["change_pct"] = usq["change_pct"]
                if usq.get("currency"): fd["currency"] = usq["currency"]
                if usq.get("sector"): fd["sector"] = usq["sector"]
                if usq.get("beta"): fd["beta"] = usq["beta"]
        except Exception as e:
            print(f"[us_adapter] {symbol}: {e}")
    
    # ====== v9.0+: SEC EDGAR 作为美股主要财务数据源（优先级最高）======
    # SEC EDGAR 是财报最权威原始源，完全免费、无Key、限10次/秒
    # 放在westock之前，确保SEC数据优先；末尾的SEC覆盖层作为最终确认
    if prefix == "us":
        try:
            from sec_edgar_adapter import parse_sec_financials
            fd = parse_sec_financials(fd, sym)
            rd = fd.get("report_date", "")
            if rd and len(rd) >= 10:
                from datetime import datetime
                try:
                    from us_stock_adapter import calc_us_fiscal_period
                    fp = calc_us_fiscal_period(rd, sym)
                    if fp:
                        fd["_report_period"] = fp
                    else:
                        dt = datetime.strptime(rd[:10], "%Y-%m-%d")
                        q = (dt.month - 1) // 3 + 1
                        fd["_report_period"] = f"FY{dt.year%100}Q{q}"
                except:
                    pass
                fd["_report_date"] = rd[:10]
                fd["_data_source"] = "sec_edgar_v9.0_primary"
        except Exception as e:
            print(f"[sec_edgar_primary] {symbol}: {e}")
    
    # westock profile (补充名称+行业映射)
    md = westock(["profile", stock_code], 10)
    rows = parse_md_table(md)
    if rows:
        r = rows[0]
        fd["_name"] = r.get("name", sym)
        fd["_cn_sector"] = (r.get("sector") or "")  # 留中文行业名用于映射
    # K线获取52周高低（用于波动率计算）
    md_k = westock(["kline", stock_code, "--period", "day", "--limit", "365"], 15)
    rows_k = parse_md_table(md_k)
    if rows_k:
        prices = []
        for rk in rows_k:
            try:
                last_p = float(rk.get("last", 0))
                if last_p > 0:
                    prices.append(last_p)
            except: pass
        if prices:
            fd["high_52w"] = max(prices)
            fd["low_52w"] = min(prices)
            fd["price"] = prices[0]  # kline返回[新→旧]，第1个是最新

    # ----- 2. 利润表(8季度) -----
    md2 = westock(["finance", stock_code, "--type", "income", "--num", "8"], 20)
    rows2 = parse_md_table(md2)
    # ⚠️ 警告: westock多期数据排列为[旧→新], rows[-1]=最新, rows[-5]=4季前
    if rows2:
        r = rows2[-1]  # 最新一期（rows[-1] = latest）
        # 提取报告期
        end_date = r.get("EndDate") or r.get("_date") or ""
        if end_date:
            fd["_report_date"] = str(end_date)[:10]
            # v8.9: 美股使用财年计算，A股/港股使用自然季度
            if prefix == "us":
                try:
                    from us_stock_adapter import calc_us_fiscal_period
                    fp = calc_us_fiscal_period(fd["_report_date"], sym)
                    if fp:
                        fd["_report_period"] = fp
                    else:
                        # 回退到自然季度
                        from datetime import datetime
                        dt = datetime.strptime(fd["_report_date"] if len(fd["_report_date"])>=10 else "2000-01-01", "%Y-%m-%d")
                        q = (dt.month - 1) // 3 + 1
                        fd["_report_period"] = f"FY{dt.year%100}Q{q}"
                except:
                    fd["_report_period"] = fd["_report_date"]
            else:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(fd["_report_date"] if len(fd["_report_date"])>=10 else "2000-01-01", "%Y-%m-%d")
                    q = (dt.month - 1) // 3 + 1
                    fy = dt.year
                    fd["_report_period"] = f"FY{fy%100}Q{q}"
                except:
                    fd["_report_period"] = fd["_report_date"]
        rev = _fmt_num(r.get("Sales_Q"))
        if rev: fd["revenue"] = rev * 1e6
        ni = _fmt_num(r.get("NetIncome_Q"))
        if ni: fd["net_income"] = ni * 1e6
        gp = _fmt_num(r.get("GrossIncome_Q"))
        if gp: fd["gross_profit"] = gp * 1e6
        gm_v = _fmt_num(r.get("GrossMargin_Q"))
        if gm_v is not None: fd["gross_margin"] = gm_v / 100
        nm_v = _fmt_num(r.get("NetMargin_Q"))
        if nm_v is not None: fd["net_margin"] = nm_v / 100
        om_v = _fmt_num(r.get("OperatingMargin_Q"))
        if om_v is not None: fd["operating_margin"] = om_v / 100
        eps_v = _fmt_num(r.get("DilutedEPS_Q"))
        if not eps_v:
            eps_v = _fmt_num(r.get("BasicEPS_Q"))  # 备用字段
        if eps_v: fd["eps"] = eps_v
        ebit_v = _fmt_num(r.get("EBIT_Q"))
        if ebit_v: fd["ebit"] = ebit_v * 1e6
        ebitda_v = _fmt_num(r.get("EBITDA_Q"))
        if ebitda_v: fd["ebitda"] = ebitda_v * 1e6
        cogs_v = _fmt_num(r.get("Cogs_Q"))
        if cogs_v: fd["cogs"] = cogs_v * 1e6
        tax = _fmt_num(r.get("IncomeTax_Q"))
        pretax = _fmt_num(r.get("PretaxIncome_Q"))
        if tax and pretax and pretax > 0: fd["_tax_rate"] = tax / pretax

        # YoY增长率 (最新vs4季前: rows[-1] vs rows[-5])
        if len(rows2) >= 5:
            r_yoy = rows2[-5]  # 4 quarters before the latest
            rev_yoy = _fmt_num(r_yoy.get("Sales_Q"))
            if rev and rev_yoy and rev_yoy > 0: fd["revenue_growth"] = (rev - rev_yoy) / rev_yoy
            ni_yoy = _fmt_num(r_yoy.get("NetIncome_Q"))
            if ni and ni_yoy and ni_yoy > 0: fd["profit_growth"] = (ni - ni_yoy) / ni_yoy
        # v7.5: 如果不足5季度但至少2期，用首尾差值估算增长率
        elif len(rows2) >= 2 and fd.get("revenue_growth") is None:
            r_first = _fmt_num(rows2[0].get("Sales_Q"))
            if rev and r_first and r_first > 0:
                # rows2[0]是最旧的一期，rows2[-1]是最新一期
                n_periods = len(rows2) - 1
                ratio = rev / r_first
                if ratio > 0:
                    fd["revenue_growth"] = round((ratio ** (1.0 / n_periods) - 1) * 4, 4)  # 年化

        # 多期数据收集（保持旧→新顺序，稳定性函数只关心分布）
        for row in rows2[:]:
            rv = _fmt_num(row.get("Sales_Q"))
            if rv: multi.setdefault("revenue", []).append(rv * 1e6)
            gv = _fmt_num(row.get("GrossMargin_Q"))
            if gv is not None: multi.setdefault("gross_margin", []).append(gv / 100)
            nv = _fmt_num(row.get("NetMargin_Q"))
            if nv is not None: multi.setdefault("net_margin", []).append(nv / 100)
            ev = _fmt_num(row.get("EBITDA_Q"))
            if ev: multi.setdefault("ebitda", []).append(ev * 1e6)

        # v8.7: TTM EPS - 优先用westock提供的TTM字段(DilutedEPS不带_Q)，备胎求和4季度
        r_latest = rows2[-1] if rows2 else None
        if r_latest:
            # 优先: TTM字段（不带_Q后缀）
            ttm_eps = _fmt_num(r_latest.get("DilutedEPS"))
            if ttm_eps and ttm_eps > 0:
                fd["eps_ttm"] = round(ttm_eps, 4)
            else:
                # 备胎: 求和最近4期DilutedEPS_Q
                ttm_eps = 0
                ttm_count = 0
                for row in rows2[-4:]:
                    eps_q = _fmt_num(row.get("DilutedEPS_Q"))
                    if not eps_q:
                        eps_q = _fmt_num(row.get("BasicEPS_Q"))
                    if eps_q and eps_q > 0:
                        ttm_eps += eps_q
                        ttm_count += 1
                if ttm_count >= 4 and ttm_eps > 0:
                    fd["eps_ttm"] = round(ttm_eps, 4)
                elif ttm_count > 0:
                    fd["eps_ttm"] = round(ttm_eps / ttm_count * 4, 4)
            # 用TTM重算PE（覆盖westock单季PE）
            if fd.get("eps_ttm") and fd.get("price"):
                fd["pe"] = round(fd["price"] / fd["eps_ttm"], 2)

    # ----- 3. 资产负债表(8季度) -----
    md3 = westock(["finance", stock_code, "--type", "balance", "--num", "8"], 20)
    rows3 = parse_md_table(md3)
    if rows3:
        r = rows3[-1]  # 最新一期
        ta = _fmt_num(r.get("TotalAssets"))
        if ta: fd["total_assets"] = ta * 1e6
        tl = _fmt_num(r.get("TotalLiabilities"))
        if tl: fd["total_liabilities"] = tl * 1e6
        eq = _fmt_num(r.get("CommonStockEquity"))
        if eq: fd["equity"] = eq * 1e6
        if fd.get("total_liabilities") and fd.get("total_assets") and fd["total_assets"] > 0:
            fd["debt_ratio"] = fd["total_liabilities"] / fd["total_assets"]
        roe_v = _fmt_num(r.get("ROE"))
        if roe_v is not None: fd["roe"] = roe_v / 100
        roa_v = _fmt_num(r.get("ROA"))
        if roa_v is not None: fd["roa"] = roa_v / 100
        cr_v = _fmt_num(r.get("CurrentRatio"))
        if cr_v: fd["current_ratio"] = cr_v
        qr_v = _fmt_num(r.get("QuickRatio"))
        if qr_v: fd["quick_ratio"] = qr_v
        cash = _fmt_num(r.get("CashShortTermInvestment"))
        if cash: fd["cash_short_term"] = cash * 1e6
        inv_v = _fmt_num(r.get("Inventory"))
        if inv_v: fd["inventory"] = inv_v * 1e6
        recv_v = _fmt_num(r.get("ShortTermReceivable"))
        if recv_v: fd["receivables"] = recv_v * 1e6
        bps_v = _fmt_num(r.get("BPS"))
        if bps_v: fd["book_value_per_share"] = bps_v
        std_v = _fmt_num(r.get("ShortTermDebt"))
        if std_v: fd["_short_term_debt"] = std_v * 1e6
        ltd_v = _fmt_num(r.get("LongTermDebt"))
        if ltd_v: fd["_long_term_debt"] = ltd_v * 1e6
        cl_v = _fmt_num(r.get("CurrentLiabilities"))
        if cl_v: fd["_current_liabilities"] = cl_v * 1e6

        # 多期ROE和应收
        for row in rows3[:]:
            rv = _fmt_num(row.get("ROE"))
            if rv is not None: multi.setdefault("roe", []).append(rv / 100)
            iv = _fmt_num(row.get("Inventory"))
            if iv: multi.setdefault("inventory", []).append(iv * 1e6)
            rcv = _fmt_num(row.get("ShortTermReceivable"))
            if rcv: multi.setdefault("receivables", []).append(rcv * 1e6)

        # YoY应收/存货 (最新vs4季前)
        if len(rows3) >= 5:
            r_yoy3 = rows3[-5]
            r3_recv = _fmt_num(r_yoy3.get("ShortTermReceivable"))
            if recv_v and r3_recv and r3_recv > 0: fd["receivables_growth"] = (recv_v - r3_recv) / r3_recv
            r3_inv = _fmt_num(r_yoy3.get("Inventory"))
            if inv_v and r3_inv and r3_inv > 0: fd["inventory_growth"] = (inv_v - r3_inv) / r3_inv

    # ----- 4. 现金流量表 -----
    md4 = westock(["finance", stock_code, "--type", "cashflow", "--num", "1"], 10)
    rows4 = parse_md_table(md4)
    if rows4:
        r = rows4[0]
        cfo = _fmt_num(r.get("CFO_Q"))
        capex = _fmt_num(r.get("Capex_Q"))
        if cfo is not None: fd["operating_cf"] = cfo * 1e6
        if capex is not None:
            fd["capex"] = capex * 1e6
            # Capex_Q在westock中已为正数(现金流出)，直接减去
            if cfo is not None:
                fd["free_cf"] = (cfo - capex) * 1e6
            elif cfo is not None:
                fd["free_cf"] = cfo * 1e6
        elif cfo is not None:
            fd["free_cf"] = cfo * 1e6
        fcf_q = _fmt_num(r.get("FreeCF_Q"))
        if fcf_q is not None and "free_cf" not in fd:
            fd["free_cf"] = fcf_q * 1e6

    # ====== 4.5 备用数据源: westock缺失时自动补数 ======
    _fetch_fallback(fd, stock_code, prefix, sym)

    # ====== 4.6 v9.0: SEC EDGAR XBRL 实时财报覆盖（美股）======
    # SEC EDGAR 是财报最权威原始源，完全免费、无Key、限10次/秒
    if prefix == "us":
        try:
            from sec_edgar_adapter import parse_sec_financials
            fd = parse_sec_financials(fd, sym)
            
            # 如果SEC有数据，报告日期已更新
            rd = fd.get("report_date", "")
            if rd and len(rd) >= 10:
                from datetime import datetime
                # 使用美股财年计算
                try:
                    from us_stock_adapter import calc_us_fiscal_period
                    fp = calc_us_fiscal_period(rd, sym)
                    if fp:
                        fd["_report_period"] = fp
                    else:
                        dt = datetime.strptime(rd[:10], "%Y-%m-%d")
                        q = (dt.month - 1) // 3 + 1
                        fd["_report_period"] = f"FY{dt.year%100}Q{q}"
                except:
                    pass
                fd["_report_date"] = rd[:10]
                fd["_data_source"] = "sec_edgar_v9.0"
            
            # 用SEC的PE重算（如果腾讯API没拿到的话）
            if not fd.get("pe") and fd.get("eps_ttm") and fd.get("price"):
                fd["pe"] = round(fd["price"] / fd["eps_ttm"], 2)
        except Exception as e:
            print(f"[sec_edgar_overlay] {symbol}: {e}")

    # ====== 4.7 v8.9: 美股yfinance覆盖（前瞻性代码）======
    # 部署到其他服务器(如Render.com)后，若IP未被封禁则自动补新数据
    if prefix == "us":
        try:
            from us_stock_adapter import parse_us_financials_timeout, calc_us_fiscal_period
            parse_us_financials_timeout(fd, sym, timeout=25)
            # 用yfinance的最新报告日期重算_report_period
            rd = fd.get("report_date", "")
            if rd and len(rd) >= 10:
                from datetime import datetime
                fp = calc_us_fiscal_period(rd, sym)
                if fp:
                    fd["_report_period"] = fp
                    fd["_report_date"] = rd[:10]
                    fd["_yf_overlay"] = True
                else:
                    try:
                        dt = datetime.strptime(rd[:10], "%Y-%m-%d")
                        q = (dt.month - 1) // 3 + 1
                        fd["_report_period"] = f"FY{dt.year%100}Q{q}"
                        fd["_yf_overlay"] = True
                    except:
                        pass
        except Exception as e:
            print(f"[yf_overlay_us] {symbol}: {e}")

    # ====== v9.0+: SEC EDGAR 最终覆盖（仅补充缺失数据，不覆盖已有数据）======
    # 注意: westock/yfinance如果有更新的数据，保留它们的
    if prefix == "us" and _SEC_AVAILABLE:
        try:
            # 保存当前数据快照
            _existing = {k: v for k, v in fd.items() if k not in ("_report_period", "_report_date", "_data_source")}
            _pre_rd = fd.get("report_date", "")
            
            fd = parse_sec_financials(fd, sym)
            rd = fd.get("report_date", "")
            
            # 如果SEC数据比已有的旧，恢复westock的数据
            if rd and _pre_rd and rd < _pre_rd:
                # SEC数据更旧，回滚金融字段到westock版本
                for k, v in _existing.items():
                    fd[k] = v
                fd["report_date"] = _pre_rd
                fd["_data_source"] = f"westock_{_pre_rd[:10]}"
            elif rd and len(rd) >= 10:
                from datetime import datetime
                fp = calc_us_fiscal_period(rd, sym)
                if fp:
                    fd["_report_period"] = fp
                else:
                    dt = datetime.strptime(rd[:10], "%Y-%m-%d")
                    q = (dt.month - 1) // 3 + 1
                    fd["_report_period"] = f"FY{dt.year%100}Q{q}"
                fd["_report_date"] = rd[:10]
                fd["_data_source"] = f"sec_edgar_{rd[:10]}"
        except Exception as e:
            print(f"[sec_final] {symbol}: {e}")

    # ----- 5. 衍生计算 -----
    # PS (市销率) - 如果westock没返回，用手上的市值/营收算
    if not fd.get("ps") or fd.get("ps") == 0:
        if fd.get("market_cap") and fd.get("revenue") and fd["revenue"] > 0:
            # 营收是季度数据，用TTM估算（季度×4）
            fd["ps"] = fd["market_cap"] / (fd["revenue"] * 4)
    
    if fd.get("free_cf") and fd.get("market_cap") and fd["market_cap"] > 0:
        fd["fcf_yield"] = fd["free_cf"] / fd["market_cap"]
    elif fd.get("net_income") and fd.get("market_cap") and fd["market_cap"] > 0:
        fd["fcf_yield"] = fd["net_income"] / fd["market_cap"]
    if fd.get("free_cf") and fd.get("revenue") and fd["revenue"] > 0:
        fd["fcf_margin"] = fd["free_cf"] / fd["revenue"]

    # v7.5: ROE = net_income / equity (westock有时不返回ROE)
    if fd.get("roe") is None and fd.get("net_income") and fd.get("equity") and fd["equity"] > 0:
        fd["roe"] = fd["net_income"] / fd["equity"]

    # v7.5: 从已有数据估算市场数据（PE/PB/市值/PS），用于替代被限流的yfinance
    price = fd.get("price", 0)
    # PB = price / BPS
    if fd.get("pb") is None and price > 0 and fd.get("book_value_per_share") and fd["book_value_per_share"] > 0:
        fd["pb"] = round(price / fd["book_value_per_share"], 1)
    # PE = price / eps (quarterly EPS)
    if fd.get("pe") is None and price > 0 and fd.get("eps"):
        fd["pe"] = round(price / fd["eps"], 1)
    # 如果eps缺失但price和net_income都有,用equity/BPS反推股数再算eps
    if fd.get("eps") is None and price > 0 and fd.get("net_income") and fd.get("equity") and fd.get("book_value_per_share"):
        est_shares = fd["equity"] / fd["book_value_per_share"]
        if est_shares > 0:
            fd["eps"] = fd["net_income"] / est_shares
            if fd.get("pe") is None:
                fd["pe"] = round(price / fd["eps"], 1)
    # 市值 = price * (equity / BPS)
    if fd.get("market_cap") is None and price > 0 and fd.get("book_value_per_share") and fd["book_value_per_share"] > 0 and fd.get("equity") and fd["equity"] > 0:
        est_shares = fd["equity"] / fd["book_value_per_share"]
        if est_shares > 0:
            fd["market_cap"] = round(price * est_shares, 1)
    # PS = market_cap / (revenue * 4)
    if fd.get("ps") is None and fd.get("market_cap") and fd.get("revenue") and fd["revenue"] > 0:
        fd["ps"] = round(fd["market_cap"] / (fd["revenue"] * 4), 1)
    # FCF Yield用新market_cap重算
    if fd.get("free_cf") and fd.get("market_cap") and fd["market_cap"] > 0:
        fd["fcf_yield"] = fd["free_cf"] / fd["market_cap"]
    elif fd.get("net_income") and fd.get("market_cap") and fd["market_cap"] > 0:
        fd["fcf_yield"] = fd["net_income"] / fd["market_cap"]
    
    # v7.5: EV/EBITDA = (市值 + 总负债 - 现金) / EBITDA
    if fd.get("ev_ebitda") is None:
        mc = fd.get("market_cap", 0)
        tl = fd.get("total_liabilities", 0)
        cs = fd.get("cash_short_term", 0)
        eb = fd.get("ebitda", 0)
        if mc > 0 and tl > 0 and eb > 0:
            ev = mc + tl - cs
            if ev > 0:
                fd["ev_ebitda"] = round(ev / eb, 1)

    # ROIC
    if fd.get("ebit"):
        tr = fd.get("_tax_rate", 0.21)
        nopat = fd["ebit"] * (1 - tr)
        eq = fd.get("equity", 0)
        sd = fd.get("_short_term_debt", 0)
        ld = fd.get("_long_term_debt", 0)
        cs = fd.get("cash_short_term", 0)
        id = sd + ld
        if id > 0:
            ic = eq + id - cs
        else:
            cl = fd.get("_current_liabilities", 0)
            ta = fd.get("total_assets", 0)
            if ta > 0:
                ic = ta - cs - (cl * 0.5 if cl > 0 else 0)
            elif eq > 0:
                ic = eq
            else:
                ic = 0
        if ic > 0:
            fd["roic"] = nopat / ic

    # Forward PE
    if not fd.get("pe_forward") and fd.get("pe"):
        if fd.get("revenue_growth"):
            eg = min(max(abs(fd["revenue_growth"]), 0.05), 0.50)
            fd["pe_forward"] = round(fd["pe"] / (1 + eg), 1)

    # PEG = PE / 预期净利润增长率（用营收增速proxy）
    # PEG≈1 → 估值与增长匹配, <1→相对偏便宜, >1.5→偏贵
    if fd.get("peg") is None and fd.get("pe") and fd.get("pe") > 0:
        eg_rate = fd.get("revenue_growth") or fd.get("profit_growth")
        if eg_rate and eg_rate > 0:
            fd["peg"] = round(fd["pe"] / (eg_rate * 100), 2)

    if fd.get("price") and fd.get("market_cap") and fd["price"] > 1:
        if fd["market_cap"] / fd["price"] < 50000000:
            fd["market_cap"] *= 1000

    # ====== 6. 行业识别(v7.5: 用westock profile中文名映射, 替代硬编码SECTOR_MAP) ======
    cn_sector = (fd.get("_cn_sector") or "").lower()
    sector = "general"
    for cn_key, en_key in SECTOR_CN_MAP.items():
        if cn_key in cn_sector:
            sector = en_key
            break
    # 少数股票手动覆盖（如苹果虽电子技术但实为消费硬件）
    sector = SECTOR_OVERRIDE.get(sym, sector)
    fd["sector"] = sector

    # ====== 7. 行业分位数 ======
    percentiles = {}
    # 行业基准的key使用短名: rg/gm/nm/roe/roic/pe/ps
    bm_metric_map = {"revenue_growth":"rg","gross_margin":"gm","net_margin":"nm",
                     "roe":"roe","roic":"roic","pe":"pe","ps":"ps"}
    for api_key, bm_key in bm_metric_map.items():
        val = fd.get(api_key)  # fd中存储的key就是全名
        if val is not None:
            p = calc_percentile(val, sector, bm_key)
            if p: percentiles[api_key] = p
    if percentiles:
        fd["percentiles"] = percentiles

    # ====== 8. 稳定性评分 ======
    stability = {}
    for metric in ["gross_margin", "net_margin", "roe"]:
        series = multi.get(metric, [])
        if len(series) >= 4:
            s = calc_stability(series, metric)
            if s: stability[metric] = s
    if stability:
        fd["stability"] = stability

    # ====== 9. 周期位置 ======
    cyclic_sectors = {"semicon", "energy", "auto", "retail"}
    if sector in cyclic_sectors:
        cp = calc_cycle_position(fd, multi)
        if cp: fd["cycle"] = cp

    # ====== 10. 风险评分(基于52周高低) ======
    if fd.get("high_52w") and fd.get("low_52w") and fd.get("price") and fd["price"] > 0:
        price_range = fd["high_52w"] - fd["low_52w"]
        vol_est = price_range / fd["price"]
        if vol_est < 0.30: rr, rs = "低", 85
        elif vol_est < 0.50: rr, rs = "中", 65
        elif vol_est < 0.75: rr, rs = "高", 40
        else: rr, rs = "极高", 15
        fd["risk"] = {"vol": round(vol_est, 3), "rating": rr, "score": rs}

    # v7.5: Beta估算（从52周波动率映射，必须在risk字段生成之后）
    if fd.get("beta") is None and fd.get("risk"):
        vol = fd["risk"].get("vol", 0)
        if vol > 0:
            if vol < 0.30: fd["beta"] = round(0.3 + vol * 0.8, 2)
            elif vol < 0.50: fd["beta"] = round(0.5 + vol * 0.6, 2)
            elif vol < 0.75: fd["beta"] = round(0.8 + vol * 0.8, 2)
            else: fd["beta"] = round(min(1.2 + (vol - 0.75) * 1.5, 2.5), 2)

    # ====== 11. 稳定性和风险调整系数(乘法因子) ======
    adj = {"industry": 1.0, "stability": 1.0, "risk": 1.0, "cycle": 1.0}

    # 行业调整: 高于行业中位数加分，低于扣分
    if percentiles:
        # 各指标分位数平均值
        pct_vals = [p["pct"] for p in percentiles.values()]
        avg_pct = sum(pct_vals) / len(pct_vals)
        adj["industry"] = round(1.0 + (avg_pct - 50) * 0.002, 4)  # ±10%

    # 稳定性调整: 高稳定性加分
    if stability:
        scores = [s["score"] for s in stability.values()]
        avg_stab = sum(scores) / len(scores)
        adj["stability"] = round(1.0 + (avg_stab - 50) * 0.001, 4)  # ±5%

    # 风险调整: 高风险扣分
    if fd.get("risk"):
        rs = fd["risk"]["score"]
        adj["risk"] = round(1.0 - (100 - rs) * 0.002, 4)  # 最多-17%

    # 周期调整: 周期顶部扣分
    if fd.get("cycle"):
        if fd["cycle"]["position"] in ("周期顶部", "周期下降"):
            adj["cycle"] = 0.92
        elif fd["cycle"]["position"] in ("周期底部",):
            adj["cycle"] = 1.05

    fd["adjustments"] = adj
    
    # ====== 12. 机构评级（双数据源） ======
    try:
        rating_data = None
        # A股 → 新浪财经机构推荐（覆盖全面，无需token）
        if prefix in ("sh", "sz"):
            try:
                import requests as _req
                from io import StringIO as _SIO
                import pandas as _pd
                _url = f"http://stock.finance.sina.com.cn/stock/go.php/vIR_StockSearch/key/{sym}.phtml"
                _r = _req.get(_url, params={"num":"5000","p":"1"}, timeout=10)
                if _r.status_code == 200 and len(_r.text) > 1000:
                    _dfs = _pd.read_html(_SIO(_r.text), header=0)
                    if _dfs:
                        df = _dfs[0].iloc[:, :8]
                        df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
                        if "评级日期↓" in df.columns:
                            df = df.rename(columns={"评级日期↓": "评级日期"})
                        if len(df) > 0:
                            # 不同时间范围的统计
                            today = _pd.Timestamp.now()
                            time_ranges = {"3m": 90, "6m": 180, "12m": 365}
                            df["评级日期"] = _pd.to_datetime(df["评级日期"])
                            rating_data = {"forecastInstitutions": int(df["评级机构"].nunique()),
                                           "orgNames": df["评级机构"].unique().tolist(),
                                           "latestDate": str(df["评级日期"].max()).split()[0]}
                            rating_map = {"买入": "buy", "增持": "inc", "持有": "hold", "减持": "dec", "卖出": "sell"}
                            for suffix, days in time_ranges.items():
                                mask = df["评级日期"] >= today - _pd.Timedelta(days=days)
                                sub = df[mask]
                                pref = "r" + suffix + "_"
                                rating_data[pref + "cnt"] = len(sub)
                                rating_data[pref + "inst"] = int(sub["评级机构"].nunique()) if len(sub) > 0 else 0
                                for cn_label, en_key in rating_map.items():
                                    cnt = int((sub["最新评级"] == cn_label).sum())
                                    if cnt > 0:
                                        rating_data[pref + en_key] = cnt
                                tp_vals = sub["目标价"].dropna()
                                if len(tp_vals) > 0:
                                    rating_data[pref + "tpAvg"] = round(float(tp_vals.median()), 2)
                                    rating_data[pref + "tpMax"] = round(float(tp_vals.max()), 2)
                                    rating_data[pref + "tpMin"] = round(float(tp_vals.min()), 2)
                            # 默认用12月作为显示数据
                            for cn_label, en_key in rating_map.items():
                                v = rating_data.get("r12m_" + en_key, 0)
                                if v: rating_data["rating" + en_key.capitalize() + "Cnt"] = v
                            rating_data["ratingCnt"] = rating_data.get("r12m_cnt", 0)
                            tp = rating_data.get("r12m_tpAvg")
                            if tp: rating_data["targetPriceAvg"] = tp
            except Exception:
                pass
        else:
            # 非A股 → westock（国际投行）
            md5 = westock(["rating", stock_code], 15)
            rows5 = parse_md_table(md5)
            if rows5:
                r5 = rows5[0]
                rating_data = {}
                for k in ("forecastInstitutions","targetPriceAvg","targetPriceMax","targetPriceMin",
                           "ratingBuyCnt","ratingIncCnt","ratingHoldCnt","ratingDecCnt","ratingSellCnt","ratingCnt"):
                    val = _fmt_num(r5.get(k))
                    if val is not None:
                        rating_data[k] = int(val) if k.endswith("Cnt") or k=="forecastInstitutions" else val
                if rating_data.get("ratingCnt",0) > 0 or rating_data.get("forecastInstitutions",0) > 0:
                    md6 = westock(["consensus", stock_code], 15)
                    rows6 = parse_md_table(md6)
                    if rows6:
                        tp = _fmt_num(rows6[0].get("目标价"))
                        if tp is not None:
                            rating_data["targetPriceAvg"] = tp
        if rating_data:
            fd["_rating"] = rating_data
    except Exception:
        pass
    
    # ====== 13. 数据质量验证 ======
    fd["_data_quality"] = validate_data(fd)
    
    # ====== v9.0+: 最终_report_period强制覆盖（防止前面被westock/yfinance覆盖）======
    if prefix == "us":
        for _src_date in [fd.get("report_date"), fd.get("_report_date")]:
            if _src_date and len(str(_src_date)) >= 10:
                try:
                    from datetime import datetime as _dt
                    from us_stock_adapter import calc_us_fiscal_period as _cfp
                    _fp = _cfp(str(_src_date)[:10], sym)
                    if _fp:
                        fd["_report_period"] = _fp
                        break
                except:
                    continue
    
    return fd

# ====== 数据质量验证系统 ======
def validate_data(fd):
    """自动校验每个字段的合理性，返回质量报告"""
    warnings = []
    missing = []
    anomalies = []
    score = 100
    
    # ---- 行情字段校验 ----
    checks = [
        ("price", 0.5, 100000, "股价异常"),
        ("pe", -10000, 1000, "PE异常(>1000或<-10000)"),
        ("pe_forward", -10000, 500, "Forward PE异常"),
        ("pb", 0, 100, "PB异常"),
        ("ps", 0, 200, "PS异常"),
        ("market_cap", 1e6, 1e14, "市值异常(单位可能有误)"),
        ("dividend_yield", -0.01, 0.15, "股息率异常"),
        ("change_pct", -0.20, 0.20, "涨幅>20%异常"),
        ("high_52w", 0.5, 100000, "52周高异常"),
        ("low_52w", 0.5, 100000, "52周低异常"),
    ]
    
    for field, lo, hi, msg in checks:
        val = fd.get(field)
        if val is None:
            if field in ("price", "pe", "market_cap"):
                missing.append(field)
                score -= 15
            continue
        if val < lo or val > hi:
            anomalies.append({"field": field, "value": val, "range": f"[{lo}, {hi}]", "msg": msg})
            score -= 10
    
    # ---- PE为负的特殊说明（不是数据错误，是公司亏损）----
    for pe_field in ("pe", "pe_forward"):
        val = fd.get(pe_field)
        if val is not None and val < 0:
            # 从anomalies中移除PE为负的条目（用更友好的信息替代）
            anomalies[:] = [a for a in anomalies if a["field"] != pe_field]
            warnings.append({"field": pe_field, "value": val, "msg": f"PE为负(公司亏损)，非数据错误"})
    
    # ---- 财报字段校验 ----
    fin_checks = [
        ("revenue", 1000, 1e14, "营收异常"),
        ("net_income", -1e14, 1e14, "净利润异常"),
        ("gross_margin", -0.5, 1.0, "毛利率异常"),
        ("net_margin", -2.0, 1.0, "净利率异常"),
        ("eps", -10000, 100000, "EPS异常"),
        ("debt_ratio", 0, 5.0, "负债率异常"),
        ("roe", -2.0, 5.0, "ROE异常"),
        ("total_assets", 1e6, 1e16, "总资产异常"),
        ("receivables_growth", -1.0, 20.0, "应收增长异常"),
        ("revenue_growth", -1.0, 20.0, "营收增长异常"),
        ("profit_growth", -5.0, 20.0, "利润增长异常"),
    ]
    
    for field, lo, hi, msg in fin_checks:
        val = fd.get(field)
        if val is None: continue
        if val < lo or val > hi:
            anomalies.append({"field": field, "value": val, "range": f"[{lo}, {hi}]", "msg": msg})
            score -= 8
    
    # ---- 一致性校验 ----
    price = fd.get("price")
    mc = fd.get("market_cap")
    if price and mc and price > 0:
        shares = mc / price
        if shares < 100000:
            anomalies.append({"field": "market_cap", "value": mc, "msg": "市值/股价<10万股,市值单位可能错误"})
            score -= 15
        elif shares > 1e14:
            anomalies.append({"field": "market_cap", "value": mc, "msg": f"市值/股价={shares:.0f}股(过多),市值单位可能错误"})
            score -= 15
    
    # 检查PS合理性：市值/营收
    rev = fd.get("revenue")
    if mc and rev and rev > 0:
        ps_implied = mc / (rev * 4)  # TTM营收
        if ps_implied > 5000:
            anomalies.append({"field": "ps", "value": ps_implied, "msg": f"PS={ps_implied:.0f}x(过高),市值或营收单位可能错误"})
            score -= 10
    
    ep = fd.get("eps")
    pe = fd.get("pe")
    if ep and pe and pe > 0:
        implied_price = ep * pe
        if price and abs(implied_price / price - 1) > 0.5:
            pass  # EPS×PE不一定精确等于股价,不告警
    
    # ---- 缺失关键字段 ----
    critical_fields = ["price", "pe", "market_cap", "revenue", "net_income", "gross_margin"]
    for f in critical_fields:
        if f not in fd or fd.get(f) is None:
            if f not in missing:
                missing.append(f)
    
    # ---- 多期数据一致性 ----
    if fd.get("revenue_growth") is not None and fd.get("revenue") is not None and fd.get("price"):
        # 营收增速和营收数字应一致（正负方向合理）
        pass
    
    # ---- 缺失字段严重性分级 ----
    field_severity = {}
    for f in ["price", "pe", "market_cap", "revenue", "net_income", "gross_margin"]:
        field_severity[f] = "critical"
    for f in ["eps", "roe", "debt_ratio", "fcf_yield", "revenue_growth",
              "profit_growth", "roic", "roa", "dividend_yield", "pb", "ps",
              "pe_forward", "free_cf", "operating_cf", "book_value_per_share",
              "current_ratio", "cash_short_term", "ev_ebitda", "fcf_margin",
              "beta"]:
        field_severity[f] = "important"

    missing_severity = {"critical": [], "important": [], "optional": []}
    for f in missing:
        sev = field_severity.get(f, "optional")
        missing_severity[sev].append(f)

    # ---- 影响评估 ----
    impact_notes = []
    impact_map = {
        "营收/利润评分": ("revenue", "net_income", "gross_margin"),
        "估值评分": ("pe", "ps", "pb", "ev_ebitda"),
        "增长评分": ("revenue_growth", "profit_growth"),
        "盈利评分": ("roe", "roic", "roa"),
        "现金流评分": ("free_cf", "fcf_yield", "operating_cf"),
        "资产负债表评分": ("debt_ratio", "current_ratio", "cash_short_term"),
        "价格目标计算": ("price", "pe", "market_cap"),
    }
    for module, fields in impact_map.items():
        missing_in_module = [f for f in fields if f in missing]
        if missing_in_module:
            impact_notes.append(f"缺少{', '.join(missing_in_module)} \u2192 {module}不可用")
    
    # ---- 综合评分和状态 ----
    score = max(0, min(100, score))
    if score >= 80:
        status = "good"
    elif score >= 50:
        status = "warning"
    else:
        status = "error"
    
    # ---- 严重缺失降级 ----
    if missing_severity["critical"]:
        if score > 50:
            score = max(30, score - 20)  # 有严重缺失时最高50分
        status = "error"
    
    # 去重
    seen = set()
    unique_warnings = []
    for w in warnings:
        if w["msg"] not in seen:
            seen.add(w["msg"])
            unique_warnings.append(w)
    
    return {
        "status": status,
        "score": score,
        "fields_count": len([k for k in fd.keys() if not k.startswith("_")]),
        "warnings": [{"field": a["field"], "value": a["value"], "msg": a["msg"]} for a in anomalies],
        "notes": [{"field": w["field"], "value": w["value"], "msg": w["msg"]} for w in unique_warnings],
        "missing_fields": missing[:10],
        "missing_severity": missing_severity,
        "impact_notes": impact_notes
    }

# ====== API路由 ======

@app.route("/api/debug/sec")
def debug_sec():
    """SEC EDGAR 调试端点 - 诊断适配器是否正常工作"""
    sym = request.args.get("sym", "AAPL").upper()
    info = {
        "symbol": sym,
        "DATA_DIR": DATA_DIR,
        "quant_tool_dir": os.path.join(DATA_DIR, "quant_tool"),
        "isdir": os.path.isdir(os.path.join(DATA_DIR, "quant_tool")),
        "sys.path": [p for p in sys.path if "quant" in p.lower()],
    }
    try:
        from sec_edgar_adapter import parse_sec_financials, load_cik_map, get_cik
        info["adapter_import"] = "OK"
        
        # Step 1: Load CIK map
        info["step1_cik_map"] = "loading..."
        cik_map = load_cik_map()
        info["step1_cik_map"] = f"OK ({len(cik_map)} entries)"
        info["cik_count"] = len(cik_map)
        
        # Step 2: Get CIK for symbol
        info["step2_get_cik"] = "looking up..."
        cik = get_cik(sym)
        info["step2_get_cik"] = cik if cik else "NOT FOUND"
        if not cik:
            # Try to find MU in map
            mu_cik = cik_map.get("MU")
            info["mu_in_map"] = mu_cik
            return jsonify({"status": "error", "info": info})
        
        # Step 3: Fetch facts
        info["step3_fetch_facts"] = "fetching..."
        fd = {}
        fd = parse_sec_financials(fd, sym)
        info["step3_fetch_facts"] = "OK"
        
        # Results
        info["result"] = {
            "revenue": fd.get("revenue"),
            "net_income": fd.get("net_income"),
            "eps": fd.get("eps"),
            "report_date": fd.get("report_date"),
            "data_source": fd.get("_data_source_sec", False),
        }
        return jsonify({"status": "ok", "info": info})
    except Exception as e:
        import traceback
        info["error"] = str(e)
        info["traceback"] = traceback.format_exc()
        return jsonify({"status": "error", "info": info})

@app.route("/api/stock/<symbol>")
def stock(symbol):
    sym = symbol.upper()
    
    # 速率限制 (v9.0+)
    if request.remote_addr not in LOCAL_IPS and not _rl_check(request.remote_addr):
        return jsonify({"status": "error", "msg": "请求过于频繁，每分钟限制60次，请稍后再试"}), 429
    
    # 内存缓存 (v9.0+): 5分钟有效
    cache_key = f"stock:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)
    
    fd = fetch_live(sym)
    # 放宽检查: 有备用数据源(price/revenue/pe任一)即可, 不要求全部
    if not fd:
        return jsonify({"status": "error", "msg": f"{sym} 暂无数据"})
    has_price = fd.get("price") is not None
    has_financials = fd.get("revenue") is not None or fd.get("net_income") is not None or fd.get("pe") is not None
    if not (has_price or has_financials):
        return jsonify({"status": "error", "msg": f"{sym} 暂无数据"})

    name = fd.get("_name", sym)
    sector = fd.get("sector", "general")
    ds = fd.get("_data_source", "westock v8.6")
    result = {"name": name, "symbol": sym, "sector": sector,
              "finance_source": ds, "version": "v9.1"}

    today_fields = [("price","股价"),("pe","PE"),("pe_forward","Forward PE"),
                    ("market_cap","市值"),("pb","PB"),("ps","PS"),("dividend_yield","股息率"),
                    ("high_52w","52周高"),("low_52w","52周低"),("change_pct","涨幅")]
    for k, label in today_fields:
        if fd.get(k) is not None:
            result[k] = {"v": fd[k], "label": label, "date": "今日", "period": "行情"}

    fin_fields = {"revenue":"营收","net_income":"净利润","gross_margin":"毛利率","net_margin":"净利率",
                  "operating_margin":"营业利润率","eps":"EPS","ebit":"营业利润","ebitda":"EBITDA",
                  "cogs":"营业成本","gross_profit":"毛利润","revenue_growth":"营收增长","profit_growth":"利润增长",
                  "roe":"ROE","roa":"ROA","roic":"ROIC","debt_ratio":"负债率","fcf_yield":"FCF Yield",
                  "free_cf":"自由现金流","operating_cf":"经营现金流","capex":"资本支出","fcf_margin":"FCF Margin",
                  "total_assets":"总资产","total_liabilities":"总负债","equity":"股东权益",
                  "cash_short_term":"现金","inventory":"存货","receivables":"应收",
                  "current_ratio":"流动比率","quick_ratio":"速动比率","book_value_per_share":"每股净值",
                  "receivables_growth":"应收增长","inventory_growth":"存货增长",
                  "beta":"Beta系数","ev_ebitda":"EV/EBITDA","peg":"PEG"}
    for k, label in fin_fields.items():
        if fd.get(k) is not None:
            result[k] = {"v": fd[k], "label": label, 
                         "date": fd.get("_report_period") or fd.get("_report_date") or "最新", 
                         "period": "财报"}

    # 新系统数据
    if fd.get("percentiles"):
        result["percentiles"] = {"v": fd["percentiles"], "label": "行业分位", "period": "行业对比"}
    if fd.get("stability"):
        result["stability"] = {"v": fd["stability"], "label": "稳定性", "period": "多期分析"}
    if fd.get("cycle"):
        result["cycle"] = {"v": fd["cycle"], "label": "周期位置", "period": "周期分析"}
    if fd.get("risk"):
        result["risk"] = {"v": fd["risk"], "label": "风险评估", "period": "风险"}
    if fd.get("adjustments"):
        result["adjustments"] = {"v": fd["adjustments"], "label": "调整系数", "period": "评分调整"}
    if fd.get("_rating"):
        result["rating"] = {"v": fd["_rating"], "label": "机构评级", "period": "机构"}
    
    # 数据质量
    if fd.get("_data_quality"):
        result["data_quality"] = {"v": fd["_data_quality"], "label": "数据质量", "period": "验证"}
    
    resp = {"status": "ok", "data": result}
    _cache_set(cache_key, resp, CACHE_TTL["stock"])
    return jsonify(resp)

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "v8.6"})

# ====== 产业链知识库 ======
CHAINS_FILE = os.path.join(WORKSPACE, "industry_chains.json")
PORTFOLIO_FILE = os.path.join(WORKSPACE, "portfolio.json")

def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# (v8.3: get_chains_v83 替代此路由，见文件末尾)
# @app.route("/api/chains")
def get_chains_legacy():
    """返回所有产业链（含评分排序 + 动态热度）"""
    chains = _load_json(CHAINS_FILE, {})
    heat_data = _load_json(os.path.join(WORKSPACE, "chain_heat.json"), {})
    heat_chains = heat_data.get("chains", {})
    summary = {}
    
    for cid, c in chains.get("chains", {}).items():
        node_count = len(c.get("nodes", {}))
        total_companies = set()
        edge_count = 0
        bottlenecks = 0
        for nid, n in c.get("nodes", {}).items():
            for co in n.get("companies", []):
                total_companies.add(co)
            if n.get("bottleneck"):
                bottlenecks += 1
            edge_count += len(n.get("upstream", [])) + len(n.get("downstream", []))
        
        # 静态结构分（60分基准）
        structural_score = min(60, int(node_count * 3 + len(total_companies) * 2 + edge_count * 1 + bottlenecks * 5))
        
        # 动态热度分（40分基准，基于近7天新闻量）
        hc = heat_chains.get(cid, {})
        recent_counts = [d["count"] for d in hc.get("daily_counts", [])[-7:]]
        total_news_7d = sum(recent_counts) if recent_counts else 0
        heat_score = min(40, total_news_7d * 3)
        
        # 趋势方向
        net_scores = [d["net"] for d in hc.get("net_scores", [])[-7:] if d.get("net") is not None]
        avg_net = sum(net_scores) / len(net_scores) if net_scores else 0
        trend = "rising" if avg_net > 1 else "falling" if avg_net < -1 else "stable"
        
        chain_score = structural_score + heat_score
        
        summary[cid] = {
            "name": c.get("name", cid),
            "market": c.get("market", "US"),
            "timeframe": c.get("timeframe", "? "),
            "keywords": c.get("keywords", [])[:5],
            "node_count": node_count,
            "company_count": len(total_companies),
            "bottleneck_nodes": bottlenecks,
            "edge_count": edge_count,
            "structural_score": structural_score,
            "heat_score": heat_score,
            "chain_score": chain_score,
            "trend": trend,
            "recent_news_7d": total_news_7d
        }
    
    sorted_summary = dict(sorted(summary.items(), key=lambda x: -x[1]["chain_score"]))
    return jsonify({"status": "ok", "data": sorted_summary})

@app.route("/api/chains/<chain_id>")
def get_chain_detail(chain_id):
    """返回单条产业链的完整信息"""
    chains = _load_json(CHAINS_FILE, {})
    chain = chains.get("chains", {}).get(chain_id)
    if not chain:
        return jsonify({"status": "error", "msg": f"产业链 {chain_id} 不存在"})
    return jsonify({"status": "ok", "data": chain})

@app.route("/api/portfolio")
def get_portfolio():
    """返回组合看板数据"""
    pf = _load_json(PORTFOLIO_FILE, {})
    stocks = pf.get("stocks", {})
    # 按板块分组
    sectors = {}
    for sym, info in stocks.items():
        sector = info.get("sector", "其他")
        market = info.get("market", "US")
        key = f"[{market}] {sector}"
        if key not in sectors:
            sectors[key] = []
        sectors[key].append({"symbol": sym, **info})
    return jsonify({"status": "ok", "data": {
        "sectors": sectors,
        "last_scan": pf.get("last_scan"),
        "scan_status": pf.get("scan_status", "idle"),
        "pending_reviews": pf.get("pending_reviews", []),
        "stocks": stocks
    }})

@app.route("/api/portfolio/<symbol>")
def get_portfolio_stock(symbol):
    """返回组合中某只股票的详情"""
    sym = symbol.upper()
    pf = _load_json(PORTFOLIO_FILE, {})
    info = pf.get("stocks", {}).get(sym)
    if not info:
        return jsonify({"status": "error", "msg": f"{sym} 不在组合中"})
    return jsonify({"status": "ok", "data": info})

@app.route("/api/scan/start", methods=["POST"])
def start_scan():
    """启动全产业链扫描"""
    import threading as _th
    pf = _load_json(PORTFOLIO_FILE, {})
    pf["scan_status"] = "running"
    pf["scan_progress"] = {"current": 0, "total": 0, "message": "初始化..."}
    _save_json(PORTFOLIO_FILE, pf)
    
    def _run_scan():
        _do_scan()
    
    t = _th.Thread(target=_run_scan, daemon=True)
    t.start()
    return jsonify({"status": "ok", "msg": "扫描已启动"})

@app.route("/api/scan/status")
def scan_status():
    """查询扫描进度"""
    pf = _load_json(PORTFOLIO_FILE, {})
    return jsonify({
        "status": "ok",
        "data": {
            "scan_status": pf.get("scan_status", "idle"),
            "progress": pf.get("scan_progress", {}),
            "last_scan": pf.get("last_scan")
        }
    })

def _do_scan():
    """执行全产业链扫描（后台线程运行）"""
    import time as _t
    chains = _load_json(CHAINS_FILE, {}).get("chains", {})
    pf = _load_json(PORTFOLIO_FILE, {})
    pf["scan_progress"] = {"current": 0, "total": 0, "message": "扫描中..."}
    
    # 收集所有要去重的标的
    all_targets = {}  # symbol -> {sector, chain_id, node_name}
    for cid, chain in chains.items():
        sector_name = chain.get("name", cid)
        chain_market = chain.get("market", "US")
        chain_timeframe = chain.get("timeframe", "?")
        for nid, node in chain.get("nodes", {}).items():
            for sym in node.get("companies", []):
                if sym and sym not in all_targets:
                    all_targets[sym] = {
                        "sector": sector_name,
                        "chain_id": cid,
                        "node_key": nid,
                        "node_name": node.get("name", nid),
                        "node_desc": node.get("note", node.get("name", nid)),
                        "market": chain_market,
                        "timeframe": chain_timeframe,
                        "supplier_count": node.get("supplier_count", 5),
                        "is_bottleneck": node.get("bottleneck", False)
                    }
    
    total = len(all_targets)
    pf["scan_progress"] = {"current": 0, "total": total, "message": f"共 {total} 只股票"}
    _save_json(PORTFOLIO_FILE, pf)
    
    stocks = pf.get("stocks", {})
    new_discoveries = []
    
    for i, (sym, meta) in enumerate(all_targets.items()):
        try:
            # 更新进度
            pf["scan_progress"] = {"current": i+1, "total": total, "message": f"({i+1}/{total}) {sym}"}
            _save_json(PORTFOLIO_FILE, pf)
            
            # 拉取数据
            from urllib.request import urlopen as _urlopen
            url = f"http://127.0.0.1:5001/api/stock/{sym}"
            resp = _urlopen(url, timeout=20)
            data = json.loads(resp.read().decode())
            if data.get("status") != "ok":
                continue
            d = data["data"]
            
            # 提取关键打分
            price = d.get("price", {}).get("v")
            pe = d.get("pe", {}).get("v")
            revenue = d.get("revenue", {}).get("v")
            growth = d.get("revenue_growth", {}).get("v")
            net_margin = d.get("net_margin", {}).get("v")
            roic = d.get("roic", {}).get("v")
            debt_ratio = d.get("debt_ratio", {}).get("v")
            market_cap = d.get("market_cap", {}).get("v")
            
            # 5框架简评
            scores = {}
            scores["growth"] = _score_growth(growth, net_margin)
            scores["quality"] = _score_quality(roic, debt_ratio, net_margin)
            scores["value"] = _score_value(pe, market_cap, revenue)
            
            # 紫苏叶瓶颈评分
            is_bottleneck = meta.get("is_bottleneck", False)
            bottleneck_score = _score_bottleneck(meta, chain_info=chains.get(meta["chain_id"], {}))
            
            # 聚合评分
            overall = round((scores.get("growth",0)*0.3 + scores.get("quality",0)*0.3 + 
                           scores.get("value",0)*0.2 + bottleneck_score*0.2), 1)
            
            entry = {
                "symbol": sym,
                "sector": meta["sector"],
                "market": meta.get("market", "US"),
                "chain_id": meta["chain_id"],
                "timeframe": meta.get("timeframe", "?"),
                "node_name": meta["node_name"],
                "node_desc": meta.get("node_desc", meta["node_name"]),
                "price": price,
                "pe": pe,
                "market_cap": market_cap,
                "scores": scores,
                "bottleneck_score": bottleneck_score,
                "overall": overall,
                "first_seen": stocks.get(sym, {}).get("first_seen", _t.strftime("%Y-%m-%d")),
                "last_update": _t.strftime("%Y-%m-%d"),
                "stale_count": 0
            }
            
            # 去重：存在就更新，不存在新增
            if sym in stocks:
                entry["first_seen"] = stocks[sym]["first_seen"]
                entry["stale_count"] = 0
            else:
                new_discoveries.append(sym)
            
            stocks[sym] = entry
            _t.sleep(0.3)  # 防限流
            
        except Exception as e:
            print(f"[scan] {sym}: {e}")
            # 如果之前就在组合里但这次失败，增加stale_count
            if sym in stocks:
                stocks[sym]["stale_count"] = stocks[sym].get("stale_count", 0) + 1
                # 连续3次失败标记待移除
                if stocks[sym]["stale_count"] >= 3:
                    pf.setdefault("pending_reviews", []).append({
                        "symbol": sym,
                        "reason": "连续3次扫描失败",
                        "type": "removal"
                    })
    
    # 淘汰检查
    for sym in list(stocks.keys()):
        if stocks[sym].get("stale_count", 0) >= 3:
            if sym not in [r.get("symbol") for r in pf.get("pending_reviews", [])]:
                pf.setdefault("pending_reviews", []).append({
                    "symbol": sym,
                    "reason": "连续3次扫描失败/不合格",
                    "type": "removal"
                })
    
    pf["stocks"] = stocks
    pf["last_scan"] = _t.strftime("%Y-%m-%d %H:%M")
    pf["scan_status"] = "completed"
    pf["scan_progress"] = {"current": total, "total": total, "message": f"完成！新增{len(new_discoveries)}只"}
    _save_json(PORTFOLIO_FILE, pf)

def _score_growth(growth, net_margin):
    """成长评分 (0-100)"""
    s = 0
    if growth and growth > 0: s += min(growth * 200, 60)
    elif growth and growth < 0: s -= 20
    if net_margin and net_margin > 0.15: s += 20
    elif net_margin and net_margin > 0.05: s += 10
    return max(0, min(100, round(s)))

def _score_quality(roic, debt_ratio, net_margin):
    """质量评分 (0-100)"""
    s = 0
    if roic and roic > 0.15: s += 30
    elif roic and roic > 0.08: s += 15
    if debt_ratio and debt_ratio < 0.5: s += 25
    elif debt_ratio and debt_ratio < 0.8: s += 10
    else: s -= 10
    if net_margin and net_margin > 0.2: s += 25
    elif net_margin and net_margin > 0.1: s += 15
    if roic and roic > 0: s += min(roic * 100, 20)
    return max(0, min(100, round(s)))

def _score_value(pe, market_cap, revenue):
    """价值评分 (0-100)"""
    s = 50
    if pe and pe > 0 and pe < 15: s += 30
    elif pe and pe > 0 and pe < 25: s += 15
    elif pe and pe > 50: s -= 20
    if market_cap and revenue and revenue > 0:
        ps = market_cap / (revenue * 4)
        if ps < 2: s += 20
        elif ps < 5: s += 10
        elif ps > 15: s -= 10
    return max(0, min(100, round(s)))

def _score_bottleneck(meta, chain_info=None):
    """紫苏叶瓶颈评分 (0-100) — 基于Serenity方法论"""
    s = 50  # 基础分
    node = None
    if chain_info:
        # 优先用 node_key 精确匹配
        nk = meta.get("node_key", "")
        if nk:
            node = chain_info.get("nodes", {}).get(nk, {})
        # 备用：按node_name匹配
        if not node:
            node = chain_info.get("nodes", {}).get(meta.get("node_name", "").lower(), {})
        # 再备用：按symbol扫描所有节点
        if not node:
            for nid, n in chain_info.get("nodes", {}).items():
                if meta.get("symbol","") in n.get("companies", []):
                    node = n
                    break
    
    if node:
        sc = node.get("supplier_count", 5)
        if sc == 1: s += 30  # 独家垄断
        elif sc == 2: s += 20  # 双寡头
        elif sc == 3: s += 10  # 寡头
        elif sc >= 5: s -= 10  # 充分竞争
        
        if node.get("bottleneck"): s += 15  # 明确瓶颈标记
    
    # 新闻热度加分
    # (预留：后续接入新闻分析后增加媒体热度因子)
    
    return max(0, min(100, round(s)))

@app.route("/api/chain/import", methods=["POST"])
def import_chain_suggestion():
    """手动导入推文/建议到待审核"""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    chain_id = data.get("chain_id", "")
    note = data.get("note", "")
    if not symbol:
        return jsonify({"status": "error", "msg": "请提供股票代码"})
    
    pf = _load_json(PORTFOLIO_FILE, {})
    pf.setdefault("pending_reviews", []).append({
        "symbol": symbol,
        "chain_id": chain_id,
        "note": note,
        "type": "addition",
        "source": data.get("source", "manual"),
        "time": __import__("time").strftime("%Y-%m-%d %H:%M")
    })
    _save_json(PORTFOLIO_FILE, pf)
    return jsonify({"status": "ok", "msg": f"{symbol} 已加入待审核"})

@app.route("/api/review/approve", methods=["POST"])
def approve_review():
    """批准待审核建议"""
    data = request.get_json() or {}
    idx = data.get("index")
    pf = _load_json(PORTFOLIO_FILE, {})
    reviews = pf.get("pending_reviews", [])
    if idx is None or idx < 0 or idx >= len(reviews):
        return jsonify({"status": "error", "msg": "无效索引"})
    
    item = reviews.pop(idx)
    pf["pending_reviews"] = reviews
    _save_json(PORTFOLIO_FILE, pf)
    return jsonify({"status": "ok", "msg": f"{item.get('symbol','?')} 已批准"})

@app.route("/api/review/reject", methods=["POST"])
def reject_review():
    """拒绝待审核建议"""
    data = request.get_json() or {}
    idx = data.get("index")
    pf = _load_json(PORTFOLIO_FILE, {})
    reviews = pf.get("pending_reviews", [])
    if idx is None or idx < 0 or idx >= len(reviews):
        return jsonify({"status": "error", "msg": "无效索引"})
    
    item = reviews.pop(idx)
    pf["pending_reviews"] = reviews
    _save_json(PORTFOLIO_FILE, pf)
    return jsonify({"status": "ok", "msg": f"{item.get('symbol','?')} 已忽略"})

@app.route("/")
@app.route("/<path:path>")
def serve(path="quant.html"):
    resp = send_from_directory(WORKSPACE, path)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ====== v8.0 产业传播引擎 ======
# 三层结构: 实体识别 → 方向分类 → 关系传播
import re as _re

DEFAULT_IMPACT_PROFILE = {
    "demand_up": {"self": 1.0, "upstream": 0.8, "downstream": 0.3},
    "demand_down": {"self": -1.0, "upstream": -0.4, "downstream": -0.7},
    "supply_up": {"self": 0.6, "upstream": 0.8, "downstream": 0.2},
    "supply_down": {"self": -1.0, "upstream": 0.4, "downstream": -0.8}
}

def load_chains_full():
    """加载完整产业链数据（含nodes关系图）"""
    chains_file = os.path.join(WORKSPACE, "industry_chains.json")
    return _load_json(chains_file, {}).get("chains", {})

def _kw_hit(text, kw):
    """关键词匹配（英文ticker加边界识别）"""
    if _re.fullmatch(r"[A-Za-z.]{1,7}", kw):
        return _re.search(rf"\b{_re.escape(kw)}\b", text, _re.I) is not None
    return kw.lower() in text.lower()

def entity_extract(news_text, chain):
    """从新闻中提取命中的产业链节点"""
    hits = []
    text = news_text.lower()
    for nid, node in chain.get("nodes", {}).items():
        for kw in node.get("keywords", []):
            if _kw_hit(text, kw):
                hits.append(nid)
                break
    return hits

def classify_direction(news_text):
    """识别新闻影响方向（多方向输出，支持复合新闻）"""
    text = news_text.lower()
    demand_up = ["增长","订单","扩产","投资","increase","growth","order",
                 "expand","launch","推出","供不应求","紧缺","产能不足","surge","soar","boom"]
    demand_down = ["下滑","下降","减少","裁员","decline","drop","layoff",
                   "库存积压","过剩","需求疲软","slump","plunge","slowdown"]
    supply_up = ["建厂","capex","资本开支","扩建","新工厂","新产线",
                 "fab","fabrication","build","construction","capacity expansion"]
    supply_down = ["停产","断供","制裁","罢工","strike","shortage","短缺",
                   "shutdown","halt","suspend"]
    # 否定/转折词（中英文）
    negators = ["未","不","没有","不及预期","放缓","低于预期","下降趋势",
                "not","no","below","fewer","less","underperform","missed","failed"]
    
    evidence = []
    directions = []
    
    # 需求方向（互斥：up和down同时出现时，用分数高的）
    matched_du = [w for w in demand_up if w in text]
    matched_dd = [w for w in demand_down if w in text]
    matched_su = [w for w in supply_up if w in text]
    matched_sd = [w for w in supply_down if w in text]
    
    # 否定词检测：如果在词汇附近有否定词，反转方向
    def is_negated(word):
        for neg in negators:
            if neg in text:
                idx = text.find(neg)
                word_idx = text.find(word)
                if word_idx >= 0 and abs(word_idx - idx) < 20:
                    return True
        return False
    
    # 需求方向
    net_demand = 0
    for w in matched_du: 
        if not is_negated(w): net_demand += 2; evidence.append(w)
    for w in matched_dd: 
        if not is_negated(w): net_demand -= 2; evidence.append(w)
    
    if net_demand >= 2: directions.append("demand_up")
    elif net_demand <= -2: directions.append("demand_down")
    
    # 供给方向（独立于需求）
    net_supply = 0
    for w in matched_su:
        if not is_negated(w): net_supply += 1; evidence.append(w)
    for w in matched_sd:
        if not is_negated(w): net_supply -= 1; evidence.append(w)
    
    if net_supply >= 1: directions.append("supply_up")
    elif net_supply <= -1: directions.append("supply_down")
    
    if not directions:
        directions = ["neutral"]
    
    total_score = abs(net_demand) + abs(net_supply)
    confidence = min(total_score / 6.0, 1.0)
    
    # 构建结构化的 logic_path 所需信息
    direction_labels = {
        "demand_up": "需求上升", "demand_down": "需求下降",
        "supply_up": "供给扩张", "supply_down": "供给收缩"
    }
    
    return {
        "directions": directions,
        "primary": directions[0] if directions[0] != "neutral" else "neutral",
        "confidence": round(confidence, 2),
        "evidence": evidence[:5],
        "direction_labels": [direction_labels.get(d, d) for d in directions if d != "neutral"]
    }

def propagate_multi(chain, start_nodes, directions, max_depth=2):
    """
    多方向传播算法
    支持复合方向（如 demand_up + supply_up 同时传播再叠加）
    从命中节点出发，沿upstream/downstream传播
    """
    aggregate = {}
    
    def dfs(nid, depth, weight, path, direction, profile):
        if depth > max_depth:
            return
        if nid not in chain.get("nodes", {}):
            return
        if nid in path:
            return
        node = chain["nodes"][nid]
        new_path = path | {nid}
        
        self_w = profile.get("self", 0)
        if self_w == 0:
            return
        
        score = weight * self_w * node.get("sensitivity", 1.0) * node.get("bottleneck_weight", 1.0)
        
        for comp in node.get("companies", []):
            aggregate[comp] = round(aggregate.get(comp, 0) + score, 4)
        
        for nxt in node.get("downstream", []):
            dfs(nxt, depth + 1, score * profile.get("downstream", 0.3), new_path, direction, profile)
        for nxt in node.get("upstream", []):
            dfs(nxt, depth + 1, score * profile.get("upstream", 0.8), new_path, direction, profile)
    
    for direction in directions:
        if direction == "neutral":
            continue
        profile = chain.get("impact_profile", {}).get(direction, DEFAULT_IMPACT_PROFILE.get(direction, {}))
        if not profile.get("self"):
            profile = DEFAULT_IMPACT_PROFILE.get(direction, {})
        for nid in start_nodes:
            dfs(nid, 0, 1.0, set(), direction, profile)
    
    return dict(sorted(aggregate.items(), key=lambda x: -x[1]))

def get_max_depth(chain):
    """根据链规模动态调整传播深度"""
    nc = len(chain.get("nodes", {}))
    if nc >= 10: return 3
    elif nc >= 5: return 2
    return 1

DEPTH_DECAY = [1.0, 0.6, 0.35, 0.18]

def propagate_full_multi(chain, start_nodes, directions, max_depth=None):
    """
    多方向完整传播（impact_matrix.total = impact_scores 数学一致）
    score = weight × self_w × sensitivity × bottleneck_weight  (含瓶颈放大)
    matrix拆解:
      demand_part     = weight × self_w × sensitivity           (无瓶颈)
      bottleneck_part = demand_part × (bottleneck_weight - 1)   (仅瓶颈放大倍数)
      sentiment_part  = demand_part × sentiment_coef
      total = demand + bottleneck + sentiment = score           (数学一致)
    """
    if max_depth is None:
        max_depth = get_max_depth(chain)
    impact_scores = {}
    matrix = {}
    propagation_paths = []
    
    def add_mx(comp, dim, val):
        if comp not in matrix:
            matrix[comp] = {"demand":0,"supply":0,"bottleneck":0,"sentiment":0,"total":0}
        matrix[comp][dim] = round(matrix[comp].get(dim,0) + val, 4)
    
    def dfs(nid, depth, weight, path, direction, profile, path_chain):
        if depth > max_depth:
            return
        if nid not in chain.get("nodes", {}):
            return
        if nid in path:
            return
        node = chain["nodes"][nid]
        new_path = path | {nid}
        
        self_w = profile.get("self", 0)
        if self_w == 0:
            return
        
        sens = node.get("sensitivity", 1.0)
        bw = node.get("bottleneck_weight", 1.0)
        
        # 拆解 score: demand_part + bottleneck_part + sentiment_part
        demand_part = weight * self_w * sens
        bottleneck_part = demand_part * (bw - 1)  # 仅瓶颈引发的额外放大
        depth_dec = DEPTH_DECAY[depth] if depth < len(DEPTH_DECAY) else 0.1
        sentiment_part = demand_part * 0.05 * depth_dec  # 情感贡献，随深度衰减
        
        score = demand_part + bottleneck_part + sentiment_part
        
        sign = 1 if direction in ("demand_up", "supply_up") else -1
        
        for comp in node.get("companies", []):
            impact_scores[comp] = round(impact_scores.get(comp, 0) + score, 4)
            add_mx(comp, "demand", demand_part)
            add_mx(comp, "bottleneck", sign * bottleneck_part)
            add_mx(comp, "sentiment", sentiment_part * (1 if direction in ("demand_up","supply_up") else -1))
        
        current_path = path_chain + [{
            "node": nid, "name": node.get("name", nid),
            "companies": node.get("companies", []),
            "depth": depth, "direction": direction,
            "score": round(score, 4)
        }]
        propagation_paths.append(current_path)
        
        decay = profile.get("upstream", 0.8) if "up" in direction else profile.get("downstream", -0.7)
        for nxt in node.get("downstream", []):
            dfs(nxt, depth + 1, score * profile.get("downstream", 0.3), new_path, direction, profile, current_path)
        for nxt in node.get("upstream", []):
            dfs(nxt, depth + 1, score * profile.get("upstream", 0.8), new_path, direction, profile, current_path)
    
    for direction in directions:
        if direction == "neutral":
            continue
        profile = chain.get("impact_profile", {}).get(direction, DEFAULT_IMPACT_PROFILE.get(direction, {}))
        if not profile.get("self"):
            profile = DEFAULT_IMPACT_PROFILE.get(direction, {})
        for nid in start_nodes:
            dfs(nid, 0, 1.0, set(), direction, profile, [])
    
    for comp in matrix:
        matrix[comp]["total"] = round(impact_scores.get(comp, 0), 4)
    
    # 计算传播置信度
    path_conf_map = {}
    for comp in impact_scores:
        depth = 0
        for p in propagation_paths:
            for step in p:
                if comp in step.get("companies", []):
                    depth = step["depth"]
                    break
        path_conf_map[comp] = round(0.85 * (0.7 ** depth), 2)
    
    # 路径排序取Top3
    propagation_paths.sort(key=lambda p: -(p[-1]["score"] if p else 0))
    top_paths = propagation_paths[:3]
    
    direction_labels = {"demand_up":"需求上升","demand_down":"需求下降",
                        "supply_up":"供给扩张","supply_down":"供给收缩"}
    logic_path = {
        "trigger_nodes": start_nodes,
        "trigger_names": [chain.get("nodes",{}).get(n,{}).get("name",n) for n in start_nodes],
        "directions": directions,
        "direction_labels": [direction_labels.get(d,d) for d in directions if d != "neutral"],
        "summary": f"{' '.join(direction_labels.get(d,'') for d in directions if d!='neutral')} → 影响 {len(impact_scores)} 家公司",
        "path_confidence": dict(sorted(path_conf_map.items(), key=lambda x: -x[1])),
        "top_paths": [
            {
                "nodes": [p["name"] for p in path],
                "companies": list(set(c for p in path for c in p.get("companies",[]))),
                "max_depth": max(p["depth"] for p in path),
                "direction": path[0]["direction"] if path else "",
                "total_score": round(sum(p["score"] for p in path), 4)
            }
            for path in top_paths
        ]
    }
    
    return {
        "impact_scores": dict(sorted(impact_scores.items(), key=lambda x: -x[1])),
        "impact_matrix": dict(sorted(matrix.items(), key=lambda x: -x[1]["total"])),
        "logic_path": logic_path
    }

def time_decay(article_time_str, half_life_days=3, default_age_days=3):
    """时间衰减: 越新的新闻权重越高"""
    if not article_time_str or article_time_str in ("nan", "", "None"):
        return 0.5 ** (default_age_days / half_life_days)
    try:
        from datetime import datetime, timezone
        pub_time = datetime.fromisoformat(article_time_str.replace("Z","+00:00").split(".")[0])
        now = datetime.now(timezone.utc)
        if pub_time.tzinfo is None:
            pub_time = pub_time.replace(tzinfo=timezone.utc)
        days_old = abs((now - pub_time).total_seconds() / 86400)
        return max(0.1, 0.5 ** (days_old / half_life_days))
    except:
        return 0.5 ** (default_age_days / half_life_days)

def time_propagation(impact_score):
    """时间维度推导: 需求→收入→利润→估值"""
    rev_sens = 0.6
    ear_sens = 0.4
    pe_sens = 0.3
    return {
        "3m_revenue": round(impact_score * rev_sens, 4),
        "6m_earnings": round(impact_score * rev_sens * ear_sens, 4),
        "12m_valuation": round(impact_score * rev_sens * ear_sens * pe_sens, 4)
    }

# ====== 新闻扫描 & 语义分析 API ======

# ====== 新闻扫描 & 语义分析 API ======

def _fetch_news(keywords, max_results=50):
    """拉取全球财经新闻（akshare + 备用源）"""
    articles = []
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                title = str(row.get("标题", row.get("title", "")))
                content = str(row.get("内容", row.get("content", "")))
                time_str = str(row.get("发布时间", row.get("time", "")))
                if title and title != "nan":
                    articles.append({
                        "title": title,
                        "content": content if content != "nan" else "",
                        "time": time_str if time_str != "nan" else "",
                        "source": "东方财富"
                    })
    except Exception as e:
        print(f"[news] akshare global: {e}")
    
    # 备用：百度财经新闻
    if len(articles) < 5:
        try:
            import akshare as ak
            df = ak.news_economic_baidu(date="")
            if df is not None and len(df) > 0:
                for _, row in df.head(30).iterrows():
                    title = str(row.get("title", row.get("标题", "")))
                    if title and title != "nan":
                        articles.append({
                            "title": title,
                            "content": "",
                            "time": "",
                            "source": "百度财经"
                        })
        except Exception as e:
            print(f"[news] baidu: {e}")
    
    return articles[:max_results]

# ====== v8.1 Phase 4+ 高级功能 ======

def update_chain_heat(propagation_results):
    """更新链新闻热度缓存"""
    heat_file = os.path.join(WORKSPACE, "chain_heat.json")
    heat = _load_json(heat_file, {"history": [], "chains": {}})
    now = __import__("time").strftime("%Y-%m-%d")
    
    for cid, prop in propagation_results.items():
        news_count = len(prop.get("events", []))
        net = prop.get("net_score", 0)
        if cid not in heat["chains"]:
            heat["chains"][cid] = {"daily_counts": [], "net_scores": []}
        heat["chains"][cid]["daily_counts"].append({"date": now, "count": news_count})
        heat["chains"][cid]["net_scores"].append({"date": now, "net": net})
    
    # 保留最近30天
    for cid in heat["chains"]:
        heat["chains"][cid]["daily_counts"] = heat["chains"][cid]["daily_counts"][-30:]
        heat["chains"][cid]["net_scores"] = heat["chains"][cid]["net_scores"][-30:]
    
    heat["last_updated"] = now
    _save_json(heat_file, heat)

def detect_resonance(propagation_results):
    """跨链共振检测：同一公司在多条链都获得正分"""
    from collections import defaultdict
    company_chains = defaultdict(list)
    for cid, prop in propagation_results.items():
        for comp, score in prop.get("positive", {}).items():
            company_chains[comp].append({"chain": cid, "score": score})
    
    resonance = {}
    for comp, chains in company_chains.items():
        if len(chains) >= 2:
            resonance[comp] = {
                "chains": chains,
                "total_score": round(sum(c["score"] for c in chains), 4),
                "chain_count": len(chains)
            }
    return dict(sorted(resonance.items(), key=lambda x: -x[1]["total_score"]))

def compute_rotation(propagation_results):
    """行业轮动信号：基于各链净倾向"""
    rotation = []
    for cid, prop in propagation_results.items():
        net = prop.get("net_score", 0)
        positivity = "rising" if net > 0 else "falling" if net < 0 else "neutral"
        rotation.append({
            "chain_id": cid,
            "chain_name": cid,
            "net_score": round(net, 4),
            "trend": positivity,
            "event_count": len(prop.get("events", []))
        })
    rotation.sort(key=lambda x: -x["net_score"])
    return rotation

def auto_discover_chains(articles, existing_chains):
    """自动发现候选产业链（共现聚类）"""
    from collections import defaultdict
    import itertools
    
    stock_list = set()
    for chain in existing_chains.values():
        for n in chain.get("nodes", {}).values():
            for c in n.get("companies", []):
                stock_list.add(c.upper())
    
    co_matrix = defaultdict(int)
    for art in articles:
        text = (art.get("title", "") + " " + art.get("content", "")).upper()
        mentioned = [s for s in stock_list if s in text]
        for a, b in itertools.combinations(mentioned, 2):
            key = tuple(sorted([a, b]))
            co_matrix[key] += 1
    
    # 简单聚类：强共现对 → 候选链
    from collections import defaultdict as dd
    clusters = dd(set)
    for (a, b), weight in co_matrix.items():
        if weight >= 2:
            clusters[a].add(b)
            clusters[b].add(a)
    
    candidates = []
    for seed, neighbors in clusters.items():
        if len(neighbors) >= 3:
            group = {seed} | neighbors
            exists = any(
                seed in [c for n in ch.get("nodes",{}).values() for c in n.get("companies",[])]
                for ch in existing_chains.values()
            )
            if not exists:
                candidates.append({
                    "seed_stock": seed,
                    "related": list(neighbors)[:5],
                    "size": len(group),
                    "status": "candidate"
                })
    
    return candidates[:5]

def _match_articles(articles, chain_keywords, chain_info, all_chains=None):
    """将新闻匹配到产业链 → 挖掘潜在机会（非周期推导）"""
    results = {"articles": [], "chain_matches": {}, "hot_topics": [], "new_themes": [], "opportunity_analysis": {}}
    keyword_freq = {}
    cross_chain_map = {}  # 关键词 → 跨链追踪
    
    for art in articles:
        text = (art.get("title", "") + " " + art.get("content", "")).lower()
        matched_chains = []
        sentiment = 0
        
        for cid, kws in chain_keywords.items():
            for kw in kws:
                if kw and kw.lower() in text:
                    matched_chains.append(cid)
                    keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
                    cross_chain_map.setdefault(kw, {"chains": set(), "count": 0})
                    cross_chain_map[kw]["chains"].add(cid)
                    cross_chain_map[kw]["count"] += 1
                    # 情感
                    pos_words = ["投资","增长","突破","合作","研发","订单","capit","invest","growth","order","announce","expand","launch","推出"]
                    neg_words = ["下跌","裁员","风险","亏损","限制","制裁","decline","risk","loss","delay","delay"]
                    if any(w in text for w in pos_words): sentiment = max(sentiment, 0.5)
                    if any(w in text for w in neg_words): sentiment = min(sentiment, -0.3)
                    break
        
        art["matched_chains"] = matched_chains
        art["sentiment"] = sentiment
        results["articles"].append(art)
    
    # 热词（带跨界标记）
    results["hot_topics"] = sorted([
        {"keyword": k, "count": v["count"], "cross_chains": len(v["chains"])}
        for k, v in cross_chain_map.items()],
        key=lambda x: -x["count"]
    )[:20]
    
    # 新主题挖掘：高频但跨链少 → 可能是新兴赛道
    for t in results["hot_topics"][:10]:
        if t["cross_chains"] <= 1 and t["count"] >= 3:
            results["new_themes"].append({
                "keyword": t["keyword"],
                "count": t["count"],
                "note": f"该关键词高频({t['count']}次)但少匹配现有产业链，可能代表新兴赛道"
            })
    
    # ===== 机会挖掘评分 =====
    upstream_kws = ["capit","投资","建厂","扩产","供应链","supply","设备","厂房",
                   "infrastructure","制造","产能","生产线","facility","capacity","订单","order"]
    
    for cid, kws in chain_keywords.items():
        chain_arts = [a for a in results["articles"] if cid in a["matched_chains"]]
        news_count = len(chain_arts)
        pos = sum(1 for a in chain_arts if a["sentiment"] > 0.3)
        neg = sum(1 for a in chain_arts if a["sentiment"] < -0.1)
        
        # 跨界传播度
        cross_count = sum(len(cross_chain_map[kw]["chains"]) - 1 
                         for kw in kws if kw in cross_chain_map)
        cross_score = min(cross_count * 3, 30)
        
        # 上游供给信号
        upstream_signal = sum(1 for a in chain_arts 
                            if any(kw in (a.get("title","")+a.get("content","")).lower() 
                                  for kw in upstream_kws))
        
        # 4维评分
        score_news = min(news_count * 4, 35)
        score_cross = cross_score
        score_upstream = min(upstream_signal * 8, 25)
        score_new = 10 if results.get("new_themes") and new_theme_matches(results["new_themes"], kws) else 0
        
        total = round(score_news + score_cross + score_upstream + score_new, 1)
        
        signals = []
        if pos > neg * 2: signals.append("🟢利好主导")
        if news_count > 10: signals.append("📈高频曝光")
        if upstream_signal > 3: signals.append("🏭上游触发")
        if cross_count > 3: signals.append("🔗跨界传导")
        if score_new > 0: signals.append("✨新主题")
        
        label_map = [(70, "🔥 高潜力", "#3fb950"), (40, "📈 成长中", "#58a6ff"), (15, "👀 关注", "#d29922")]
        label, lc = "—", "#484f58"
        for th, lb, cl in label_map:
            if total >= th: label = lb; lc = cl; break
        
        results["opportunity_analysis"][cid] = {
            "name": chain_info.get(cid, {}).get("name", cid),
            "news_count": news_count, "cross_impact": cross_count,
            "upstream_signal": upstream_signal, "total": total,
            "label": label, "label_color": lc,
            "signals": signals or ["暂无"],
            "related_chains": list(set(cid2 for kw in kws if kw in cross_chain_map for cid2 in cross_chain_map[kw]["chains"] if cid2 != cid))[:5]
        }
    
    results["total"] = len(results["articles"])
    results["positive"] = sum(1 for a in results["articles"] if a["sentiment"] > 0.3)
    results["negative"] = sum(1 for a in results["articles"] if a["sentiment"] < -0.1)
    return results

def new_theme_matches(themes, keywords):
    return any(t["keyword"].lower() in kw.lower() or kw.lower() in t["keyword"].lower() 
              for t in themes for kw in keywords)

# --- news_scan v8.0 (原始版本，被 news_scan_v83 覆盖) ---
# 路由注册已移至 news_scan_v83，此函数仅保留作为引用
def news_scan():
    """全市场新闻扫描 + 产业链匹配"""
    # 速率限制 (v9.0+)
    if request.remote_addr not in LOCAL_IPS and not _rl_check(request.remote_addr):
        return jsonify({"status": "error", "msg": "请求过于频繁，请稍后再试"}), 429
    
    chain_id = request.args.get("chain_id", "")
    
    # 内存缓存 (v9.0+): 30分钟有效
    cache_key = f"news:{chain_id or 'all'}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)
    
    chains = _load_json(CHAINS_FILE, {}).get("chains", {})
    
    if chain_id:
        if chain_id not in chains:
            return jsonify({"status": "error", "msg": "产业链不存在"})
        target = {chain_id: chains[chain_id]}
    else:
        target = chains
    
    chain_keywords = {}
    for cid, chain in target.items():
        kws = list(set(chain.get("keywords", []) + [chain.get("name", "")]))
        chain_keywords[cid] = kws
    
    all_kws = list(set(kw for kws in chain_keywords.values() for kw in kws if kw))
    
    # 拉取新闻
    articles = _fetch_news(all_kws[:25])
    
    # 匹配分析
    results = _match_articles(articles, chain_keywords, target)
    
    # ===== v8.0 传播引擎：实体识别→方向分类→传播推导（直接对所有链做实体识别）=====
    full_chains = load_chains_full()
    propagation_results = {}
    for art in articles[:20]:
        text = art.get("title", "") + " " + art.get("content", "")
        art_time = art.get("time", "")
        decay = time_decay(art_time)
        
        for cid, chain in full_chains.items():
            if not chain.get("nodes"):
                continue
            entities = entity_extract(text, chain)
            if not entities:
                continue
            dc = classify_direction(text)
            if dc["primary"] == "neutral":
                continue
            
            prop = propagate_full_multi(chain, entities, dc["directions"])
            impact = prop["impact_scores"]
            breakdown = prop["impact_matrix"]
            logic_path = prop["logic_path"]
            
            time_proj = {}
            for comp, score in list(impact.items())[:3]:
                time_proj[comp] = time_propagation(score)
            
            if cid not in propagation_results:
                propagation_results[cid] = {"events": [], "aggregate_scores": {}}
            
            positive = {k:v for k,v in impact.items() if v > 0}
            negative = {k:v for k,v in impact.items() if v < 0}
            
            propagation_results[cid]["events"].append({
                "article_title": art.get("title", "")[:80],
                "directions": dc["directions"],
                "direction_labels": dc.get("direction_labels", []),
                "direction_confidence": dc["confidence"],
                "direction_evidence": dc["evidence"],
                "entity_nodes": entities,
                "impact_scores": dict(list(impact.items())[:5]),
                "positive_scores": dict(list(positive.items())[:5]),
                "negative_scores": dict(list(negative.items())[:5]),
                "impact_matrix": breakdown,
                "time_projection": time_proj,
                "logic_path": logic_path
            })
            for comp, score in impact.items():
                propagation_results[cid]["aggregate_scores"][comp] = round(
                    propagation_results[cid]["aggregate_scores"].get(comp, 0) + score * decay, 4
                )
    
    # 聚合后计算正负和净倾向
    for cid in propagation_results:
        agg = propagation_results[cid]["aggregate_scores"]
        propagation_results[cid]["aggregate_scores"] = dict(sorted(agg.items(), key=lambda x: -abs(x[1])))
        propagation_results[cid]["positive"] = dict(sorted(
            {k:v for k,v in agg.items() if v > 0}.items(), key=lambda x: -x[1]))
        propagation_results[cid]["negative"] = dict(sorted(
            {k:v for k,v in agg.items() if v < 0}.items(), key=lambda x: x[1]))
        net = sum(agg.values())
        propagation_results[cid]["net_sentiment"] = "bullish" if net > 0 else "bearish" if net < 0 else "neutral"
        propagation_results[cid]["net_score"] = round(net, 4)
    
    results["propagation"] = propagation_results
    
    # ===== Phase 4+ 高级功能：热度更新 + 共振 + 轮动 + 自动发现 =====
    update_chain_heat(propagation_results)
    
    results["resonance"] = detect_resonance(propagation_results)
    
    results["rotation"] = compute_rotation(propagation_results)
    
    results["candidate_chains"] = auto_discover_chains(articles, full_chains)
    
    # 保存到文件（供Tab3上半部分引用）
    news_file = os.path.join(WORKSPACE, "news_cache.json")
    try:
        cache = _load_json(news_file, {})
        cache["last_scan"] = _time.strftime("%Y-%m-%d %H:%M")
        cache["results"] = results
        _save_json(news_file, cache)
    except:
        pass
    
    resp = {"status": "ok", "data": results}
    _cache_set(cache_key, resp, CACHE_TTL["news"])
    return jsonify(resp)

# ====== v8.3 历史快照数据库 (SQLite) ======
import sqlite3, datetime

SNAPSHOT_DB = os.path.join(WORKSPACE, "snapshots.db")

def _init_snapshot_db():
    """初始化快照数据库"""
    try:
        conn = sqlite3.connect(SNAPSHOT_DB)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                company TEXT NOT NULL,
                score REAL DEFAULT 0,
                direction TEXT DEFAULT '',
                rank INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                UNIQUE(date, chain_id, company)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(date)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_company ON snapshots(company)
        """)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[snapshot_db] init error: {e}")
        return False

def save_snapshot(date_str, chain_id, company, score, direction, rank=0, metadata=None):
    """保存单条快照"""
    try:
        conn = sqlite3.connect(SNAPSHOT_DB)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO snapshots (date, chain_id, company, score, direction, rank, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (date_str, chain_id, company, round(score, 4), direction, rank,
              json.dumps(metadata or {}, ensure_ascii=False)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[snapshot] save error: {e}")
        return False

def save_scan_snapshot(propagation_results):
    """将一次新闻扫描结果存入快照数据库"""
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    _init_snapshot_db()
    count = 0
    for cid, prop in propagation_results.items():
        agg = prop.get("aggregate_scores", {})
        for rank, (comp, score) in enumerate(sorted(agg.items(), key=lambda x: -abs(x[1])), 1):
            if rank > 30:  # 每链最多存30只
                break
            direction = "pos" if score > 0 else "neg" if score < 0 else "neu"
            meta = {
                "net_sentiment": prop.get("net_sentiment", ""),
                "event_count": len(prop.get("events", [])),
                "chain_net_score": prop.get("net_score", 0)
            }
            save_snapshot(now, cid, comp, score, direction, rank, meta)
            count += 1
    return count

def query_snapshots(company=None, chain_id=None, days=30, limit=200):
    """查询快照历史"""
    _init_snapshot_db()
    try:
        conn = sqlite3.connect(SNAPSHOT_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        where = []
        params = []
        if company:
            where.append("company = ?")
            params.append(company.upper())
        if chain_id:
            where.append("chain_id = ?")
            params.append(chain_id)
        # 日期范围
        since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        where.append("date >= ?")
        params.append(since)

        sql = f"SELECT * FROM snapshots WHERE {' AND '.join(where)} ORDER BY date DESC, rank ASC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        conn.close()

        # 按日期聚合
        results = []
        seen = set()
        for r in rows:
            key = (r["date"], r["company"], r["chain_id"])
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "date": r["date"],
                "company": r["company"],
                "chain_id": r["chain_id"],
                "score": r["score"],
                "direction": r["direction"],
                "rank": r["rank"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {}
            })
        return results
    except Exception as e:
        print(f"[snapshot] query error: {e}")
        return []

def get_company_trend(company, days=30):
    """获取单家公司的时间序列趋势"""
    data = query_snapshots(company=company, days=days, limit=100)
    trend = []
    for d in data:
        trend.append({
            "date": d["date"],
            "score": d["score"],
            "chain_id": d["chain_id"],
            "direction": d["direction"]
        })
    # 按日期升序
    trend.sort(key=lambda x: x["date"])
    return trend

def get_hot_topics_since(days=7):
    """获取近期最热主题（新闻频次+传播量综合）"""
    _init_snapshot_db()
    try:
        conn = sqlite3.connect(SNAPSHOT_DB)
        c = conn.cursor()
        since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        rows = c.execute("""
            SELECT company, chain_id, COUNT(*) as freq, AVG(ABS(score)) as avg_score
            FROM snapshots WHERE date >= ? AND ABS(score) > 0.1
            GROUP BY company ORDER BY freq * ABS(avg_score) DESC LIMIT 20
        """, (since,)).fetchall()
        conn.close()
        return [
            {"company": r[0], "chain_id": r[1], "frequency": r[2], "avg_intensity": round(abs(r[3]), 4)}
            for r in rows
        ]
    except Exception as e:
        print(f"[hot_topics] error: {e}")
        return []

# ====== v8.3 Layer 5: 排名引擎 (Ranking Layer) ======

def rank_companies(impact_scores, impact_matrix, logic_path, chain=None):
    """
    多因子排名引擎
    final_score = propagation × 0.40 + bottleneck × 0.20 + size × 0.10 + momentum × 0.15 + density × 0.15
    """
    if not impact_scores:
        return []

    # 因子1: 传播分 (propagation raw score)
    max_raw = max(abs(s) for s in impact_scores.values()) or 1
    prop_factor = {comp: abs(s) / max_raw for comp, s in impact_scores.items()}

    # 因子2: 瓶颈分 (bottleneck weight from matrix)
    bottleneck_factor = {}
    for comp in impact_scores:
        mx = impact_matrix.get(comp, {})
        bw_contrib = abs(mx.get("bottleneck", 0))
        bottleneck_factor[comp] = min(bw_contrib / 0.5, 1.0)  # normalized

    # 因子3: 市值规模代理 (用公司在链中的节点数推测关注度)
    size_factor = {}
    if chain:
        n_companies_per_node = {}
        for nid, n in chain.get("nodes", {}).items():
            for c in n.get("companies", []):
                n_companies_per_node[c] = n_companies_per_node.get(c, 0) + 1
        max_nodes = max(n_companies_per_node.values()) if n_companies_per_node else 1
        for comp in impact_scores:
            size_factor[comp] = n_companies_per_node.get(comp, 1) / max_nodes
    else:
        size_factor = {comp: 0.5 for comp in impact_scores}

    # 因子4: 动量分 (最近的snapshot中score变化率)
    momentum_factor = {}
    for comp in impact_scores:
        trend = get_company_trend(comp, days=7)
        if len(trend) >= 2:
            recent = trend[-1]["score"]
            older = trend[-2]["score"]
            change = (abs(recent) - abs(older)) / (abs(older) + 0.01)
            momentum_factor[comp] = min(max(change * 5 + 0.5, 0), 1.0)  # 范围0-1
        else:
            momentum_factor[comp] = 0.5  # 无历史→中性

    # 因子5: 新闻密度 (链内事件数)
    density_factor = {}
    event_count = len(logic_path.get("top_paths", [])) if logic_path else 1
    max_path_score = max((p.get("total_score", 0) for p in (logic_path.get("top_paths", []) or [])), default=1)
    for comp in impact_scores:
        comp_in_paths = sum(1 for p in (logic_path.get("top_paths", []) or [])
                           if comp in p.get("companies", []))
        density_factor[comp] = min(comp_in_paths / max(1, event_count) * 2, 1.0)

    # 加权汇总
    ranked = []
    for comp in impact_scores:
        raw = impact_scores[comp]
        final = (
            prop_factor.get(comp, 0) * 0.40 +
            bottleneck_factor.get(comp, 0) * 0.20 +
            size_factor.get(comp, 0) * 0.10 +
            momentum_factor.get(comp, 0) * 0.15 +
            density_factor.get(comp, 0) * 0.15
        )
        # 方向保持
        sign = 1 if raw > 0 else -1
        ranked.append((comp, round(final * 100, 1), round(raw, 4), {
            "propagation": round(prop_factor.get(comp, 0), 3),
            "bottleneck": round(bottleneck_factor.get(comp, 0), 3),
            "size": round(size_factor.get(comp, 0), 3),
            "momentum": round(momentum_factor.get(comp, 0), 3),
            "density": round(density_factor.get(comp, 0), 3)
        }))

    # 先按方向分组，再按分数排序
    pos = sorted([r for r in ranked if r[0] in impact_scores and impact_scores[r[0]] > 0], key=lambda x: -x[1])
    neg = sorted([r for r in ranked if r[0] in impact_scores and impact_scores[r[0]] < 0], key=lambda x: -x[1])

    return {
        "ranked": ranked,
        "top3": [r[0] for r in pos[:3]],
        "top5": [r[0] for r in pos[:5]],
        "top10": [r[0] for r in pos[:10]],
        "details": {comp: {"rank_score": sc, "propagation_score": raw_sc, "factors": factors}
                    for comp, sc, raw_sc, factors in ranked},
        "positive_sorted": [(r[0], r[1], r[2]) for r in pos],
        "negative_sorted": [(r[0], r[1], r[2]) for r in neg]
    }

# ====== v8.3 API: 快照查询 ======

@app.route("/api/snapshots")
def api_snapshots():
    """查询快照历史"""
    company = request.args.get("company", "")
    chain_id = request.args.get("chain_id", "")
    days = int(request.args.get("days", 30))
    
    if company:
        trend = get_company_trend(company, days=days)
        return jsonify({"status": "ok", "data": trend, "company": company})
    
    chain_data = query_snapshots(chain_id=chain_id, days=days, limit=500) if chain_id else []
    return jsonify({"status": "ok", "data": chain_data, "chain_id": chain_id or "all"})

@app.route("/api/snapshots/hot")
def api_hot_topics():
    """近期热点主题"""
    days = int(request.args.get("days", 7))
    hot = get_hot_topics_since(days=days)
    return jsonify({"status": "ok", "data": hot, "days": days})

@app.route("/api/snapshots/trend")
def api_trend():
    """多家公司批量趋势查询"""
    companies = request.args.get("companies", "").split(",")
    if not companies or companies == [""]:
        return jsonify({"status": "error", "msg": "请提供 companies 参数"})
    result = {}
    for comp in companies[:10]:
        result[comp.strip().upper()] = get_company_trend(comp.strip(), days=30)
    return jsonify({"status": "ok", "data": result})

# ====== v8.3 修改news_scan: 自动存档+排名 ======
# (news_scan_v83 覆盖原始 news_scan，添加排名+快照功能，并包含速率限制和缓存)

@app.route("/api/news/scan")
def news_scan_v83():
    """v8.3 全市场新闻扫描 + 传播推导 + 排名 + 快照"""
    # 速率限制 (v9.0+)
    if request.remote_addr not in LOCAL_IPS and not _rl_check(request.remote_addr):
        return jsonify({"status": "error", "msg": "请求过于频繁，请稍后再试"}), 429
    
    chain_id = request.args.get("chain_id", "")
    
    # 内存缓存 (v9.0+): 30分钟有效
    cache_key = f"news:{chain_id or 'all'}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)
    
    chains = _load_json(CHAINS_FILE, {}).get("chains", {})

    if chain_id:
        if chain_id not in chains:
            return jsonify({"status": "error", "msg": "产业链不存在"})
        target = {chain_id: chains[chain_id]}
    else:
        target = chains

    chain_keywords = {}
    for cid, chain in target.items():
        kws = list(set(chain.get("keywords", []) + [chain.get("name", "")]))
        chain_keywords[cid] = kws

    all_kws = list(set(kw for kws in chain_keywords.values() for kw in kws if kw))

    # 拉取新闻
    articles = _fetch_news(all_kws[:25])

    # 匹配分析
    results = _match_articles(articles, chain_keywords, target)

    # ===== 传播引擎 =====
    full_chains = load_chains_full()
    propagation_results = {}
    for art in articles[:20]:
        text = art.get("title", "") + " " + art.get("content", "")
        art_time = art.get("time", "")
        decay = time_decay(art_time)

        for cid, chain in full_chains.items():
            if not chain.get("nodes"):
                continue
            entities = entity_extract(text, chain)
            if not entities:
                continue
            dc = classify_direction(text)
            if dc["primary"] == "neutral":
                continue

            prop = propagate_full_multi(chain, entities, dc["directions"])
            impact = prop["impact_scores"]
            breakdown = prop["impact_matrix"]
            logic_path = prop["logic_path"]

            # v8.3: 排名引擎
            ranking = rank_companies(impact, breakdown, logic_path, chain)

            time_proj = {}
            for comp, score in list(impact.items())[:3]:
                time_proj[comp] = time_propagation(score)

            if cid not in propagation_results:
                propagation_results[cid] = {"events": [], "aggregate_scores": {}}

            positive = {k: v for k, v in impact.items() if v > 0}
            negative = {k: v for k, v in impact.items() if v < 0}

            propagation_results[cid]["events"].append({
                "article_title": art.get("title", "")[:80],
                "directions": dc["directions"],
                "direction_labels": dc.get("direction_labels", []),
                "direction_confidence": dc["confidence"],
                "direction_evidence": dc["evidence"],
                "entity_nodes": entities,
                "impact_scores": dict(list(impact.items())[:5]),
                "positive_scores": dict(list(positive.items())[:5]),
                "negative_scores": dict(list(negative.items())[:5]),
                "impact_matrix": breakdown,
                "time_projection": time_proj,
                "logic_path": logic_path,
                "ranking": {  # v8.3 排名结果
                    "top3": ranking["top3"],
                    "top5": ranking["top5"],
                    "top10": ranking["top10"],
                    "details": {k: v for k, v in list(ranking["details"].items())[:10]}
                }
            })
            for comp, score in impact.items():
                propagation_results[cid]["aggregate_scores"][comp] = round(
                    propagation_results[cid]["aggregate_scores"].get(comp, 0) + score * decay, 4
                )

    # 聚合后计算正负和净倾向
    for cid in propagation_results:
        agg = propagation_results[cid]["aggregate_scores"]
        propagation_results[cid]["aggregate_scores"] = dict(sorted(agg.items(), key=lambda x: -abs(x[1])))
        propagation_results[cid]["positive"] = dict(sorted(
            {k: v for k, v in agg.items() if v > 0}.items(), key=lambda x: -x[1]))
        propagation_results[cid]["negative"] = dict(sorted(
            {k: v for k, v in agg.items() if v < 0}.items(), key=lambda x: x[1]))
        net = sum(agg.values())
        propagation_results[cid]["net_sentiment"] = "bullish" if net > 0 else "bearish" if net < 0 else "neutral"
        propagation_results[cid]["net_score"] = round(net, 4)

        # v8.3: 对聚合结果也运行排名
        chain_obj = full_chains.get(cid)
        if chain_obj and agg:
            # 简化排名（无logic_path）
            fake_logic = {"top_paths": []}
            ranked = rank_companies(agg, {}, fake_logic, chain_obj)
            propagation_results[cid]["ranking"] = {
                "top3": ranked["top3"],
                "top5": ranked["top5"],
                "top10": ranked["top10"],
                "positive_sorted": ranked["positive_sorted"][:5],
                "negative_sorted": ranked["negative_sorted"][:5]
            }

    results["propagation"] = propagation_results

    # ===== v8.3: 保存快照 =====
    try:
        saved = save_scan_snapshot(propagation_results)
        results["snapshots_saved"] = saved
    except Exception as e:
        print(f"[snapshot] save error: {e}")
        results["snapshots_saved"] = 0

    # ===== 高级功能 =====
    update_chain_heat(propagation_results)
    results["resonance"] = detect_resonance(propagation_results)
    results["rotation"] = compute_rotation(propagation_results)
    results["candidate_chains"] = auto_discover_chains(articles, full_chains)

    # 保存缓存
    news_file = os.path.join(WORKSPACE, "news_cache.json")
    try:
        cache = _load_json(news_file, {})
        cache["last_scan"] = _time.strftime("%Y-%m-%d %H:%M")
        cache["results"] = results
        _save_json(news_file, cache)
    except:
        pass
    
    resp = {"status": "ok", "data": results}
    _cache_set(cache_key, resp, CACHE_TTL["news"])
    return jsonify(resp)

# ====== v8.3 链评分拆分: Importance vs Opportunity ======
# 修改 /api/chains 路由
_original_get_chains = get_chains_legacy

@app.route("/api/chains")
def get_chains_v83():
    """v8.3 返回所有产业链（Importance + Opportunity 双维度评分）"""
    chains = _load_json(CHAINS_FILE, {})
    heat_data = _load_json(os.path.join(WORKSPACE, "chain_heat.json"), {})
    heat_chains = heat_data.get("chains", {})
    
    # 读取快照统计
    _init_snapshot_db()
    snapshot_stats = {}
    try:
        conn = sqlite3.connect(SNAPSHOT_DB)
        c = conn.cursor()
        since_7d = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        rows = c.execute("""
            SELECT chain_id, COUNT(DISTINCT company) as comp_count, 
                   AVG(ABS(score)) as avg_impact, MAX(ABS(score)) as max_impact
            FROM snapshots WHERE date >= ? 
            GROUP BY chain_id
        """, (since_7d,)).fetchall()
        conn.close()
        for r in rows:
            snapshot_stats[r[0]] = {"comp_count": r[1], "avg_impact": abs(r[2]) if r[2] else 0, "max_impact": abs(r[3]) if r[3] else 0}
    except:
        pass

    summary = {}

    for cid, c in chains.get("chains", {}).items():
        node_count = len(c.get("nodes", {}))
        total_companies = set()
        edge_count = 0
        bottlenecks = 0
        for nid, n in c.get("nodes", {}).items():
            for co in n.get("companies", []):
                total_companies.add(co)
            if n.get("bottleneck"):
                bottlenecks += 1
            edge_count += len(n.get("upstream", [])) + len(n.get("downstream", []))

        # v8.3: Importance Score (静态结构分)
        importance_score = min(100, int(
            node_count * 4 + len(total_companies) * 3 + edge_count * 1.5 + bottlenecks * 8
        ))

        # v8.3: Opportunity Score (动态机会分)
        hc = heat_chains.get(cid, {})
        recent_counts = [d["count"] for d in hc.get("daily_counts", [])[-7:]]
        total_news_7d = sum(recent_counts) if recent_counts else 0
        
        net_scores = [d["net"] for d in hc.get("net_scores", [])[-7:] if d.get("net") is not None]
        avg_net = sum(net_scores) / len(net_scores) if net_scores else 0
        
        # 快照因子
        snap = snapshot_stats.get(cid, {})
        snap_comp_count = snap.get("comp_count", 0)
        snap_avg_impact = snap.get("avg_impact", 0)

        opp_news = min(30, total_news_7d * 3)
        opp_propagation = min(30, snap_avg_impact * 10)
        opp_momentum = min(25, max(0, (avg_net + 5) * 3))
        opp_coverage = min(15, snap_comp_count * 3)
        opportunity_score = round(opp_news + opp_propagation + opp_momentum + opp_coverage, 1)

        trend = "rising" if avg_net > 1 else "falling" if avg_net < -1 else "stable"

        summary[cid] = {
            "name": c.get("name", cid),
            "market": c.get("market", "US"),
            "timeframe": c.get("timeframe", "? "),
            "keywords": c.get("keywords", [])[:5],
            "node_count": node_count,
            "company_count": len(total_companies),
            "bottleneck_nodes": bottlenecks,
            "edge_count": edge_count,
            # v8.3 双维度
            "importance_score": importance_score,
            "opportunity_score": opportunity_score,
            "chain_score": round((importance_score + opportunity_score) / 2, 1),
            "trend": trend,
            "recent_news_7d": total_news_7d
        }

    sorted_summary = dict(sorted(summary.items(), key=lambda x: -x[1]["chain_score"]))
    return jsonify({"status": "ok", "data": sorted_summary})


# ====== 初始化快照数据库 ======
_init_snapshot_db()

if __name__ == "__main__":
    _port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=_port, debug=False, threaded=True)
