"""
历史波动率(HV)计算模块

HV_N = std(ln(close_t/close_{t-1})) × √252
"""

import math


def calc_hv(klines: list, window: int = 20) -> float:
    """
    计算历史波动率
    klines: [{day, open, high, low, close, volume}, ...]
    window: 计算窗口 (20或60)
    返回: 年化波动率 (如0.15表示15%)
    """
    if len(klines) < window + 1:
        return None

    # 取最近window+1条 (需要window个收益率)
    recent = klines[-(window + 1):]

    # 计算日对数收益率
    returns = []
    for i in range(1, len(recent)):
        c0 = recent[i - 1]["close"]
        c1 = recent[i]["close"]
        if c0 > 0 and c1 > 0:
            returns.append(math.log(c1 / c0))

    if len(returns) < window:
        return None

    # 标准差 × √252
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    hv = math.sqrt(variance) * math.sqrt(252)

    return round(hv, 4)


def calc_all_hv(klines_map: dict) -> dict:
    """
    计算所有品种的历史波动率
    klines_map: {"510050": [kline_list], ...}
    返回: {"510050": {"hv20": 0.15, "hv60": 0.18, "price_range_20d": (3.01, 3.15)}, ...}
    注: 市场标准用HV60, 报告主要展示HV60
    """
    result = {}
    for code, klines in klines_map.items():
        if not klines:
            result[code] = {"hv20": None, "hv60": None, "price_range_20d": None, "price_range_60d": None, "position_60d": None}
            continue

        hv20 = calc_hv(klines, window=20)
        hv60 = calc_hv(klines, window=60) if len(klines) >= 61 else None

        # 近20日价格区间
        recent_20 = klines[-20:] if len(klines) >= 20 else klines
        lows_20 = [k["low"] for k in recent_20]
        highs_20 = [k["high"] for k in recent_20]
        range_20 = (round(min(lows_20), 4), round(max(highs_20), 4))

        # 近60日价格区间
        recent_60 = klines[-60:] if len(klines) >= 60 else klines
        lows_60 = [k["low"] for k in recent_60]
        highs_60 = [k["high"] for k in recent_60]
        range_60 = (round(min(lows_60), 4), round(max(highs_60), 4))

        # 60日价格位置 (0=最低点, 1=最高点)
        latest_close = klines[-1]["close"]
        low_60 = min(lows_60)
        high_60 = max(highs_60)
        position_60d = None
        if high_60 > low_60:
            position_60d = round((latest_close - low_60) / (high_60 - low_60), 2)

        result[code] = {
            "hv20": hv20,
            "hv60": hv60,
            "price_range_20d": range_20,
            "price_range_60d": range_60,
            "position_60d": position_60d,
            "latest_close": latest_close,
            "latest_date": klines[-1]["day"],
        }

        pos_str = f"{position_60d*100:.0f}%" if position_60d is not None else "N/A"
        print(f"  HV {code}: HV20={hv20}, HV60={hv60}, 20日区间={range_20}, 位置={pos_str}")

    return result
