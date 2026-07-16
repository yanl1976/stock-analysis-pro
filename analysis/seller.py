"""
卖方指标计算模块

核心指标:
- 保证金估算 (简化版)
- 直接收益率 / 年化收益率
- IV-HV Spread / Ratio
- 安全边际 (卖Put: 距现价百分比)
- 风险因子 (被行权概率 × 波动风险 × 流动性惩罚)
- 收益风险比 (年化收益率 / 风险因子)
"""

import math


def calc_margin(strike: float, premium: float, multiplier: int = 10000, option_type: str = "P") -> float:
    """
    简化保证金计算 (卖出开仓)
    
    卖认沽保证金 ≈ max(行权价×乘数×12% + 权利金×乘数, 行权价×乘数×7% + 权利金×乘数)
    简化为: 行权价 × 乘数 × 12% (取主要部分)
    
    实际公式更复杂，这里用12%近似，足够做对比分析
    """
    # 卖Put保证金 = 行权价 × 乘数 × 保证金比例 + 权利金 × 乘数
    # 保证金比例: 约12% (虚值部分可减免，但简化处理)
    margin_rate = 0.12
    margin = strike * multiplier * margin_rate + premium * multiplier
    return round(margin, 2)


def calc_seller_metrics(contract: dict, hv60: float = None) -> dict:
    """
    计算单个合约的卖方指标
    
    contract: 期权合约数据 (含 last_price, strike, iv, days_to_expiry, volume, amount, option_type, underlying_price, delta)
    hv60: 标的的历史波动率(60日, 市场标准)
    
    返回: 卖方指标字典
    """
    premium = contract["last_price"]
    strike = contract["strike"]
    iv = contract["iv"]
    days = contract["days_to_expiry"]
    etf_price = contract["underlying_price"]
    multiplier = contract.get("multiplier", 10000)
    option_type = contract["option_type"]
    delta = contract.get("delta")
    amount = contract.get("amount", 0)

    # 基本校验
    if premium <= 0 or strike <= 0 or days <= 0:
        return None

    # 保证金
    margin = calc_margin(strike, premium, multiplier, option_type)
    if margin <= 0:
        return None

    # 收益率
    direct_yield = premium / (strike * 0.12)  # 简化: 权利金 / (行权价×12%)
    annualized_yield = direct_yield * 365 / days

    # 安全边际 (绝对距离)
    if option_type == "P":
        # 卖Put: 安全边际 = (ETF价 - 行权价) / ETF价
        safety_margin = (etf_price - strike) / etf_price if etf_price > 0 else 0
    else:
        # 卖Call: 安全边际 = (行权价 - ETF价) / ETF价
        safety_margin = (strike - etf_price) / etf_price if etf_price > 0 else 0

    # 波动率归一化安全边际 (σ倍数)
    safety_margin_sigma = None
    if hv60 and hv60 > 0 and days > 0:
        time_adjusted_hv = hv60 * math.sqrt(days / 252)
        if time_adjusted_hv > 0:
            safety_margin_sigma = round(safety_margin / time_adjusted_hv, 2)

    # IV-HV分析
    iv_hv_spread = None
    iv_hv_ratio = None
    if hv60 and hv60 > 0 and iv > 0:
        iv_hv_spread = round(iv - hv60, 4)
        iv_hv_ratio = round(iv / hv60, 2)

    # 单位风险收益 = 年化收益率 / |Delta|
    return_per_risk = None
    if delta is not None and abs(delta) > 0.001:
        return_per_risk = round(annualized_yield / abs(delta), 2)

    # 判定 (基于σ倍数)
    if safety_margin_sigma is not None:
        if safety_margin_sigma >= 2.0 and annualized_yield > 0.20 and iv_hv_spread and iv_hv_spread > 0:
            verdict = "🟢"
            verdict_text = "优质卖方机会"
        elif safety_margin_sigma >= 1.5 and annualized_yield > 0.15:
            verdict = "🟡"
            verdict_text = "正常卖方机会"
        elif safety_margin_sigma >= 1.0:
            verdict = "🟠"
            verdict_text = "勉强可做"
        else:
            verdict = "🔴"
            verdict_text = "风险过高"
    else:
        # 无σ数据时降级为绝对收益率判定
        if annualized_yield > 0.30 and iv_hv_spread and iv_hv_spread > 0.03:
            verdict = "🟡"
            verdict_text = "正常卖方机会(无σ)"
        elif annualized_yield > 0.10:
            verdict = "🟠"
            verdict_text = "勉强可做"
        else:
            verdict = "🔴"
            verdict_text = "不值得卖"

    return {
        "premium": premium,
        "strike": strike,
        "days": days,
        "margin": margin,
        "direct_yield": round(direct_yield, 4),
        "annualized_yield": round(annualized_yield, 4),
        "safety_margin": round(safety_margin, 4),
        "safety_margin_sigma": safety_margin_sigma,
        "iv": iv,
        "hv60": hv60,
        "iv_hv_spread": iv_hv_spread,
        "iv_hv_ratio": iv_hv_ratio,
        "delta": delta,
        "return_per_risk": return_per_risk,
        "amount": amount,
        "verdict": verdict,
        "verdict_text": verdict_text,
    }


def rank_sellers(contracts: list, hv_map: dict, min_days: int = 7, max_days: int = 90,
                 min_amount: float = 100000, max_delta: float = 0.4, min_annual: float = 0.50,
                 min_premium_per_contract: float = 100, min_sigma: float = 1.0) -> list:
    """
    全合约扫描，输出卖方Top排名
    
    过滤条件:
    - 剩余天数 >= min_days
    - 剩余天数 <= max_days
    - 成交额 > min_amount
    - |Delta| < max_delta (卖虚值)
    - 年化收益率 > min_annual
    - 单张合约权利金 > min_premium_per_contract (绝对收益门槛)
    - σ倍数 >= min_sigma (波动率归一化安全边际)
    
    排序: 单位风险收益 (年化/Delta) 降序
    """
    results = []

    for c in contracts:
        days = c.get("days_to_expiry", 0)
        amount = c.get("amount", 0)
        iv = c.get("iv", 0)
        premium = c.get("last_price", 0)
        multiplier = c.get("multiplier", 10000)

        # 基本过滤
        if days < min_days or days > max_days:
            continue
        if amount < min_amount:
            continue
        if premium <= 0 or iv <= 0:
            continue

        # 绝对收益门槛: 单张合约权利金 = premium × multiplier / multiplier
        # 即 premium × 10000 元 → 要 > min_premium_per_contract
        premium_per_contract = premium * multiplier
        if premium_per_contract < min_premium_per_contract:
            continue

        # Delta过滤 (如果有)
        delta = c.get("delta")
        if delta is not None and abs(delta) > max_delta:
            continue

        # 获取对应品种的HV
        underlying_code = c.get("underlying_code", "")
        hv60 = hv_map.get(underlying_code, {}).get("hv60")

        # 计算卖方指标
        metrics = calc_seller_metrics(c, hv60)
        if metrics is None:
            continue

        # 年化收益率过滤
        if metrics["annualized_yield"] < min_annual:
            continue

        # ===== σ倍数筛选: 必须 >= min_sigma =====
        sigma = metrics.get("safety_margin_sigma")
        if sigma is None or sigma < min_sigma:
            continue

        # ===== 60日位置惩罚 =====
        position = hv_map.get(underlying_code, {}).get("position_60d")
        option_type = c.get("option_type")
        if position is not None:
            # 卖Put + 低位 → 危险; 卖Call + 高位 → 危险
            if (option_type == "P" and position < 0.3) or (option_type == "C" and position > 0.7):
                # 位置不利, 降低单位风险收益
                if metrics.get("return_per_risk"):
                    metrics["return_per_risk"] = round(metrics["return_per_risk"] * 0.67, 2)  # 等效×1.5惩罚
        metrics["position_60d"] = position

        # 合并合约信息
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
            "premium_per_contract": round(premium_per_contract, 2),
            **metrics,
        }
        results.append(entry)

    # 排序: 单位风险收益降序
    results.sort(key=lambda x: x.get("return_per_risk") or 0, reverse=True)

    return results
