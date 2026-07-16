"""
新浪ETF期权数据采集器

数据源: hq.sinajs.cn
- 合约列表: OP_UP_{underlying}{month} / OP_DOWN_{underlying}{month}
- 合约行情: CON_OP_XXXXX (51字段, GBK编码)
"""

import requests
import re
import time
from datetime import datetime, date

HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

# 品种配置
UNDERLYINGS = {
    "510050": {"name": "50ETF", "exchange": "sh", "multiplier": 10000},
    "510300": {"name": "300ETF(沪)", "exchange": "sh", "multiplier": 10000},
    "159919": {"name": "300ETF(深)", "exchange": "sz", "multiplier": 10000},
    "510500": {"name": "500ETF", "exchange": "sh", "multiplier": 10000},
    "159915": {"name": "创业板ETF", "exchange": "sz", "multiplier": 10000},
    "588000": {"name": "科创50ETF", "exchange": "sh", "multiplier": 10000},
}

# 可用月份 (需要动态检测)
MONTH_CANDIDATES = ["2607", "2608", "2609", "2610", "2612", "2703", "2706"]


def get_etf_price(underlying: str) -> float:
    """获取ETF实时价格"""
    info = UNDERLYINGS.get(underlying)
    if not info:
        return 0.0
    symbol = f"{info['exchange']}{underlying}"
    url = f"https://hq.sinajs.cn/list={symbol}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "gbk"
        parts = r.text.split('"')[1].split(",")
        return float(parts[3])  # 当前价
    except Exception as e:
        print(f"  [WARN] ETF价格获取失败 {underlying}: {e}")
        return 0.0


def get_contract_codes(underlying: str, month: str) -> dict:
    """
    获取某品种某月份的全部合约代码
    返回: {"call": [code1, code2, ...], "put": [code1, code2, ...]}
    """
    result = {"call": [], "put": []}

    # 认购
    url = f"https://hq.sinajs.cn/list=OP_UP_{underlying}{month}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        r.encoding = "gbk"
        if '"' in r.text:
            codes = [c for c in r.text.split('"')[1].split(",") if c]
            result["call"] = codes
    except:
        pass

    # 认沽
    url = f"https://hq.sinajs.cn/list=OP_DOWN_{underlying}{month}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        r.encoding = "gbk"
        if '"' in r.text:
            codes = [c for c in r.text.split('"')[1].split(",") if c]
            result["put"] = codes
    except:
        pass

    return result


def parse_option_fields(fields: list) -> dict:
    """
    解析新浪期权51字段为结构化数据
    字段映射见DESIGN.md
    """
    if len(fields) < 47:
        return None

    try:
        return {
            "last_price": float(fields[1]) if fields[1] else 0,
            "bid": float(fields[2]) if fields[2] else 0,
            "ask": float(fields[3]) if fields[3] else 0,
            "open_interest": int(fields[5]) if fields[5] else 0,
            "change_pct": float(fields[6]) if fields[6] else 0,
            "strike": float(fields[7]) if fields[7] else 0,
            "iv": float(fields[10]) if fields[10] else 0,
            "risk_free_rate": float(fields[11]) if fields[11] else 0.02,
            "theoretical_price": float(fields[12]) if fields[12] else 0,
            "timestamp": fields[32],
            "underlying": fields[36],
            "name": fields[37],
            "amplitude": float(fields[38]) if fields[38] else 0,
            "open": float(fields[39]) if fields[39] else 0,
            "prev_close": float(fields[40]) if fields[40] else 0,
            "volume": int(fields[41]) if fields[41] else 0,
            "amount": float(fields[42]) if fields[42] else 0,
            "settlement": float(fields[44]) if fields[44] and fields[44] != "0" else None,
            "option_type": fields[45],  # C or P
            "expiry_date": fields[46],
            "days_to_expiry": int(fields[47]) if fields[47] else 0,
            "multiplier": 10000,  # 中国ETF期权合约乘数固定10000份
        }
    except (ValueError, IndexError) as e:
        return None


def get_option_quotes(codes: list, batch_size: int = 50) -> list:
    """
    批量获取期权合约行情
    codes: CON_OP_XXXXX 格式的合约代码列表
    返回: 解析后的dict列表
    """
    results = []

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        codes_str = ",".join(batch)
        url = f"https://hq.sinajs.cn/list={codes_str}"

        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.encoding = "gbk"

            for line in r.text.strip().split("\n"):
                if '"' not in line:
                    continue
                # 提取合约代码
                code_match = re.search(r'list=([A-Z_0-9]+)', line)
                code = code_match.group(1) if code_match else "unknown"

                data = line.split('"')[1]
                fields = data.split(",")
                parsed = parse_option_fields(fields)
                if parsed:
                    parsed["code"] = code
                    results.append(parsed)
        except Exception as e:
            print(f"  [WARN] 行情获取失败 batch {i//batch_size+1}: {e}")

        # 批次间隔
        if i + batch_size < len(codes):
            time.sleep(0.1)

    return results


def discover_months(underlying: str) -> list:
    """
    检测某品种当前有哪些合约月份
    返回: 有合约的月份列表 (如 ["2607", "2608", "2609", "2612"])
    """
    months = []
    for month in MONTH_CANDIDATES:
        codes = get_contract_codes(underlying, month)
        if codes["call"] or codes["put"]:
            months.append(month)
        time.sleep(0.05)
    return months


def fetch_all_options(underlying: str = None, month: str = None) -> dict:
    """
    采集全量期权数据
    返回: {
        "underlyings": {code: {"name": ..., "price": ..., "months": [...]}},
        "contracts": [contract_dict, ...],
        "fetch_time": "2026-07-09 15:00:00"
    }
    """
    result = {
        "underlyings": {},
        "contracts": [],
        "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 确定要采集的品种
    if underlying:
        targets = {underlying: UNDERLYINGS[underlying]}
    else:
        targets = UNDERLYINGS

    for code, info in targets.items():
        print(f"  采集 {info['name']}({code})...")
        etf_price = get_etf_price(code)
        if etf_price == 0:
            print(f"    ⚠ 跳过: 无法获取ETF价格")
            continue

        # 发现可用月份
        if month:
            months = [month]
        else:
            months = discover_months(code)

        result["underlyings"][code] = {
            "name": info["name"],
            "exchange": info["exchange"],
            "price": etf_price,
            "multiplier": info["multiplier"],
            "months": months,
        }

        # 采集每个合约月份的数据
        for m in months:
            print(f"    月份 {m}...", end=" ")
            codes = get_contract_codes(code, m)
            all_codes = codes["call"] + codes["put"]
            if not all_codes:
                print("无合约")
                continue

            quotes = get_option_quotes(all_codes)
            for q in quotes:
                q["underlying_code"] = code
                q["underlying_name"] = info["name"]
                q["underlying_price"] = etf_price
                q["month"] = m
            result["contracts"].extend(quotes)
            print(f"{len(quotes)}个合约")

            time.sleep(0.1)

    return result
