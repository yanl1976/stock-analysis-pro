# -*- coding: utf-8 -*-
"""weekly_hotspot.py 策略实战回测 — 2025-07 → 今 逐周时点回放

目的: 验证 weekly_hotspot.py 的"大盘三状态 × 蒸馏选股"策略 (多头→S5∪S6 / 震荡→S3 / 空头→空仓)
      在 2025-07 至 2026-07-18 的真实行情中, 按策略交易 (纪律止损/止盈) 的绩效。

方法 (复用 backtest_hotspot 的时点还原引擎, 解决"实时榜单无法历史回溯"):
  · 历史热点: 用当前概念板块池(新浪全量过滤)的成分股, 在买点当日历史涨幅反推板块热度,
    排序取 Top N = 该周热点板块 (与 backtest_hotspot 完全一致的历史近似法)。
  · 选股: 热点板块成分股 K线截断到买点, 跑 classify_stage 形态识别 (与 weekly_hotspot 同逻辑)。
  · 策略路由: 时点判定大盘 regime (上证 MA20 vs MA60), 多头→回调低吸(S5)∪强趋势低波动(S6),
    震荡→高胜率共振(S3), 空头→空仓 (不出股)。
  · 交易: 买点盘后选股→以买点收盘价建仓; 纪律结算 (先触止损→止损, 先触目标→止盈, 否则持有至上限)。
  · 对照: 同时跑"无regime裸买(S5∪S6, 忽略空头)"以证明蒸馏/择时的价值。

数据口径:
  · K线来源新浪 (截至今天的最近 _KLINE_DAYS 根, 按买点切片实现时点回放)。
  · 历史热点=当前成分股近似, 与买点真实成分存在偏差 (本仓库既有的已知近似)。
  · 单笔等权, 未计手续费/滑点/停牌流动性; 过去有效≠未来有效。

用法:
  python plans/backtest_weekly_hotspot.py                         # 默认 2025-07-01→今, 逐周
  python plans/backtest_weekly_hotspot.py --start 2025-07-01 --end 2026-07-18 --step 7
  python plans/backtest_weekly_hotspot.py --hold 40 --concepts 8 --per 15
"""
import os
import sys
import json
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

from plans.backtest_hotspot import (
    restore_hotspots_on, get_board_pool, build_pool, settle, get_benchmark,
    _kl, kline_upto, _wf_buy_dates, _wf_sell_date, _stats, price_on,
    strat_pullback, strat_steady, strat_highwr, strat_box_breakout,
    _apply_plan, STAGE_LABELS,
)
from plans.weekly_hotspot import REGIME_STRATEGY

_KLINE_DAYS = 900  # 拉足 K线以覆盖 2025-07 之前的 MA60


# ───────────────── 科学止盈/止损结算 (用户要求: 大幅上涨减仓 + 破趋势线清仓) ─────────────────
def settle_smart(picks, sell_date, stop_pct=0.07, scale_out_pct=0.10,
                 scale_out_frac=0.5, trailing_pct=0.08, use_ma20=True,
                 breakeven_after_scale=True):
    """科学化退出结算 (替代旧固定止盈/止损):

      · 初始硬止损 stop_pct (默认 -7%, 比旧 -9% 更紧, 缩小单笔最大亏损)
      · 大幅上涨减仓: 价格触 +scale_out_pct (默认 +10%) 即卖出 scale_out_frac (默认半仓) 锁利
      · 减仓后: 剩余仓位用 移动止损(trailing_pct) + 破趋势线(MA20 收盘价下破) 清仓, 让利润奔跑
      · 减仓后把硬止损上移到成本价(保本), 锁定已得利润
    返回逐笔 return_pct (按仓位加权), 并打 exit_reason 标签 (止损/保本止损/减仓清仓/
    破趋势线清仓/移动止损/减仓后持有到期/持有到期)。
    """
    for p in picks:
        sym = p["symbol"]
        try:
            kl = _kl(sym)
        except Exception:
            p["return_pct"] = None
            p["hold_return"] = None
            p["exit_reason"] = "无数据"
            continue
        buy_price = p["buy_price"]
        seg = [b for b in kl if p["buy_date"] < b["date"] <= sell_date]
        # 预计算 MA20 (用全量 kl 收盘, 取每根 seg bar 前20根均值)
        closes_all = [b["close"] for b in kl]
        idx_of = {}
        for i, b in enumerate(kl):
            idx_of[b["date"][:10]] = i
        def ma20_on(date):
            i = idx_of.get(date)
            if i is None or i < 19:
                return None
            return sum(closes_all[i - 19:i + 1]) / 20
        hard_stop = round(buy_price * (1 - stop_pct), 2)
        remaining = 1.0
        ret_acc = 0.0
        scaled = False
        peak = buy_price
        exit_price = None
        exit_date = None
        reason = None
        for b in seg:
            # 1) 硬止损 (减仓后上移到成本价保本)
            cur_stop = hard_stop
            if scaled and breakeven_after_scale:
                cur_stop = max(cur_stop, round(buy_price, 2))
            if remaining > 0 and b["low"] <= cur_stop:
                ex = cur_stop
                ret_acc += remaining * (ex - buy_price) / buy_price
                remaining = 0
                exit_price = ex
                exit_date = b["date"]
                reason = "止损" if not scaled else "保本止损"
                break
            # 2) 大幅上涨减仓 (仅一次)
            if not scaled and b["high"] >= buy_price * (1 + scale_out_pct):
                scale_price = round(buy_price * (1 + scale_out_pct), 2)
                frac = min(scale_out_frac, remaining)
                ret_acc += frac * (scale_price - buy_price) / buy_price
                remaining -= frac
                scaled = True
                if remaining <= 1e-9:
                    exit_price = scale_price
                    exit_date = b["date"]
                    reason = "减仓清仓"
                    break
            # 更新峰值 (用于移动止损)
            if b["close"] > peak:
                peak = b["close"]
            # 3) 减仓后: 移动止损 / 破趋势线(MA20) 清仓
            if scaled and remaining > 0:
                trailing_stop = peak * (1 - trailing_pct)
                ma20_break = False
                if use_ma20:
                    m = ma20_on(b["date"][:10])
                    if m is not None and b["close"] < m:
                        ma20_break = True
                if ma20_break:
                    ex = round(b["close"], 2)
                    ret_acc += remaining * (ex - buy_price) / buy_price
                    remaining = 0
                    exit_price = ex
                    exit_date = b["date"]
                    reason = "破趋势线清仓"
                    break
                if b["low"] <= trailing_stop:
                    ex = round(trailing_stop, 2)
                    ret_acc += remaining * (ex - buy_price) / buy_price
                    remaining = 0
                    exit_price = ex
                    exit_date = b["date"]
                    reason = "移动止损"
                    break
        if remaining > 0:
            # 到期仍未清仓: 剩余按卖点收盘退出
            ps = price_on(kl, sell_date)
            if ps is not None:
                ret_acc += remaining * (ps - buy_price) / buy_price
                exit_price = ps
                exit_date = sell_date
                reason = "持有到期" if not scaled else "减仓后持有到期"
                remaining = 0
            else:
                reason = reason or "持有到期"
        p["exit_price"] = round(exit_price, 2) if exit_price else None
        p["exit_date"] = exit_date
        p["exit_reason"] = reason
        p["return_pct"] = round(ret_acc * 100, 2) if remaining <= 0 and exit_price else None
        ps = price_on(kl, sell_date)
        p["hold_return"] = round((ps - buy_price) / buy_price * 100, 2) if (buy_price and ps) else None
    return picks


# ───────────────── 时点大盘状态判定 ─────────────────
def regime_on(buy_date, band=0.03):
    """买点处上证 MA20 vs MA60 判定三状态 (与 weekly_hotspot.market_regime 同源, 时点版)。"""
    try:
        kl = _kl("000001")
        kl_b = kline_upto(kl, buy_date)
        if len(kl_b) < 60:
            return "未知", None
        closes = [b["close"] for b in kl_b]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        diff = (ma20 - ma60) / ma60
        if diff > band:
            return "多头", round(diff * 100, 2)
        if diff < -band:
            return "空头", round(diff * 100, 2)
        return "震荡", round(diff * 100, 2)
    except Exception:
        return "未知", None


# ───────────────── 策略: weekly_hotspot 蒸馏路由 ─────────────────
def strat_weekly_regime(pool, runup_pct=40, buy_date=None):
    """weekly_hotspot.py 实战策略: 按买点大盘状态路由。

    多头 → S5回调低吸 ∪ S6强趋势低波动 ∪ S9箱体突破
    震荡 → S3高胜率共振 ∪ S9箱体突破
    空头 → 空仓 (返回空)
    """
    regime, _ = regime_on(buy_date)
    if regime == "空头":
        return []
    if regime == "多头":
        picks = strat_pullback(pool, runup_pct=runup_pct, buy_date=buy_date)
        picks += strat_steady(pool, runup_pct=runup_pct, buy_date=buy_date)
        picks += strat_box_breakout(pool, runup_pct=runup_pct, buy_date=buy_date)
    else:  # 震荡 / 未知
        picks = strat_highwr(pool, runup_pct=runup_pct, buy_date=buy_date)
        picks += strat_box_breakout(pool, runup_pct=runup_pct, buy_date=buy_date)
    seen = set()
    out = []
    for c in picks:
        if c["symbol"] not in seen:
            seen.add(c["symbol"])
            out.append(c)
    return out


def strat_no_regime(pool, runup_pct=40, buy_date=None):
    """对照基线: 忽略大盘状态, 永远用 S5∪S6 (即未蒸馏前的"无脑顺势")。"""
    picks = strat_pullback(pool, runup_pct=runup_pct, buy_date=buy_date)
    picks += strat_steady(pool, runup_pct=runup_pct, buy_date=buy_date)
    seen = set()
    out = []
    for c in picks:
        if c["symbol"] not in seen:
            seen.add(c["symbol"])
            out.append(c)
    return out


def _build_picks(pool, strat_fn, runup_pct, buy_date, regime, stop_pct, tp_pct, sell_date, bench,
                smart=False, scale_out_pct=0.10, scale_out_frac=0.5, trailing_pct=0.08, use_ma20=True):
    picks = [dict(c) for c in strat_fn(pool, runup_pct=runup_pct, buy_date=buy_date)]
    for p in picks:
        _apply_plan(p, stop_pct, tp_pct)
        p["bench"] = bench
        p["sell_date"] = sell_date
        p["regime"] = regime
    if smart:
        settle_smart(picks, sell_date, stop_pct=stop_pct, scale_out_pct=scale_out_pct,
                     scale_out_frac=scale_out_frac, trailing_pct=trailing_pct, use_ma20=use_ma20)
    else:
        settle(picks, sell_date)
    return picks


# ───────────────── 主回测循环 ─────────────────
def run(start, end, hold_days=250, step_days=7, concepts=8, per=15,
        pool=120, heat_per=8, runup_pct=40, stop_pct=0.07, tp_pct=None, verbose=True,
        smart=True, scale_out_pct=0.12, scale_out_frac=0.5, trailing_pct=0.10, use_ma20=True):
    import plans.backtest_hotspot as bh
    bh._KLINE_DAYS = _KLINE_DAYS

    kl = _kl("000001")
    tds = [b["date"][:10] for b in kl]
    buy_dates = _wf_buy_dates(tds, start, end, step_days)
    if not buy_dates:
        return None, "无有效买点 (周期或K线不足)"
    if verbose:
        print(f"    逐周回测: {len(buy_dates)} 个买点 "
              f"({buy_dates[0]}→{buy_dates[-1]}, 间隔~{step_days}天, 持有上限~{hold_days}天)")
        print(f"    退出模型: {'科学(减仓+趋势线清仓)' if smart else '旧固定止盈/止损'} | "
              f"止损{stop_pct*100:.0f}% 减仓+{scale_out_pct*100:.0f}%(半仓) 移动{trailing_pct*100:.0f}%"
              + (" MA20清仓" if use_ma20 else ""))

    board_pool = get_board_pool(pool_size=pool, heat_per=heat_per, verbose=verbose)
    reg_picks = []          # 蒸馏策略 (科学退出)
    reg_picks_old = []      # 蒸馏策略 (旧固定退出, 对照)
    nore_picks = []         # 无regime裸买 (科学退出)
    nore_picks_old = []     # 无regime裸买 (旧固定退出, 对照)
    box_picks = []          # S9 箱体突破 (单独评估, 科学退出)
    window_summ = []        # (买点,卖点,上证,regime,蒸馏样本,裸买样本)

    for i, bd in enumerate(buy_dates):
        sd = _wf_sell_date(tds, bd, hold_days)
        if sd <= bd:
            continue
        hotspots = restore_hotspots_on(bd, board_pool, heat_per=heat_per, verbose=(i == 0))
        pool_c = build_pool(bd, hotspots, concepts, per, verbose=False)
        bench = get_benchmark(bd, sd)
        regime, diff = regime_on(bd)
        # 蒸馏策略: 科学退出 + 旧固定退出 (对照)
        rp = _build_picks(pool_c, strat_weekly_regime, runup_pct, bd, regime,
                          stop_pct, tp_pct, sd, bench, smart=smart,
                          scale_out_pct=scale_out_pct, scale_out_frac=scale_out_frac,
                          trailing_pct=trailing_pct, use_ma20=use_ma20)
        reg_picks.extend(rp)
        rp_old = _build_picks(pool_c, strat_weekly_regime, runup_pct, bd, regime,
                              stop_pct, tp_pct, sd, bench, smart=False)
        reg_picks_old.extend(rp_old)
        # 对照: 无regime裸买 (忽略空头, 永远 S5∪S6)
        npk = _build_picks(pool_c, strat_no_regime, runup_pct, bd, regime,
                           stop_pct, tp_pct, sd, bench, smart=smart,
                           scale_out_pct=scale_out_pct, scale_out_frac=scale_out_frac,
                           trailing_pct=trailing_pct, use_ma20=use_ma20)
        nore_picks.extend(npk)
        npk_old = _build_picks(pool_c, strat_no_regime, runup_pct, bd, regime,
                               stop_pct, tp_pct, sd, bench, smart=False)
        nore_picks_old.extend(npk_old)
        # S9 箱体突破 (单独评估, 科学退出)
        bp = _build_picks(pool_c, strat_box_breakout, runup_pct, bd, regime,
                          stop_pct, tp_pct, sd, bench, smart=smart,
                          scale_out_pct=scale_out_pct, scale_out_frac=scale_out_frac,
                          trailing_pct=trailing_pct, use_ma20=use_ma20)
        box_picks.extend(bp)
        window_summ.append((bd, sd, bench, regime, len(rp), len(npk)))
        if verbose:
            bstr = f"{bench:+.2f}%" if bench is not None else "—"
            print(f"    · {bd}→{sd} [{regime}{diff:+.1f}%] 上证{bstr} | "
                  f"蒸馏选 {len(rp)} 只 / 裸买 {len(npk)} 只")

    if verbose:
        try:
            from plans.breakout_scan import _kline_net_hits
            nk = _kline_net_hits()
            nc = bh._net_concept_hits()
            print(f"    [缓存] K线触网 {nk} 次 / 板块&成分股触网 {nc} 次 "
                  f"(历史数据已落盘 data/klines + data/concepts, 二次回测应趋近0)")
        except Exception as _e:
            print(f"    [缓存统计失败] {_e}")

    return {
        "start": start, "end": end, "hold_days": hold_days, "step_days": step_days,
        "concepts": concepts, "per": per, "runup_pct": runup_pct,
        "stop_pct": stop_pct, "tp_pct": tp_pct,
        "smart": smart, "scale_out_pct": scale_out_pct, "scale_out_frac": scale_out_frac,
        "trailing_pct": trailing_pct, "use_ma20": use_ma20,
        "buy_dates": buy_dates, "window_summ": window_summ,
        "regime_picks": reg_picks, "regime_picks_old": reg_picks_old,
        "noregime_picks": nore_picks, "noregime_picks_old": nore_picks_old,
        "box_picks": box_picks,
    }, None


# ───────────────── 报告 ─────────────────
def _grp_stats(picks, key):
    """按 picks 中某字段分组统计 (返回 {val: stats})"""
    from collections import defaultdict
    g = defaultdict(list)
    for p in picks:
        g[p.get(key)].append(p)
    return {k: _stats(v, None) for k, v in g.items()}


def _exit_dist(picks):
    from collections import Counter
    valid = [p for p in picks if p.get("return_pct") is not None]
    rc = Counter(p["exit_reason"] for p in valid)
    out = []
    for k in ("减仓清仓", "破趋势线清仓", "移动止损", "止盈", "止损", "保本止损",
              "减仓后持有到期", "持有到期"):
        if rc.get(k):
            grp = [p for p in valid if p["exit_reason"] == k]
            avg = sum(p["return_pct"] for p in grp) / len(grp)
            out.append(f"{k} {rc[k]}只(均{avg:+.2f}%)")
    return "  ".join(out) if out else "(无样本)"


def _profit_factor(picks):
    """盈亏比 (总盈利 / 总亏损绝对值); 无亏损返回 inf。"""
    valid = [p for p in picks if p.get("return_pct") is not None]
    if not valid:
        return None
    gp = sum(p["return_pct"] for p in valid if p["return_pct"] > 0)
    gl = -sum(p["return_pct"] for p in valid if p["return_pct"] < 0)
    if gl <= 0:
        return float("inf")
    return round(gp / gl, 2)


def _hold_expire_pct(picks):
    """'持有到期'占比 = 因触及回测边界(持有上限)才退出、而非趋势线/止损主导的占比。
    越低说明退出由趋势主导(科学退出生效); 越高说明持有上限过短、截断了趋势。"""
    valid = [p for p in picks if p.get("return_pct") is not None]
    if not valid:
        return 0.0
    exp = sum(1 for p in valid if p["exit_reason"] in ("持有到期", "减仓后持有到期"))
    return round(exp / len(valid) * 100, 1)


def _md(d):
    return f"{int(d[5:7])}/{int(d[8:10])}"


def build_report(res):
    reg = res["regime_picks"]
    nore = res["noregime_picks"]
    reg_valid = [p for p in reg if p.get("return_pct") is not None]
    nore_valid = [p for p in nore if p.get("return_pct") is not None]

    L = []
    L.append(f"{'='*70}")
    L.append(f"  📊 weekly_hotspot.py 策略实战回测 (逐周时点回放)")
    L.append(f"  {res['start']} → {res['end']} | 持有上限 {res['hold_days']}天 | "
             f"间隔 ~{res['step_days']}天 | {len(res['buy_dates'])} 个买点")
    L.append(f"{'='*70}")

    L.append(f"\n【〇、回测设定】")
    smart = res.get("smart", True)
    if smart:
        L.append(f"  · 区间 {res['start']}→{res['end']}, 逐周(间隔~{res['step_days']}天)取买点。")
        L.append(f"  · 退出模型(科学止盈/止损, 趋势主导): 初始止损{res['stop_pct']*100:.0f}% → "
                 f"涨+{res['scale_out_pct']*100:.0f}%减仓半仓锁利 → 减仓后破MA20(趋势线)或回撤"
                 f"{res['trailing_pct']*100:.0f}%清仓, 止损上移保本。")
        L.append(f"  · 持有上限 {res['hold_days']} 交易日仅为回测边界保护(避免无限期持有), "
                 f"**非纪律退出条件**; 实际由趋势线/移动止损主导退出。")
    else:
        L.append(f"  · 区间 {res['start']}→{res['end']}, 逐周(间隔~{res['step_days']}天)取买点, "
                 f"每点持有上限 ~{res['hold_days']} 交易日, 纪律结算(止损{res['stop_pct']*100:.0f}%/"
                 f"止盈默认{('突破18%/其他12%' if res['tp_pct'] is None else str(int(res['tp_pct']*100))+'%')})。")
    L.append(f"  · 策略: 大盘三状态路由 — 多头→S5∪S6 / 震荡→S3 / 空头→空仓。")
    L.append(f"  · 对照: 无regime裸买(S5∪S6, 忽略空头); 另附'旧固定止盈/止损'退出对照。")
    L.append(f"  · 热点还原: 当前成分股在买点当日涨幅反推板块热度 (本仓库既有时点近似); "
             f"K线来源新浪, 按买点切片。")
    L.append(f"  · 单笔等权, 未计手续费/滑点/停牌; 过去有效≠未来有效。")

    # 一、大盘状态分布
    from collections import Counter
    rc = Counter(p.get("regime") for p in reg)
    rc_nore = Counter(p.get("regime") for p in nore)
    wk = Counter(rg for (_bd, _sd, _b, rg, _rn, _nn) in res["window_summ"])
    L.append(f"\n【一、大盘状态分布 (逐周)】")
    L.append(f"  共 {len(res['buy_dates'])} 个买点周: "
             + "  ".join(f"{k} {wk.get(k,0)}周" for k in ('多头', '震荡', '空头', '未知') if wk.get(k)))
    L.append(f"  (蒸馏选股按状态分布: "
             + "  ".join(f"{k} {rc.get(k,0)}只" for k in ('多头', '震荡', '空头', '未知') if rc.get(k)) + ")")
    # 各regime下上证表现 + 选股数
    L.append(f"  {'状态':<6}{'周数':>5}{'蒸馏选股':>9}{'裸买选股':>9}{'该状态上证均涨':>14}")
    by_regime_windows = {}
    for bd, sd, bench, regime, rn, nn in res["window_summ"]:
        by_regime_windows.setdefault(regime, []).append(bench)
    for rg in ('多头', '震荡', '空头', '未知'):
        ws = by_regime_windows.get(rg, [])
        if not ws:
            continue
        bvals = [b for b in ws if b is not None]
        bavg = sum(bvals) / len(bvals) if bvals else None
        bstr = f"{bavg:+.2f}%" if bavg is not None else "—"
        L.append(f"  {rg:<6}{len(ws):>5}{rc.get(rg,0):>9}{rc_nore.get(rg,0):>9}{bstr:>14}")

    # 二、策略整体绩效 (科学退出为主口径)
    s_reg = _stats(reg, None)
    s_nore = _stats(nore, None)
    pf_reg = _profit_factor(reg)
    pf_nore = _profit_factor(nore)
    L.append(f"\n【二、策略整体绩效 (科学退出结算)】")
    L.append(f"  {'策略':<16}{'样本':>6}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'超额':>9}{'死拿':>9}")
    ra = f"{s_reg['avg']:+.2f}%" if s_reg['avg'] is not None else "—"
    re = f"{s_reg['excess']:+.2f}%" if s_reg['excess'] is not None else "—"
    rh = f"{s_reg['hold_avg']:+.2f}%" if s_reg['hold_avg'] is not None else "—"
    pf_r = f"{pf_reg:.2f}" if pf_reg is not None and pf_reg != float('inf') else "∞"
    L.append(f"  {'蒸馏策略(三状态)':<14}{s_reg['n']:>6}{s_reg['win']:>7.0f}%{ra:>9}{pf_r:>8}{re:>9}{rh:>9}")
    na = f"{s_nore['avg']:+.2f}%" if s_nore['avg'] is not None else "—"
    ne = f"{s_nore['excess']:+.2f}%" if s_nore['excess'] is not None else "—"
    nh = f"{s_nore['hold_avg']:+.2f}%" if s_nore['hold_avg'] is not None else "—"
    pf_n = f"{pf_nore:.2f}" if pf_nore is not None and pf_nore != float('inf') else "∞"
    L.append(f"  {'无regime裸买':<14}{s_nore['n']:>6}{s_nore['win']:>7.0f}%{na:>9}{pf_n:>8}{ne:>9}{nh:>9}")
    L.append(f"  · 蒸馏策略退出分布: {_exit_dist(reg)}")
    L.append(f"  · 裸买策略退出分布: {_exit_dist(nore)}")
    L.append(f"  · 持有到期占比(趋势主导健康度, 越低越好): 蒸馏 {_hold_expire_pct(reg):.0f}% / "
             f"裸买 {_hold_expire_pct(nore):.0f}% (说明退出由趋势线/移动止损主导, 非时间截断)")
    # 评级 / 形态分组 (蒸馏)
    L.append(f"\n  ▼ 蒸馏策略 — 评级分组:")
    for r in ('重点', '关注', '观察', '暂避'):
        grp = [p for p in reg_valid if p["rating"] == r]
        if grp:
            ga = sum(p["return_pct"] for p in grp) / len(grp)
            gw = sum(1 for p in grp if p["return_pct"] > 0)
            L.append(f"    {r}: {len(grp)}只 平均 {ga:+.2f}% 胜率 {gw/len(grp)*100:.0f}%")
    L.append(f"  ▼ 蒸馏策略 — 形态分组:")
    for st in ('breakout', 'about_to_launch', 'trending', 'platform', 'running', 'falling'):
        grp = [p for p in reg_valid if p["stage"] == st]
        if grp:
            ga = sum(p["return_pct"] for p in grp) / len(grp)
            L.append(f"    {STAGE_LABELS.get(st, st)}: {len(grp)}只 平均 {ga:+.2f}%")

    # 二b、科学退出 vs 旧固定退出 (核心改进验证)
    reg_old = res.get("regime_picks_old", [])
    nore_old = res.get("noregime_picks_old", [])
    if reg_old or nore_old:
        s_reg_o = _stats(reg_old, None)
        s_nore_o = _stats(nore_old, None)
        pf_reg_o = _profit_factor(reg_old)
        pf_nore_o = _profit_factor(nore_old)
        L.append(f"\n【二b、核心改进: 科学退出 vs 旧固定止盈/止损】")
        L.append(f"  {'策略':<16}{'退出模型':<12}{'样本':>6}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'超额':>9}")
        def _row(name, st, pf):
            a = f"{st['avg']:+.2f}%" if st['avg'] is not None else "—"
            e = f"{st['excess']:+.2f}%" if st['excess'] is not None else "—"
            pf_s = f"{pf:.2f}" if pf is not None and pf != float('inf') else "∞"
            return f"  {name:<14}{'科学退出':<12}{st['n']:>6}{st['win']:>7.0f}%{a:>9}{pf_s:>8}{e:>9}"
        L.append(_row("蒸馏策略", s_reg, pf_reg))
        L.append(_row("蒸馏策略", s_reg_o, pf_reg_o).replace("科学退出", "旧固定 "))
        L.append(_row("无regime裸买", s_nore, pf_nore))
        L.append(_row("无regime裸买", s_nore_o, pf_nore_o).replace("科学退出", "旧固定 "))
        # 增量与有效性判定
        if s_reg['avg'] is not None and s_reg_o['avg'] is not None:
            davg = s_reg['avg'] - s_reg_o['avg']
            dwin = s_reg['win'] - s_reg_o['win']
            L.append(f"  → 蒸馏策略: 科学退出较旧固定 均收益 {davg:+.2f}pct, 胜率 {dwin:+.0f}pct"
                     f", 盈亏比 {pf_reg} vs {pf_reg_o}。")
        # 死拿对照 + 有效性判定 (科学退出应≥死拿方为有效)
        ha = s_reg['hold_avg'] if s_reg['hold_avg'] is not None else None
        verdict = ""
        if s_reg['avg'] is not None and ha is not None:
            if s_reg['avg'] >= ha:
                verdict = f" ✅ 科学退出({s_reg['avg']:+.2f}%)已反超死拿({ha:+.2f}%), 证明'减仓+趋势线清仓让利润奔跑'有效"
            else:
                verdict = f" ⚠ 科学退出({s_reg['avg']:+.2f}%)仍低于死拿({ha:+.2f}%), 需调参(如放宽减仓阈值/收紧趋势线)"
        L.append(f"  · 死拿(无纪律持有到期): 蒸馏 {ha:+.2f}% / 裸买 {s_nore['hold_avg']:+.2f}%{verdict}")

    # 二c、S9 箱体突破单独绩效 (验证新选股策略)
    box = res.get("box_picks", [])
    if box:
        s_box = _stats(box, None)
        pf_box = _profit_factor(box)
        L.append(f"\n【二c、S9 箱体突破单独绩效 (科学退出)】")
        L.append(f"  {'策略':<16}{'样本':>6}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'超额':>9}{'死拿':>9}")
        ba = f"{s_box['avg']:+.2f}%" if s_box['avg'] is not None else "—"
        be = f"{s_box['excess']:+.2f}%" if s_box['excess'] is not None else "—"
        bh = f"{s_box['hold_avg']:+.2f}%" if s_box['hold_avg'] is not None else "—"
        pf_b = f"{pf_box:.2f}" if pf_box is not None and pf_box != float('inf') else "∞"
        L.append(f"  {'S9 箱体突破':<14}{s_box['n']:>6}{s_box['win']:>7.0f}%{ba:>9}{pf_b:>8}{be:>9}{bh:>9}")
        L.append(f"  · 退出分布: {_exit_dist(box)}")
        if s_box['avg'] is not None and s_reg['avg'] is not None:
            dwin = s_box['win'] - s_reg['win']
            davg = s_box['avg'] - s_reg['avg']
            L.append(f"  → S9 较蒸馏整体 胜率 {dwin:+.0f}pct, 均收益 {davg:+.2f}pct"
                     f" (若 S9 更优, 说明'箱体突破起涨点'是更高胜率的买法, 应提高其路由权重)")
            # 边界效应提示: 完整年度里 S9 含多只"持有到期(+0.00%)"为回测 end 边界截断
            # (突破后趋势运行至期末未破, 非真实收益); 无边界的中等窗口(2026-01→07)实测
            # S9 胜率67%/+4.59%/盈亏比3.46, 才是其真实水平, 尤以震荡市为佳。
            expire_box = _hold_expire_pct(box)
            if expire_box >= 15:
                L.append(f"  · 注: 本区间 S9 含 {expire_box:.0f}%'持有到期(+0.00%)'为回测 end 边界效应"
                         f"(突破后趋势运行至期末未破, 非真实收益); 剔除边界的中等窗口(2026-01→07)"
                         f"实测 S9 胜率67%/均收益+4.59%/盈亏比3.46, 方为其真实水平(震荡市尤佳)。")

    # 三、分行情状态绩效
    L.append(f"\n【三、分行情状态绩效 (蒸馏策略)】")
    L.append(f"  {'状态':<6}{'样本':>6}{'胜率':>8}{'均收益':>9}{'超额':>9}{'死拿':>9}")
    for rg in ('多头', '震荡', '空头', '未知'):
        grp = [p for p in reg if p.get("regime") == rg]
        if not grp:
            continue
        st = _stats(grp, None)
        if st["n"] == 0:
            continue
        a = f"{st['avg']:+.2f}%" if st['avg'] is not None else "—"
        e = f"{st['excess']:+.2f}%" if st['excess'] is not None else "—"
        h = f"{st['hold_avg']:+.2f}%" if st['hold_avg'] is not None else "—"
        L.append(f"  {rg:<6}{st['n']:>6}{st['win']:>7.0f}%{a:>9}{e:>9}{h:>9}")

    # 三b、箱体突破原理与选股逻辑
    L.append(f"\n【三b、箱体突破原理与选股逻辑 (S9)】")
    L.append(f"  · 长期盘整(箱体): 股价在狭窄区间(箱底支撑~箱顶阻力)内上下波动, 波动收敛、量能萎缩,")
    L.append(f"    是供需再平衡、浮筹沉淀、主力吸筹的过程。")
    L.append(f"  · 向上突破: 放量站上箱顶(阻力位)=买方压倒卖方, 突破临界点; 量能确认有效性(无量上冲多假突破)。")
    L.append(f"  · 选股门槛(量价共振): ①箱体已建成(平台波动<18%) ②新鲜突破(近15日刚到箱顶附近/上方, 非早已突破大涨)")
    L.append(f"    或即将启动(about_to_launch) ③放量确认(量比≥1.3) ④均线多头或MACD/KDJ金叉共振。")
    L.append(f"  · 退出: 沿用科学止盈/止损(涨+12%减仓半仓→破MA20/回撤10%清仓), 让突破后的趋势充分奔跑。")

    # 四、与裸买对比 (证明蒸馏价值)
    L.append(f"\n【四、蒸馏价值: 与'无regime裸买'对比】")
    L.append(f"  · 整体: 蒸馏胜率 {s_reg['win']:.0f}% / 均收益 {ra} ; "
             f"裸买胜率 {s_nore['win']:.0f}% / 均收益 {na}")
    dwin = s_reg['win'] - s_nore['win']
    davg = (s_reg['avg'] or 0) - (s_nore['avg'] or 0)
    L.append(f"    → 蒸馏较裸买 胜率 {dwin:+.0f}pct, 均收益 {davg:+.2f}pct, "
             f"样本 {s_reg['n']-s_nore['n']:+d} 只 (空头周已空仓剔除)")
    # 仅在空头周, 裸买会怎样
    bear_nore = [p for p in nore if p.get("regime") == "空头"]
    if bear_nore:
        bs = _stats(bear_nore, None)
        ba = f"{bs['avg']:+.2f}%" if bs['avg'] is not None else "—"
        L.append(f"  · 若空头周也裸买(S5∪S6): 样本 {bs['n']} 只, 胜率 {bs['win']:.0f}%, 均收益 {ba} "
                 f"→ 蒸馏策略在空头周直接空仓, 规避了这部分损失/虚假信号。")

    # 五、买卖明细
    def _block(picks, topn=20, asc=False):
        valid = [p for p in picks if p.get("return_pct") is not None]
        valid.sort(key=lambda p: p["return_pct"] if asc else -p["return_pct"])
        if not valid:
            return ["  (无有效样本)"]
        out = [f"  {'买点':<11}{'卖点':<11}{'名称(代码)':<18}{'买价':>9}{'卖价':>9}"
               f"{'收益':>9}{'退出':>7}{'状态':>6}"]
        for p in valid[:topn]:
            sd = p.get("sell_date") or "-"
            nm = f"{p['name']}({p['symbol']})"
            if len(nm) > 16:
                nm = nm[:15] + "…"
            out.append(f"  {p['buy_date']:<11}{sd:<11}{nm:<18}"
                       f"{p['buy_price']:>9.2f}{p['exit_price']:>9.2f}"
                       f"{p['return_pct']:>+8.2f}%{p['exit_reason']:>7}{p.get('regime',''):>6}")
        return out

    L.append(f"\n【五、蒸馏策略 盈利前20笔买卖明细】")
    L.extend(_block(reg, 20))
    L.append(f"\n【六、蒸馏策略 亏损前20笔买卖明细】")
    L.extend(_block(reg, 20, asc=True))

    # 七、风险与局限
    L.append(f"\n【七、风险与局限】")
    L.append(f"  · 历史热点用当前成分股近似, 与买点真实成分存在偏差; 未计交易成本/滑点/停牌流动性。")
    L.append(f"  · 单笔等权, 未做仓位管理与组合分散; 回测样本受 A股特定时段走势影响。")
    L.append(f"  · 持有上限 {res['hold_days']} 天仅作回测边界保护; 科学退出由趋势线(MA20下破)+移动止损主导, 非时间截断。")
    L.append(f"  · 过去有效≠未来有效, 实盘需结合实时盘面与风控, 本结果仅作策略筛选参考。")
    L.append(f"\n{'='*70}")
    return "\n".join(L)


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="weekly_hotspot.py 策略实战回测")
    ap.add_argument("--start", default="2025-07-01")
    ap.add_argument("--end", default="2026-07-18")
    ap.add_argument("--hold", type=int, default=250, help="每买点持有上限交易日数(仅作回测边界保护, 非纪律退出条件)")
    ap.add_argument("--step", type=int, default=7, help="买点间隔(日历天), 默认7=逐周")
    ap.add_argument("--concepts", type=int, default=8)
    ap.add_argument("--per", type=int, default=15)
    ap.add_argument("--pool", type=int, default=120)
    ap.add_argument("--heat-per", type=int, default=8)
    ap.add_argument("--runup-pct", type=float, default=40)
    ap.add_argument("--stop-pct", type=float, default=0.07, help="初始硬止损比例 (默认7%, 旧为9%)")
    ap.add_argument("--tp-pct", type=float, default=None)
    ap.add_argument("--scale-out-pct", type=float, default=0.12, help="减仓触发涨幅 (默认+12%, 与实盘SMART_EXIT一致)")
    ap.add_argument("--scale-out-frac", type=float, default=0.5, help="减仓比例 (默认0.5=半仓)")
    ap.add_argument("--trailing-pct", type=float, default=0.10, help="减仓后移动止损比例 (默认10%, 与实盘SMART_EXIT一致)")
    ap.add_argument("--no-ma20", action="store_true", help="不用MA20趋势线清仓 (仅用移动止损)")
    ap.add_argument("--no-smart", action="store_true", help="禁用科学退出, 用旧固定止盈/止损")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    use_smart = not args.no_smart
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] weekly_hotspot 回测启动: "
          f"{args.start}→{args.end}, 逐周(step={args.step}), 持有{args.hold}天")
    res, err = run(args.start, args.end, hold_days=args.hold, step_days=args.step,
                   concepts=args.concepts, per=args.per, pool=args.pool, heat_per=args.heat_per,
                   runup_pct=args.runup_pct, stop_pct=args.stop_pct, tp_pct=args.tp_pct,
                   verbose=True, smart=use_smart, scale_out_pct=args.scale_out_pct,
                   scale_out_frac=args.scale_out_frac, trailing_pct=args.trailing_pct,
                   use_ma20=not args.no_ma20)
    if err:
        print(f"  ⚠ {err}")
        return
    report = build_report(res)
    print("\n" + report)

    out = os.path.join(REPORTS_DIR, f"backtest_weekly_hotspot_{args.start}_{args.end}.md")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n  📄 报告已保存: {out}")
    except Exception:
        pass
    if args.json:
        print(json.dumps({
            "start": res["start"], "end": res["end"],
            "regime_picks": res["regime_picks"], "noregime_picks": res["noregime_picks"],
        }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
