"""
akshare Greeks数据采集器

数据源: option_risk_indicator_sse (上交所期权风险指标, T-1日)
需要HTTPS_PROXY走代理
"""

import os
from datetime import datetime, timedelta


def fetch_greeks(date_str: str = None) -> dict:
    """
    获取期权Greeks数据
    date_str: YYYYMMDD格式, 默认取昨天(T-1)
    返回: {contract_symbol: {delta, theta, gamma, vega, rho, iv}, ...}
    """
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:10809")

    import akshare as ak

    if date_str is None:
        # T-1: 取昨天(跳过周末)
        today = datetime.now()
        if today.weekday() == 0:  # 周一
            target = today - timedelta(days=3)
        elif today.weekday() == 6:  # 周日
            target = today - timedelta(days=2)
        else:
            target = today - timedelta(days=1)
        date_str = target.strftime("%Y%m%d")

    try:
        df = ak.option_risk_indicator_sse(date=date_str)
        print(f"  Greeks: {len(df)}条 (日期: {date_str})")

        result = {}
        for _, row in df.iterrows():
            symbol = row['CONTRACT_SYMBOL']
            result[symbol] = {
                "delta": float(row['DELTA_VALUE']) if row['DELTA_VALUE'] == row['DELTA_VALUE'] else None,
                "theta": float(row['THETA_VALUE']) if row['THETA_VALUE'] == row['THETA_VALUE'] else None,
                "gamma": float(row['GAMMA_VALUE']) if row['GAMMA_VALUE'] == row['GAMMA_VALUE'] else None,
                "vega": float(row['VEGA_VALUE']) if row['VEGA_VALUE'] == row['VEGA_VALUE'] else None,
                "rho": float(row['RHO_VALUE']) if row['RHO_VALUE'] == row['RHO_VALUE'] else None,
                "iv_akshare": float(row['IMPLC_VOLATLTY']) if row['IMPLC_VOLATLTY'] and row['IMPLC_VOLATLTY'] == row['IMPLC_VOLATLTY'] else None,
                "contract_id": row.get('CONTRACT_ID', ''),
            }
        return result
    except Exception as e:
        print(f"  [WARN] Greeks获取失败: {e}")
        return {}


def match_greeks_to_contracts(contracts: list, greeks: dict) -> list:
    """
    将Greeks数据匹配到期权合约列表
    匹配方式: 用合约名称匹配 (如 "50ETF购7月2700")
    """
    matched = 0
    for c in contracts:
        name = c.get("name", "")
        if name in greeks:
            g = greeks[name]
            c["delta"] = g["delta"]
            c["theta"] = g["theta"]
            c["gamma"] = g["gamma"]
            c["vega"] = g["vega"]
            c["rho"] = g["rho"]
            c["iv_akshare"] = g["iv_akshare"]
            matched += 1
        else:
            c["delta"] = None
            c["theta"] = None
            c["gamma"] = None
            c["vega"] = None
            c["rho"] = None
            c["iv_akshare"] = None

    print(f"  Greeks匹配: {matched}/{len(contracts)}")
    return contracts
