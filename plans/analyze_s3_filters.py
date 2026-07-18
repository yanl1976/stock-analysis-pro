# -*- coding: utf-8 -*-
"""全策略 × 买点regime门控 归因分析。

复用 walk_forward 的 window_picks (各策略已结算逐笔), 对每套策略比较:
  (A) 原始全样本胜率/均收益/超额
  (B) 买点regime门控 (上证 MA20<=MA60 的买点直接空仓) 后的胜率/均收益/超额
并输出"该参与/该空仓"买点清单 (基于 S0 全样本窗口盈亏比, 作决策基准)。

用法: python plans/analyze_s3_filters.py
"""
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from plans.backtest_hotspot import (
    walk_forward, _kl, kline_upto, _stats, _market_weak, STRATEGIES,
    _append_window_pnl,
)

WF_START, WF_END = "2024-06-01", "2026-07-18"
HOLD, STEP, CONCEPTS, PER = 30, 25, 8, 15


def _regime_ok(buy_date):
    return not _market_weak(buy_date)


def main():
    print(f"[{datetime.now():%H:%M:%S}] walk-forward 取全策略 window_picks ...")
    agg, buy_dates, window_summ, window_picks = walk_forward(
        WF_START, WF_END, hold_days=HOLD, step_days=STEP,
        concepts=CONCEPTS, per=PER, verbose=False)

    print(f"\n{'='*64}\n  全策略: 原始 vs 买点regime门控 (空仓弱势买点)\n{'='*64}")
    print(f"  {'策略':<20}{'模式':<8}{'样本':>5}{'胜率':>8}{'均收益':>9}{'超额':>9}")
    best_fwd = None
    for name, _fn in STRATEGIES:
        wp = window_picks.get(name, [])
        # (A) 原始
        allp = []
        for bd, sd, bench, picks in wp:
            allp.extend(picks)
        sa = _stats(allp, None)
        # (B) regime门控: 剔除弱势买点
        gated = []
        for bd, sd, bench, picks in wp:
            if _regime_ok(bd):
                gated.extend(picks)
        sg = _stats(gated, None)
        def fmt(s):
            avg = f"{s['avg']:+.2f}%" if s["avg"] is not None else "—"
            ex = f"{s['excess']:+.2f}%" if s["excess"] is not None else "—"
            return s["n"], s["win"], avg, ex
        na, wa, aa, ea = fmt(sa)
        ng, wg, ag, eg = fmt(sg)
        print(f"  {name:<18}{'原始':<8}{na:>5}{wa:>7.0f}%{aa:>9}{ea:>9}")
        print(f"  {name:<18}{'门控':<8}{ng:>5}{wg:>7.0f}%{ag:>9}{eg:>9}")
        # 门控后若胜率提升且样本>=15, 记为候选
        if ng >= 15 and sg["win"] > sa["win"] and (sg["avg"] or 0) >= (sa["avg"] or 0):
            if best_fwd is None or sg["win"] > best_fwd[2]:
                best_fwd = (name, ng, sg["win"], sg["avg"], sa["win"])

    if best_fwd:
        print(f"\n  → regime门控后胜率提升的策略: {best_fwd[0]} "
              f"({best_fwd[2]:.0f}% vs {best_fwd[4]:.0f}%, 样本{best_fwd[1]})")
    else:
        print(f"\n  → 结论: 买点regime门控未能提升任何策略的胜率 "
              f"(弱势买点同时含盈利与亏损样本, 无法干净分离)")

    # 买点参与/空仓决策 (基于 S0 全样本窗口盈亏比)
    print(f"\n{'='*64}\n  买点窗口盈亏比汇总 (S0 基线 / S3 高胜率)\n{'='*64}")
    s0_rows = _append_window_pnl([], window_picks.get("S0 基线(评分≥45+不追)", []), "S0")
    print()
    _append_window_pnl([], window_picks.get("S3 高胜率共振", []), "S3")
    part = [r[0] for r in s0_rows if r[9] == "参与"]
    skip = [r[0] for r in s0_rows if r[9] == "空仓"]
    print(f"\n  · 基于 S0 全样本: 该参与买点 {len(part)} 个 → {part}")
    print(f"  · 该空仓买点 {len(skip)} 个 → {skip}")
    print(f"\n[{datetime.now():%H:%M:%S}] 完成")


if __name__ == "__main__":
    main()
