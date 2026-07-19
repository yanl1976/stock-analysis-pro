# -*- coding: utf-8 -*-
"""验证用: 生成一份测试 HTML 周报, 在「蒸馏精选 + 买入建议」注入样本股 (强制震荡市),
并复用真实补全的自选股, 供人工核对页面布局/字段是否正确。不影响正式报告逻辑。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plans.weekly_hotspot import (
    enrich_watchlist, enrich_pool_entries,
    _estimate_win_rate, _rating, _build_plan, _sell_hint, _trend_state,
)
from plans.breakout_scan import _kline_cached
from plans.stock_pool import load_pool as _pool_load
from collectors.quote import realtime
from analysis.breakout import classify_stage
from core.html_renderer import render

WATCHLIST_PATH = os.path.join("data", "watchlist.json")
POOL_PATH = os.path.join("data", "stock_pool.json")


def _make_sample_candidate():
    """优先用真实股票(600519)构造样本, 失败则退回硬编码样本, 保证离线也能验证布局。"""
    try:
        sym = "600519"
        q = realtime(sym)
        kl = _kline_cached(sym)
        closes = [b["close"] for b in kl]
        highs = [b["high"] for b in kl]
        lows = [b["low"] for b in kl]
        vols = [b["volume"] for b in kl]
        res = classify_stage(closes, highs, lows, vols, price=q["price"])
        wr = _estimate_win_rate(res["stage"], res["signals"])
        rating = _rating(res["score"], wr, res["stage"])
        ma = _trend_state(closes, q["price"]) if len(closes) >= 20 else None
        bp, sl, sp, pos, bl = _build_plan(
            {"price": q["price"], "stage": res["stage"], "change_pct": q["change_pct"]}, wr, rating, ma)
        tp, sh = _sell_hint(q["price"], res["stage"], sl)
        return {
            "name": q["name"], "symbol": sym, "price": q["price"], "change_pct": q["change_pct"],
            "stage": res["stage"], "score": res["score"], "signals": res["signals"], "details": res["details"],
            "concept": "验证板块(真实数据)", "concepts": ["验证板块", "白酒"],
            "win_rate": wr, "rating": rating, "position": pos,
            "buy_point": bp, "stop_loss": sl, "stop_pct": sp, "take_profit": tp, "sell_hint": sh,
            "prior_runup": 5.0,
        }
    except Exception as e:
        print(f"  [样本] 真实数据构造失败, 退回硬编码样本: {e}")
        return {
            "name": "贵州茅台", "symbol": "600519", "price": 1680.0, "change_pct": 1.85,
            "stage": "breakout", "score": 78,
            "signals": ["平台整理", "VCP波动收缩×2", "MACD金叉", "均线多头排列", "放量(量比1.83)"],
            "details": {"band_width": 0.12, "bb_squeeze_pct": 0.18, "vcp": 2, "vol_ratio": 1.83,
                        "pct_to_resistance": 1.2, "macd_gc": True, "kdj_gc": False, "ma_bull": True},
            "concept": "验证板块(硬编码)", "concepts": ["验证板块", "白酒"],
            "win_rate": 0.68, "rating": "重点", "position": 8,
            "buy_point": "突破回踩5日线≈¥1596.00低吸，放量过前高加仓",
            "stop_loss": 1562.4, "stop_pct": -7.0, "take_profit": 1881.6,
            "sell_hint": "涨+12%减仓半仓(¥1881.6)锁利; 减仓后破MA20(趋势线)或回撤10%清仓; 破¥1562.4止损(减仓后上移保本)",
            "prior_runup": 5.0,
        }


def main():
    print("▶ 生成验证用 HTML 周报...", flush=True)
    cand = _make_sample_candidate()
    final = [cand]
    breakthrough = {
        "regime": "震荡",
        "regime_diff": -0.5,
        "strategy_label": "高胜率共振",
        "strategy_desc": "大盘纠缠震荡, 仅高胜率共振(S3)有正期望(56%/+2.97%), 边薄。降仓位、严止损。",
        "candidates": final,
        "final": final,
        "count": len(final),
        "final_count": len(final),
        "excluded": [],
        "excluded_count": 0,
        "error": None,
    }
    hotspots = [{
        "name": "验证热点板块", "change_pct": 2.51, "amount": 1.23e8,
        "leader": "示例股", "leader_pct": 3.12,
    }]
    # 真实补全自选股 (网络失败自动降级为仅代码)
    try:
        raw = json.load(open(WATCHLIST_PATH, encoding="utf-8")) or []
    except Exception:
        raw = []
    wl = enrich_watchlist(raw, verbose=True)

    # 真实股票池 (86 只, 加载后注入实时 现价/涨幅, 验证新列)
    pool_entries = _pool_load().get("entries", [])
    enrich_pool_entries(pool_entries, verbose=True)
    print(f"    策略股票池 {len(pool_entries)} 只已注入现价/涨幅", flush=True)

    path = render(
        {
            "date": "2026-07-19 (验证)",
            "hotspots": hotspots,
            "human_watchlist": wl,
            "stock_pool": pool_entries,
            "breakthrough": breakthrough,
        },
        "weekly_hotspot_report",
        output_dir=os.path.join("data"),
        filename="_verify_report.html",
    )
    print(f"  ✓ 验证报告已生成: {path}")
    print("    打开该文件即可核对: 自选股(综合分析卡片) / 蒸馏精选(填充) / 买入建议与计划(填充)")


if __name__ == "__main__":
    main()
