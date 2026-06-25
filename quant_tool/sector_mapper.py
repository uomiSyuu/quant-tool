# -*- coding: utf-8 -*-
"""
行业分类映射器 v1.0
====================
基于东方财富API的申万二级行业分类，替代关键词模糊匹配。

数据流:
  EM API (push2.eastmoney.com) → SQLite本地缓存 → code查行业

优势:
  - 按股票代码直接查询，不用中文名猜行业
  - 申万二级行业(f100)为官方分类，稳定可靠
  - 本地SQLite缓存，7天更新一次
  - 美股/港股使用GICS行业（GICS→内部分类硬编码）
"""

import sqlite3, os, time, json
import requests

DB_PATH = os.path.join(os.path.dirname(__file__), "stock_sector.db")
CACHE_TTL = 7 * 86400  # 7天更新一次

# 东方财富A股全市场列表接口
EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 申万二级行业(f100) → 内部分类映射
# 这是官方行业名到分类引擎key的确定性映射（不是关键词匹配）
SW_L2_MAP = {
    # 半导体/电子
    "半导体": "semicon", "集成电路": "semicon", "芯片设计": "semicon",
    "电子元器件": "semicon", "光学光电子": "semicon", "LED": "semicon",
    "消费电子": "tech_hardware", "计算机设备": "tech_hardware",
    "其他电子Ⅱ": "semicon", "电子制造Ⅱ": "semicon",
    # 软件/互联网
    "软件开发": "saas", "IT服务Ⅱ": "saas", "计算机应用": "saas",
    "互联网": "internet", "互联网服务": "internet",
    "通信服务": "internet", "通信设备": "internet",
    "广告传媒": "internet", "影视院线": "internet", "游戏Ⅱ": "internet",
    "电视广播": "internet", "数字媒体": "internet",
    # 金融
    "银行Ⅱ": "finance", "国有大型银行Ⅱ": "finance",
    "股份制银行Ⅱ": "finance", "城商行Ⅱ": "finance",
    "证券Ⅱ": "finance", "保险Ⅱ": "finance",
    "多元金融": "finance", "金融信息服务": "finance",
    # 医疗健康
    "医疗器械": "health", "医疗服务": "health",
    "化学制药": "health", "中药Ⅱ": "health",
    "生物制品": "health", "医药商业": "health",
    "医药生物": "health", "医疗": "health",
    # 消费(食品/饮料/家电/纺织)
    "白酒Ⅱ": "consumer", "食品加工": "consumer",
    "调味发酵品Ⅱ": "consumer", "饮料乳品": "consumer",
    "休闲食品": "consumer", "烘焙食品": "consumer",
    "白色家电": "consumer", "黑色家电": "consumer",
    "厨房电器Ⅱ": "consumer", "小家电": "consumer",
    "纺织制造": "consumer", "服装家纺": "consumer",
    "饰品Ⅱ": "consumer", "个护用品": "consumer",
    "日用化学": "consumer", "化妆品": "consumer",
    # 零售/贸易
    "一般零售": "retail", "专业连锁Ⅱ": "retail",
    "商业贸易": "retail", "跨境电商": "retail",
    # 汽车
    "乘用车": "auto", "商用车": "auto",
    "汽车零部件": "auto", "汽车服务": "auto",
    "摩托车及其他": "auto",
    # 能源/资源
    "煤炭开采": "energy", "焦炭Ⅱ": "energy",
    "石油开采": "energy", "炼化及贸易": "energy",
    "油田服务": "energy", "油气开采Ⅱ": "energy",
    "油服工程": "energy",
    "化学原料": "energy", "化学制品": "energy",
    "化学纤维": "energy", "橡胶": "energy",
    "塑料": "energy", "农化制品": "energy",
    "能源": "energy", "化工": "energy",
    # 工业/制造/建设
    "工程机械": "industrial", "通用设备": "industrial",
    "专用设备": "industrial", "自动化设备": "industrial",
    "机器人": "industrial", "机床工具": "industrial",
    "仪器仪表Ⅱ": "industrial",
    "电网设备": "industrial", "输变电设备": "industrial",
    "光伏设备": "industrial", "风电设备": "industrial",
    "电池": "industrial", "能源金属": "industrial",
    "轨交设备Ⅱ": "industrial", "航海装备Ⅱ": "industrial",
    "航空装备Ⅱ": "industrial", "军工装备Ⅱ": "industrial",
    "地面兵装Ⅱ": "industrial",
    "房屋建设Ⅱ": "industrial", "基础建设": "industrial",
    "专业工程": "industrial", "工程咨询服务Ⅱ": "industrial",
    "水泥": "industrial", "玻璃玻纤": "industrial",
    "装修建材": "industrial", "建筑装饰": "industrial",
    "环保Ⅱ": "industrial", "环境治理": "industrial",
    # 材料
    "有色金属": "materials", "工业金属": "materials",
    "贵金属": "materials", "稀有金属": "materials",
    "钢铁": "materials", "冶钢原料": "materials",
    "金属新材料": "materials", "非金属材料Ⅱ": "materials",
    "建筑材料": "materials",
    # 公用事业
    "电力": "utility", "热力服务": "utility",
    "燃气Ⅱ": "utility", "水务": "utility",
    "公用事业": "utility",
    # 房地产
    "房地产开发": "real_estate", "房地产服务": "real_estate",
    "商业地产": "real_estate",
    # 交通运输
    "铁路运输": "transport", "公路运输": "transport",
    "航空运输": "transport", "航运": "transport",
    "港口": "transport", "机场": "transport",
    "物流": "transport", "快递": "transport",
    "仓储物流": "transport",
    # 其他
    "农林牧渔": "agriculture", "养殖业": "agriculture",
    "种植业": "agriculture", "渔业": "agriculture",
    "饲料": "agriculture",
    "综合Ⅱ": "general", "综合": "general",
}

# GICS行业(美股) → 内部分类
GICS_MAP = {
    "Technology": "semicon", "Information Technology": "semicon",
    "Semiconductors": "semicon", "Software": "saas",
    "Internet": "internet", "Media": "internet",
    "Entertainment": "internet", "Telecom": "internet",
    "Financial": "finance", "Banks": "finance",
    "Insurance": "finance", "Capital Markets": "finance",
    "Health Care": "health", "Pharmaceuticals": "health",
    "Biotechnology": "health", "Medical Devices": "health",
    "Consumer Discretionary": "consumer", "Consumer": "consumer",
    "Consumer Staples": "consumer", "Food": "consumer",
    "Beverages": "consumer", "Retail": "retail",
    "Automobiles": "auto", "Auto": "auto",
    "Energy": "energy", "Oil & Gas": "energy",
    "Materials": "materials", "Metals & Mining": "materials",
    "Chemicals": "energy", "Industrials": "industrial",
    "Manufacturing": "industrial", "Capital Goods": "industrial",
    "Utilities": "utility", "Electric": "utility",
    "Real Estate": "real_estate", "REITs": "real_estate",
    "Transportation": "transport", "Logistics": "transport",
    "Agriculture": "agriculture",
}

# ====== SQLite 建表 ======

def _get_conn():
    """获取SQLite连接（线程安全）"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    """初始化数据库表"""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_sector (
                code        TEXT PRIMARY KEY,
                name        TEXT,
                industry_l2 TEXT,       -- 申万二级行业(f100)
                update_date TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _needs_update():
    """检查是否需要更新缓存（7天有效）"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT MAX(update_date) FROM stock_sector").fetchone()
        if not row or not row[0]:
            return True
        from datetime import datetime
        last = datetime.strptime(row[0], "%Y-%m-%d")
        return (datetime.now() - last).days >= 7
    except:
        return True
    finally:
        conn.close()


# ====== 从EM API拉取全市场数据 ======

def _fetch_all_a_share_industries():
    """
    从东方财富API拉取A股全市场股票→申万二级行业映射
    返回: {code: {name, industry_l2}}
    """
    import time as _t
    result = {}
    # 分页拉取（EM API pz=100实际返回100条，全市场约5000只）
    # 注意：排序按f3(涨跌幅)，热门股在前，前20页覆盖大部分主流股票
    for page in range(1, 45):
        if page > 1:
            _t.sleep(0.3)  # 限速，避免被禁
        params = {
            "pn": page, "pz": 200, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14,f100"
        }
        try:
            r = requests.get(EM_CLIST_URL, params=params, headers=EM_HEADERS, timeout=20)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            if not items:
                break
            before = len(result)
            for item in items:
                code = str(item.get("f12", ""))
                name = str(item.get("f14", ""))
                industry = str(item.get("f100", ""))
                if code and industry and industry not in ("-", "") and code not in result:
                    result[code] = {"name": name, "industry_l2": industry}
            new = len(result) - before
            if new == 0:
                # 连续2页无新数据则停止
                break
        except Exception as e:
            if page <= 2:
                print(f"[sector_mapper] 第{page}页: {e}")
            continue
    print(f"[sector_mapper] 拉取 {len(result)} 条行业数据")
    return result


def update_cache():
    """更新本地缓存"""
    _init_db()
    if not _needs_update():
        print("[sector_mapper] 缓存未过期，跳过更新")
        return
    
    data = _fetch_all_a_share_industries()
    if not data:
        print("[sector_mapper] 无数据，跳过")
        return
    
    conn = _get_conn()
    try:
        today = time.strftime("%Y-%m-%d")
        for code, info in data.items():
            need_update = conn.execute(
                "SELECT 1 FROM stock_sector WHERE code=? AND update_date=?",
                (code, today)
            ).fetchone() is None
            if need_update:
                conn.execute(
                    "INSERT OR REPLACE INTO stock_sector (code, name, industry_l2, update_date) VALUES (?, ?, ?, ?)",
                    (code, info["name"], info["industry_l2"], today)
                )
        conn.commit()
        print(f"[sector_mapper] 缓存更新完成: {len(data)} 条")
    finally:
        conn.close()


# ====== 查询接口 ======

def get_stock_industry(code):
    """
    按股票代码查询行业信息
    返回: {industry_l2 (申万二级), sector (内部分类key)} 或 None
    
    Args:
        code: A股6位代码 (如 "600519", "000001") 或带前缀格式
    """
    # 标准化代码: 去除前缀
    raw = code.strip().upper()
    for prefix in ["SH", "SZ", "BJ", "SH.", "SZ.", "BJ."]:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    
    # 确保是6位数字
    import re
    digits = re.sub(r'\D', '', raw)
    if len(digits) != 6:
        return None
    
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT name, industry_l2 FROM stock_sector WHERE code=?",
            (digits,)
        ).fetchone()
        if row:
            ind_l2 = row[1]
            internal = SW_L2_MAP.get(ind_l2, "general")
            return {
                "name": row[0],
                "industry_l2": ind_l2,
                "cn_sector": ind_l2,        # 显示用
                "sector": internal,          # 分类引擎用
            }
        return None
    finally:
        conn.close()


def get_sector_by_industry(industry_name):
    """
    通过行业名查询内部分类（支持申万二级或EM INDUSTRY_NAME）
    """
    if not industry_name:
        return "general"
    
    # 精确匹配
    if industry_name in SW_L2_MAP:
        return SW_L2_MAP[industry_name]
    
    # 模糊匹配（仅限确实无法精确匹配时）
    for key, val in SW_L2_MAP.items():
        if key in industry_name or industry_name in key:
            return val
    
    return "general"


def get_gics_sector(gics_name):
    """美股GICS行业映射"""
    if not gics_name:
        return "general"
    return GICS_MAP.get(gics_name, "general")


# ====== 自测 ======
if __name__ == "__main__":
    print("=" * 50)
    print("行业分类映射器自测")
    print("=" * 50)
    
    update_cache()
    
    for code in ["600519", "000001", "300750", "600037", "601088", "600900"]:
        info = get_stock_industry(code)
        if info:
            print(f"  {code} {info['name']}: {info['industry_l2']} → {info['sector']}")
        else:
            print(f"  {code}: 未找到")
    
    print("\n缓存条数:", end=" ")
    conn = _get_conn()
    try:
        print(conn.execute("SELECT COUNT(*) FROM stock_sector").fetchone()[0])
    finally:
        conn.close()
