"""
买方指标计算模块

核心指标:
- 期权成本率 = 权利金 / 行权价
- 杠杆倍数 = ETF价 / 权利金
- 盈亏平衡点
- 买方σ倍数 = 盈亏平衡距离 / 时间调整波动率 (需要多大波动才能盈利)
- IV折扣 = 1 - IV/HV60 (IV比HV便宜多少)
- 性价比 = 杠杆 × iv_discount / 买方σ (综合排序)
- 60日位置加成 (买Call低位加分, 买Put高位加分)
"""

import math


def calc_buyer_metrics(contract: dict, hv60: float = None, position_60d: float = None) -> dict:
    """
    计算单个合约的买方指标
    """
    premium = contract["last_price"]
    strike = contract["strike"]
    iv = contract["iv"]
    days = contract["days_to_expiry"]
    etf_price = contract["underlying_price"]
    option_type = contract["option_type"]
    delta = contract.get("delta")
    amount = contract.get("amount", 0)

    if premium <= 0 or strike <= 0 or days <= 0:
        return None

    # 成本率: 权利金占行权价的比例
    cost_rate = premium / strike

    # 杠杆: ETF价 / 权利金
    leverage = etf_price / premium if premium > 0 else 0

    # 盈亏平衡
    if option_type == "C":
        breakeven = strike + premium
        breakeven_pct = (breakeven - etf_price) / etf_price  # 需涨多少%才赚钱
    else:
        breakeven = strike - premium
        breakeven_pct = (etf_price - breakeven) / etf_price  # 需跌多少%才赚钱

    # IV/HV分析
    iv_hv_ratio = None
    iv_discount = 0
    if hv60 and hv60 > 0 and iv > 0:
        iv_hv_ratio = round(iv / hv60, 2)
        iv_discount = 1 - iv / hv60  # 正值=IV便宜, 负值=IV偏贵

    # 买方σ倍数: 盈亏平衡距离 / 时间调整波动率
    # σ越小 → 越容易盈利
    buyer_sigma = None
    if hv60 and hv60 > 0 and days > 0 and etf_price > 0:
        time_adjusted_hv = hv60 * math.sqrt(days / 252)
        if time_adjusted_hv > 0:
            buyer_sigma = round(abs(breakeven_pct) / time_adjusted_hv, 2)

    # 性价比 = 杠杆 × iv_discount / 买方σ
    # 杠杆高 + IV便宜 + 容易盈利 → 性价比高
    value_score = None
    if leverage > 0 and iv_discount > 0 and buyer_sigma and buyer_sigma > 0:
        value_score = round(leverage * iv_discount / buyer_sigma, 2)

    # 60日位置加成
    # 买Call: 低位好 (position<0.3 → ×1.3)
    # 买Put: 高位好 (position>0.7 → ×1.3)
    position_bonus = 1.0
    if position_60d is not None:
        if option_type == "C" and position_60d < 0.3:
            position_bonus = 1.3
        elif option_type == "P" and position_60d > 0.7:
            position_bonus = 1.3

    # 最终性价比 = 基础性价比 × 位置加成
    adjusted_value_score = None
    if value_score is not None:
        adjusted_value_score = round(value_score * position_bonus, 2)

    # 判定
    verdict = "🔴"
    verdict_text = "不建议买入"
    if buyer_sigma is not None and iv_discount > 0:
        if buyer_sigma < 0.5 and iv_discount > 0.3:
            verdict = "🟢"
            verdict_text = "优质买方机会"
        elif buyer_sigma < 1.0 and iv_discount > 0.1:
            verdict = "🟡"
            verdict_text = "良好买方机会"
        elif buyer_sigma < 1.5:
            verdict = "🟠"
            verdict_text = "可考虑"
    elif iv_hv_ratio and iv_hv_ratio >= 1.0:
        verdict = "🔴"
        verdict_text = "IV偏贵，考虑做卖方"

    return {
        "premium": premium,
        "strike": strike,
        "days": days,
        "cost_rate": round(cost_rate, 4),
        "leverage": round(leverage, 2),
        "breakeven": round(breakeven, 4),
        "breakeven_pct": round(breakeven_pct, 4),
        "iv": iv,
        "hv60": hv60,
        "iv_hv_ratio": iv_hv_ratio,
        "iv_discount": round(iv_discount, 4),
        "buyer_sigma": buyer_sigma,
        "delta": delta,
        "position_60d": position_60d,
        "position_bonus": position_bonus,
        "value_score": adjusted_value_score,
        "raw_value_score": value_score,
        "amount": amount,
        "verdict": verdict,
        "verdict_text": verdict_text,
    }


def rank_buyers(contracts: list, hv_map: dict, min_days: int = 14, max_days: int = 90,
                min_amount: float = 100000, min_delta: float = 0.15, max_delta: float = 0.70,
                max_iv_hv: float = 1.0, max_buyer_sigma: float = 1.5) -> list:
    """
    全合约扫描，输出买方Top排名
    
    过滤条件:
    - 剩余天数 14~90
    - 成交额 > 10万
    - Delta在 [0.15, 0.70] 之间 (不要太极端)
    - IV/HV < 1.0 (IV没被高估, 买方要买便宜的)
    - 买方σ < 1.5 (有合理盈利概率)
    
    排序: 性价比 (杠杆×iv_discount/买方σ×位置加成) 降序
    """
    results = []

    for c in contracts:
        days = c.get("days_to_expiry", 0)
        amount = c.get("amount", 0)
        iv = c.get("iv", 0)
        premium = c.get("last_price", 0)

        # 基本过滤
        if days < min_days or days > max_days:
            continue
        if amount < min_amount:
            continue
        if premium <= 0 or iv <= 0:
            continue

        # Delta过滤
        delta = c.get("delta")
        if delta is not None:
            abs_delta = abs(delta)
            if abs_delta < min_delta or abs_delta > max_delta:
                continue

        # 获取HV和位置
        underlying_code = c.get("underlying_code", "")
        hv60 = hv_map.get(underlying_code, {}).get("hv60")
        position_60d = hv_map.get(underlying_code, {}).get("position_60d")

        # IV/HV过滤
        if hv60 and hv60 > 0 and iv > 0:
            if iv / hv60 > max_iv_hv:
                continue

        # 计算买方指标
        metrics = calc_buyer_metrics(c, hv60, position_60d)
        if metrics is None:
            continue

        # 买方σ过滤
        buyer_sigma = metrics.get("buyer_sigma")
        if buyer_sigma is None or buyer_sigma > max_buyer_sigma:
            continue

        # 必须有性价比分数
        if metrics.get("value_score") is None:
            continue

        entry = {
            "name": c["name"],
            "underlying_name": c["underlying_name"],
            "underlying_code": underlying_code,
            "underlying_price": c["underlying_price"],
            "option_type": c["option_type"],
            "month": c.get("month", ""),
            "code": c.get("code", ""),
            "volume": c.get("volume", 0),
            "open_interest": c.get("open_interest", 0),
            **metrics,
        }
        results.append(entry)

    # 排序: 性价比降序
    results.sort(key=lambda x: x.get("value_score") or 0, reverse=True)

    return results
