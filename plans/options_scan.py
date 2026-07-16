"""
全合约扫描引擎

调度: 数据采集 → HV计算 → 卖方排名 → 买方排名 → 微笑分析 → 汇总输出
"""

from collectors.options import fetch_all_options, UNDERLYINGS
from collectors.etf_kline import fetch_all_klines
from collectors.greeks import fetch_greeks, match_greeks_to_contracts
from analysis.hv import calc_all_hv
from analysis.seller import rank_sellers
from analysis.buyer import rank_buyers


def run_scan(underlying: str = None, month: str = None, top_n: int = 10, 
             min_sigma: float = 1.5, max_days: int = 60) -> dict:
    """
    执行全量扫描分析
    
    参数:
        underlying: 指定品种 (如"510050"), None=全部
        month: 指定月份 (如"2607"), None=全部
        top_n: 排名取前N
    
    返回: 完整分析结果字典
    """
    print("=" * 60)
    print("ETF期权全合约扫描")
    print("=" * 60)

    # Step 1: 采集期权数据
    print("\n[1/5] 采集期权数据...")
    options_data = fetch_all_options(underlying=underlying, month=month)
    contracts = options_data["contracts"]
    underlyings_info = options_data["underlyings"]
    print(f"  共采集 {len(contracts)} 个合约, {len(underlyings_info)} 个品种")

    # Step 2: 采集ETF K线 (计算HV)
    print("\n[2/5] 计算历史波动率...")
    klines_map = fetch_all_klines()
    hv_map = calc_all_hv(klines_map)

    # Step 3: 采集Greeks
    print("\n[3/5] 采集Greeks数据...")
    greeks = fetch_greeks()
    if greeks:
        contracts = match_greeks_to_contracts(contracts, greeks)
    else:
        print("  ⚠ Greeks不可用，使用近似Delta")

    # Step 4: 卖方排名
    print(f"\n[4/5] 卖方机会扫描...")
    seller_rank = rank_sellers(contracts, hv_map, min_sigma=min_sigma, max_days=max_days)
    print(f"  符合条件: {len(seller_rank)} 个合约")

    # Step 5: 买方排名
    print(f"\n[5/5] 买方机会扫描...")
    buyer_rank = rank_buyers(contracts, hv_map)
    print(f"  符合条件: {len(buyer_rank)} 个合约")

    # 汇总
    result = {
        "fetch_time": options_data["fetch_time"],
        "underlyings": underlyings_info,
        "hv_map": hv_map,
        "total_contracts": len(contracts),
        "seller_top": seller_rank[:top_n],
        "buyer_top": buyer_rank[:top_n],
        "seller_all_count": len(seller_rank),
        "buyer_all_count": len(buyer_rank),
        "contracts_raw": contracts,  # 全量原始数据 (HTML报告用)
    }

    return result


def print_summary(result: dict):
    """终端文字摘要"""
    print("\n" + "=" * 60)
    print("市场全景")
    print("=" * 60)

    for code, info in result["underlyings"].items():
        hv_data = result["hv_map"].get(code, {})
        hv60 = hv_data.get("hv60")
        hv60_str = f"{hv60*100:.1f}%" if hv60 else "N/A"
        print(f"  {info['name']:12s} 现价={info['price']:.4f}  HV60={hv60_str}")

    print(f"\n卖方Top{len(result['seller_top'])} (按单位风险收益排序):")
    print(f"{'合约名':20s} {'行权价':>6s} {'天':>3s} {'年化':>7s} {'安全边际':>8s} {'σ倍数':>7s} {'IV-HV':>7s} {'单位收益':>8s} {'判定'}")
    print("-" * 95)
    for s in result["seller_top"]:
        spread = f"{s['iv_hv_spread']*100:+.1f}%" if s.get("iv_hv_spread") is not None else "N/A"
        sigma_str = f"{s['safety_margin_sigma']:.2f}σ" if s.get("safety_margin_sigma") is not None else "N/A"
        return_per_risk_str = f"{s['return_per_risk']:.2f}" if s.get("return_per_risk") is not None else "N/A"
        print(f"  {s['name']:18s} {s['strike']:6.2f} {s['days']:3d} {s['annualized_yield']*100:6.1f}% {s['safety_margin']*100:7.1f}% {sigma_str:>7s} {spread:>7s} {return_per_risk_str:>8s} {s['verdict']}")

    print(f"\n买方Top{len(result['buyer_top'])} (按性价比排序):")
    print(f"{'合约名':20s} {'行权价':>6s} {'天':>3s} {'成本率':>7s} {'杠杆':>5s} {'买方σ':>6s} {'IV折扣':>7s} {'性价比':>7s} {'判定'}")
    print("-" * 90)
    for b in result["buyer_top"]:
        sigma_str = f"{b['buyer_sigma']:.2f}" if b.get("buyer_sigma") is not None else "N/A"
        discount_str = f"{b['iv_discount']*100:+.1f}%" if b.get("iv_discount") is not None else "N/A"
        score_str = f"{b['value_score']:.2f}" if b.get("value_score") is not None else "N/A"
        bonus = " *" if b.get("position_bonus", 1) > 1 else ""
        print(f"  {b['name']:18s} {b['strike']:6.2f} {b['days']:3d} {b['cost_rate']*100:6.2f}% {b['leverage']:5.1f}x {sigma_str:>6s} {discount_str:>7s} {score_str:>7s}{bonus} {b['verdict']}")
