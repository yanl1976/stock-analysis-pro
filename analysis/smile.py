"""
波动率微笑分析模块

同一到期日，IV随行权价的分布:
- 虚值Put端: IV偏高 (恐慌溢价)
- 平值附近: IV最低
- 虚值Call端: IV偏高

检测:
- 偏离微笑曲线的异常合约 → 定价异常 → 套利机会
- 整体曲线上移/下移 → IV环境变化
"""


def build_smile(contracts: list, underlying_price: float) -> dict:
    """
    构建波动率微笑数据
    
    contracts: 同一品种同一月份的合约列表
    underlying_price: ETF现价
    
    返回: {
        "calls": [{"strike": 2.7, "iv": 0.35, "name": "...", "amount": ...}, ...],
        "puts": [{"strike": 2.7, "iv": 0.25, "name": "...", "amount": ...}, ...],
        "atm_strike": 3.0,
        "atm_iv_call": 0.18,
        "atm_iv_put": 0.20,
        "skew": {"left": iv_diff, "right": iv_diff},
        "anomalies": [异常合约列表],
    }
    """
    calls = []
    puts = []

    for c in contracts:
        iv = _get_iv(c)
        if iv <= 0:
            continue
        entry = {
            "strike": c["strike"],
            "iv": iv,
            "name": c["name"],
            "amount": c.get("amount", 0),
            "last_price": c["last_price"],
            "volume": c.get("volume", 0),
        }
        if c["option_type"] == "C":
            calls.append(entry)
        else:
            puts.append(entry)

    # 按行权价排序
    calls.sort(key=lambda x: x["strike"])
    puts.sort(key=lambda x: x["strike"])

    # 找平值行权价 (最接近ETF现价的)
    if calls:
        atm_strike = min(calls, key=lambda x: abs(x["strike"] - underlying_price))["strike"]
    elif puts:
        atm_strike = min(puts, key=lambda x: abs(x["strike"] - underlying_price))["strike"]
    else:
        atm_strike = underlying_price

    # 平值IV
    atm_iv_call = None
    atm_iv_put = None
    for c in calls:
        if abs(c["strike"] - atm_strike) < 0.001:
            atm_iv_call = c["iv"]
            break
    for p in puts:
        if abs(p["strike"] - atm_strike) < 0.001:
            atm_iv_put = p["iv"]
            break

    # 偏度分析: 虚值端 vs 平值的IV差
    skew = {"left": 0, "right": 0}
    atm_iv = atm_iv_put or atm_iv_call or 0

    if atm_iv > 0 and puts:
        # 左偏: 最虚值的Put (行权价最低)
        otm_put = min(puts, key=lambda x: x["strike"])
        skew["left"] = round(otm_put["iv"] - atm_iv, 4)

    if atm_iv > 0 and calls:
        # 右偏: 最虚值的Call (行权价最高)
        otm_call = max(calls, key=lambda x: x["strike"])
        skew["right"] = round(otm_call["iv"] - atm_iv, 4)

    # 异常检测: IV偏离相邻合约均值超过30%
    anomalies = []
    for lst in [calls, puts]:
        for i, item in enumerate(lst):
            neighbors = []
            if i > 0:
                neighbors.append(lst[i-1]["iv"])
            if i < len(lst) - 1:
                neighbors.append(lst[i+1]["iv"])
            if neighbors:
                avg_neighbor = sum(neighbors) / len(neighbors)
                if avg_neighbor > 0:
                    deviation = abs(item["iv"] - avg_neighbor) / avg_neighbor
                    if deviation > 0.3 and item["amount"] > 100000:
                        anomalies.append({
                            "name": item["name"],
                            "strike": item["strike"],
                            "iv": item["iv"],
                            "expected_iv": round(avg_neighbor, 4),
                            "deviation": round(deviation, 2),
                            "type": "高估" if item["iv"] > avg_neighbor else "低估",
                        })

    # ===== 预计算SVG坐标 (避免模板中复杂数学) =====
    svg_data = _build_svg(calls, puts, atm_strike)

    return {
        "calls": calls,
        "puts": puts,
        "atm_strike": atm_strike,
        "atm_iv_call": atm_iv_call,
        "atm_iv_put": atm_iv_put,
        "atm_iv": atm_iv,
        "skew": skew,
        "anomalies": anomalies,
        "svg": svg_data,
    }


def _get_iv(contract: dict) -> float:
    """获取IV：优先akshare(交易所官方)，其次新浪"""
    iv_ak = contract.get("iv_akshare", 0)
    if iv_ak and iv_ak > 0:
        return float(iv_ak)
    return float(contract.get("iv", 0))


def _build_svg(calls: list, puts: list, atm_strike: float) -> dict:
    """
    预计算SVG坐标，返回模板可直接渲染的数据
    SVG画布: 宽480, 高200, 绘图区 x=[50,460], y=[25,165]
    
    合并Call+Put在同一行权价的IV，形成完整微笑曲线
    """
    # IV来源：优先akshare(交易所官方数据)，其次新浪(可能有偏差)
    # akshare在深度实值合约返回IV=0，此时fallback到新浪
    
    # 构建微笑曲线：低行权价用Put IV，高行权价用Call IV
    # 原理：put在低strike更活跃，call在高strike更活跃
    # 这样形成: 左翼(put IV高) → ATM(IV最低) → 右翼(call IV高)
    
    put_map = {p["strike"]: p for p in puts if _get_iv(p) > 0}
    call_map = {c["strike"]: c for c in calls if _get_iv(c) > 0}
    all_strikes_set = sorted(set(list(put_map.keys()) + list(call_map.keys())))
    
    if len(all_strikes_set) < 3:
        return {"empty": True}
    
    # 对每个strike选择最佳IV源
    smile_points = []
    for strike in all_strikes_set:
        p = put_map.get(strike)
        c = call_map.get(strike)
        
        # 每个strike取OTM侧的IV（OTM时间价值占比大，IV计算更可靠）
        # strike < atm → put是OTM → 用put IV
        # strike >= atm → call是OTM → 用call IV（ATM归入call侧，取到真正的最低点）
        if strike < atm_strike:
            # 左侧：优先put
            chosen = p if p else c
        else:
            # 右侧（含ATM）：优先call
            chosen = c if c else p
        
        if chosen:
            smile_points.append({
                "strike": strike,
                "iv": _get_iv(chosen),
                "source": "P" if chosen is p else "C",
            })
    
    if len(smile_points) < 3:
        return {"empty": True}
    
    all_ivs = [p["iv"] for p in smile_points]
    min_s, max_s = min(all_strikes_set), max(all_strikes_set)
    max_iv = max(all_ivs) * 1.15
    if max_iv < 0.1:
        max_iv = 0.6
    
    # 坐标映射
    def sx(strike):
        if max_s == min_s:
            return 255
        return 50 + (strike - min_s) / (max_s - min_s) * 410
    
    def sy(iv):
        return 165 - (iv / max_iv) * 140
    
    # 合并曲线点
    merged_points = []
    for p in smile_points:
        merged_points.append({
            "x": round(sx(p["strike"]), 1),
            "y": round(sy(p["iv"]), 1),
            "strike": p["strike"],
            "iv": p["iv"],
            "source": p["source"],
        })
    
    merged_polyline = " ".join(f"{p['x']},{p['y']}" for p in merged_points)
    
    # Call单独线 (淡蓝, 半透明) — 使用_get_iv
    call_pts = [{"x": round(sx(c["strike"]), 1), "y": round(sy(_get_iv(c)), 1)}
                for c in calls if _get_iv(c) > 0]
    call_polyline = " ".join(f"{p['x']},{p['y']}" for p in call_pts) if len(call_pts) > 1 else ""
    
    # Put单独线 (淡红, 半透明) — 使用_get_iv
    put_pts = [{"x": round(sx(p["strike"]), 1), "y": round(sy(_get_iv(p)), 1)}
               for p in puts if _get_iv(p) > 0]
    put_polyline = " ".join(f"{p['x']},{p['y']}" for p in put_pts) if len(put_pts) > 1 else ""
    
    # ATM标线
    atm_x = round(sx(atm_strike), 1)
    
    # Y轴刻度 (取5-6个整数值)
    y_ticks = []
    iv_step = 0.05 if max_iv < 0.4 else 0.1
    v = iv_step
    while v <= max_iv:
        y_ticks.append({"value": f"{v*100:.0f}%", "y": round(sy(v), 1)})
        v += iv_step
    
    # X轴刻度 (取5-7个均匀分布的strike)
    x_ticks = []
    n_ticks = min(7, len(all_strikes_set))
    if n_ticks > 0:
        step = max(1, len(all_strikes_set) // (n_ticks - 1)) if n_ticks > 1 else 1
        indices = list(range(0, len(all_strikes_set), step))
        if indices[-1] != len(all_strikes_set) - 1:
            indices.append(len(all_strikes_set) - 1)
        for idx in indices:
            s = all_strikes_set[idx]
            x_ticks.append({"value": f"{s:.2f}", "x": round(sx(s), 1)})
    
    # HV参考线 (如果有, 由外部传入)
    return {
        "empty": False,
        "merged_points": merged_points,
        "merged_polyline": merged_polyline,
        "call_polyline": call_polyline,
        "put_polyline": put_polyline,
        "atm_x": atm_x,
        "atm_strike": atm_strike,
        "y_ticks": y_ticks,
        "x_ticks": x_ticks,
        "max_iv": max_iv,
    }


def build_all_smiles(contracts: list, underlyings_info: dict) -> dict:
    """
    构建所有品种所有月份的微笑数据
    返回: {underlying_code: {month: smile_data, ...}, ...}
    """
    result = {}

    for code, info in underlyings_info.items():
        result[code] = {}
        etf_price = info["price"]

        for month in info["months"]:
            month_contracts = [c for c in contracts
                                if c["underlying_code"] == code and c.get("month") == month]
            if month_contracts:
                result[code][month] = build_smile(month_contracts, etf_price)

    return result
