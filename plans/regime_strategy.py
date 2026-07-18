# -*- coding: utf-8 -*-
"""大盘三状态 × 策略 蒸馏分析。

复用 walk_forward 的 window_picks (各策略已结算逐笔), 对每个买点按上证
MA20/MA60 判定大盘状态 (多头/震荡/空头), 再把每个策略的 picks 按状态分组,
统计各状态下的胜率/均收益/超额, 从而蒸馏出:

  · 多头排列行情 → 该用哪套策略 (顺势追强?)
  · 震荡纠缠行情 → 该用哪套策略 (回调低吸?)
  · 空头排列行情 → 是否应空仓 (剔除虚假信号?)

用法:
  python plans/regime_strategy.py            # 跑 walk-forward + 蒸馏 (慢, ~5min)
  python plans/regime_strategy.py --use-cache # 用已缓存的 window_picks 快速重算
"""
import os
import sys
import json
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_PATH = os.path.join(DATA_DIR, "regime_window_picks.json")

from plans.backtest_hotspot import (
    walk_forward, _kl, kline_upto, _stats, STRATEGIES,
)

WF_START, WF_END = "2024-06-01", "2026-07-18"
HOLD, STEP, CONCEPTS, PER = 30, 25, 8, 15

# 三状态阈值: MA20 相对 MA60
#   多头: MA20 > MA60 * (1+bull_margin)
#   空头: MA20 < MA60 * (1-bull_margin)
#   震荡: 其余 (MA20/MA60 在 ±neutral_band 内视为纠缠; 但此处用 margin 直接二分,
#          neutral_band 仅用于把"极度纠缠"单独标出)
BULL_MARGIN = 0.0
NEUTRAL_BAND = 0.03


def classify_regime(buy_date):
    """上证 MA20 vs MA60 判定大盘三状态。"""
    try:
        kl = _kl("000001")
        kl_b = kline_upto(kl, buy_date)
        if len(kl_b) < 60:
            return "未知"
        closes = [b["close"] for b in kl_b]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        if ma20 > ma60 * (1 + BULL_MARGIN):
            if abs(ma20 - ma60) / ma60 <= NEUTRAL_BAND:
                return "震荡"
            return "多头"
        if ma20 < ma60 * (1 - BULL_MARGIN):
            if abs(ma20 - ma60) / ma60 <= NEUTRAL_BAND:
                return "震荡"
            return "空头"
        return "震荡"
    except Exception:
        return "未知"


def _strip_kl(wp):
    """去掉 picks 里的 kl 大字段, 便于缓存。"""
    out = {}
    for name, rows in wp.items():
        nr = []
        for bd, sd, bench, picks in rows:
            npk = []
            for p in picks:
                q = dict(p)
                q.pop("kl", None)
                npk.append(q)
            nr.append((bd, sd, bench, npk))
        out[name] = nr
    return out


def _load_or_run(use_cache):
    if use_cache and os.path.exists(CACHE_PATH):
        print(f"[{datetime.now():%H:%M:%S}] 读取缓存 window_picks: {CACHE_PATH}")
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            wp = json.load(f)
        # json 把 tuple 存成 list, 还原 (bd,sd,bench,picks)
        wp = {k: [(r[0], r[1], r[2], r[3]) for r in v] for k, v in wp.items()}
        return wp
    print(f"[{datetime.now():%H:%M:%S}] 运行 walk-forward (慢) ...")
    agg, buy_dates, window_summ, wp = walk_forward(
        WF_START, WF_END, hold_days=HOLD, step_days=STEP,
        concepts=CONCEPTS, per=PER, verbose=False)
    stripped = _strip_kl(wp)
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(stripped, f, ensure_ascii=False)
        print(f"[{datetime.now():%H:%M:%S}] 已缓存 window_picks → {CACHE_PATH}")
    except Exception as e:
        print(f"  ⚠️ 缓存失败: {e}")
    return wp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-cache", action="store_true", help="用已缓存 window_picks")
    args = ap.parse_args()

    wp = _load_or_run(args.use_cache)

    # 每个买点的状态 (各策略共享同一组买点)
    any_name = next(iter(wp))
    bd_regime = {}
    for bd, sd, bench, picks in wp[any_name]:
        bd_regime[bd] = classify_regime(bd)

    regimes = ["多头", "震荡", "空头"]
    print(f"\n{'='*72}")
    print(f"  大盘三状态 × 策略 蒸馏 (walk-forward {WF_START}→{WF_END})")
    print(f"  状态判定: 多头=MA20>MA60(>3%纠缠外) / 空头=MA20<MA60(>3%纠缠外) / 震荡=纠缠")
    print(f"{'='*72}")

    # 全策略 × 三状态 矩阵
    print(f"\n  {'策略':<20}{'状态':<6}{'样本':>5}{'胜率':>8}{'均收益':>9}{'超额':>9}")
    best_per_regime = {r: None for r in regimes}
    for name, _fn in STRATEGIES:
        rows = wp.get(name, [])
        by_reg = {r: [] for r in regimes}
        for bd, sd, bench, picks in rows:
            rg = bd_regime.get(bd, "未知")
            if rg in by_reg:
                by_reg[rg].extend(picks)
        for rg in regimes:
            st = _stats(by_reg[rg], None)
            if st["n"] == 0:
                continue
            avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
            ex = f"{st['excess']:+.2f}%" if st["excess"] is not None else "—"
            print(f"  {name:<18}{rg:<6}{st['n']:>5}{st['win']:>7.0f}%{avg:>9}{ex:>9}")
            # 记录每状态最优策略 (胜率优先, 均收益次之, 样本>=10)
            if st["n"] >= 10:
                cur = best_per_regime[rg]
                if cur is None or (st["win"], st["avg"] or -999) > (cur[1], cur[2] or -999):
                    best_per_regime[rg] = (name, st["win"], st["avg"], st["n"])
        print()

    print(f"{'='*72}")
    print(f"  蒸馏结论: 各行情状态最优策略")
    print(f"{'='*72}")
    for rg in regimes:
        b = best_per_regime[rg]
        if b:
            print(f"  · {rg}: {b[0]}  (胜率 {b[1]:.0f}% / 均收益 {b[2]:+.2f}% / 样本 {b[3]})")
        else:
            print(f"  · {rg}: (样本不足, 建议空仓)")

    # 额外: 空头状态若胜率极低, 印证"空仓"决策
    print(f"\n  说明: 若'空头'列各策略胜率普遍<45%, 则空头行情应直接空仓, "
          f"不出股 — 这是从'买点窗口盈亏比'对比得出的主要虚假信号来源。")
    print(f"[{datetime.now():%H:%M:%S}] 完成")


if __name__ == "__main__":
    main()
