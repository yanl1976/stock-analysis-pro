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

from plans.backtest_hotspot import (
    restore_hotspots_on, get_board_pool, build_pool, settle, get_benchmark,
    _kl, kline_upto, _wf_buy_dates, _wf_sell_date, _stats,
    strat_pullback, strat_steady, strat_highwr, _apply_plan, STAGE_LABELS,
)
from plans.weekly_hotspot import REGIME_STRATEGY

_KLINE_DAYS = 900  # 拉足 K线以覆盖 2025-07 之前的 MA60


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

    多头 → S5回调低吸 ∪ S6强趋势低波动
    震荡 → S3高胜率共振
    空头 → 空仓 (返回空)
    """
    regime, _ = regime_on(buy_date)
    if regime == "空头":
        return []
    if regime == "多头":
        picks = strat_pullback(pool, runup_pct=runup_pct, buy_date=buy_date)
        picks += strat_steady(pool, runup_pct=runup_pct, buy_date=buy_date)
    else:  # 震荡 / 未知
        picks = strat_highwr(pool, runup_pct=runup_pct, buy_date=buy_date)
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


def _build_picks(pool, strat_fn, runup_pct, buy_date, regime, stop_pct, tp_pct, sell_date, bench):
    picks = [dict(c) for c in strat_fn(pool, runup_pct=runup_pct, buy_date=buy_date)]
    for p in picks:
        _apply_plan(p, stop_pct, tp_pct)
        p["bench"] = bench
        p["sell_date"] = sell_date
        p["regime"] = regime
    settle(picks, sell_date)
    return picks


# ───────────────── 主回测循环 ─────────────────
def run(start, end, hold_days=40, step_days=7, concepts=8, per=15,
        pool=120, heat_per=8, runup_pct=40, stop_pct=0.09, tp_pct=None, verbose=True):
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

    board_pool = get_board_pool(pool_size=pool, heat_per=heat_per, verbose=verbose)
    reg_picks = []      # weekly_hotspot 蒸馏策略
    nore_picks = []     # 无regime裸买对照
    window_summ = []    # (买点,卖点,上证,regime,蒸馏样本,裸买样本)

    for i, bd in enumerate(buy_dates):
        sd = _wf_sell_date(tds, bd, hold_days)
        if sd <= bd:
            continue
        hotspots = restore_hotspots_on(bd, board_pool, heat_per=heat_per, verbose=(i == 0))
        pool_c = build_pool(bd, hotspots, concepts, per, verbose=False)
        bench = get_benchmark(bd, sd)
        regime, diff = regime_on(bd)
        # 蒸馏策略
        rp = _build_picks(pool_c, strat_weekly_regime, runup_pct, bd, regime,
                          stop_pct, tp_pct, sd, bench)
        reg_picks.extend(rp)
        # 对照: 无regime裸买 (忽略空头, 永远 S5∪S6)
        npk = _build_picks(pool_c, strat_no_regime, runup_pct, bd, regime,
                           stop_pct, tp_pct, sd, bench)
        nore_picks.extend(npk)
        window_summ.append((bd, sd, bench, regime, len(rp), len(npk)))
        if verbose:
            bstr = f"{bench:+.2f}%" if bench is not None else "—"
            print(f"    · {bd}→{sd} [{regime}{diff:+.1f}%] 上证{bstr} | "
                  f"蒸馏选 {len(rp)} 只 / 裸买 {len(npk)} 只")

    return {
        "start": start, "end": end, "hold_days": hold_days, "step_days": step_days,
        "concepts": concepts, "per": per, "runup_pct": runup_pct,
        "stop_pct": stop_pct, "tp_pct": tp_pct,
        "buy_dates": buy_dates, "window_summ": window_summ,
        "regime_picks": reg_picks, "noregime_picks": nore_picks,
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
    for k in ("止盈", "止损", "移动止损", "持有到期"):
        if rc.get(k):
            grp = [p for p in valid if p["exit_reason"] == k]
            avg = sum(p["return_pct"] for p in grp) / len(grp)
            out.append(f"{k} {rc[k]}只(均{avg:+.2f}%)")
    return "  ".join(out) if out else "(无样本)"


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
    L.append(f"  · 区间 {res['start']}→{res['end']}, 逐周(间隔~{res['step_days']}天)取买点, "
             f"每点持有上限 ~{res['hold_days']} 交易日, 纪律结算(止损{res['stop_pct']*100:.0f}%/"
             f"止盈默认{('突破18%/其他12%' if res['tp_pct'] is None else str(int(res['tp_pct']*100))+'%')})。")
    L.append(f"  · 策略: 大盘三状态路由 — 多头→S5∪S6 / 震荡→S3 / 空头→空仓。")
    L.append(f"  · 对照: 无regime裸买(S5∪S6, 忽略空头)。")
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

    # 二、策略整体绩效
    s_reg = _stats(reg, None)
    s_nore = _stats(nore, None)
    L.append(f"\n【二、策略整体绩效 (纪律结算)】")
    L.append(f"  {'策略':<16}{'样本':>6}{'胜率':>8}{'均收益':>9}{'超额':>9}{'死拿':>9}")
    ra = f"{s_reg['avg']:+.2f}%" if s_reg['avg'] is not None else "—"
    re = f"{s_reg['excess']:+.2f}%" if s_reg['excess'] is not None else "—"
    rh = f"{s_reg['hold_avg']:+.2f}%" if s_reg['hold_avg'] is not None else "—"
    L.append(f"  {'蒸馏策略(三状态)':<14}{s_reg['n']:>6}{s_reg['win']:>7.0f}%{ra:>9}{re:>9}{rh:>9}")
    na = f"{s_nore['avg']:+.2f}%" if s_nore['avg'] is not None else "—"
    ne = f"{s_nore['excess']:+.2f}%" if s_nore['excess'] is not None else "—"
    nh = f"{s_nore['hold_avg']:+.2f}%" if s_nore['hold_avg'] is not None else "—"
    L.append(f"  {'无regime裸买':<14}{s_nore['n']:>6}{s_nore['win']:>7.0f}%{na:>9}{ne:>9}{nh:>9}")
    L.append(f"  · 蒸馏策略退出分布: {_exit_dist(reg)}")
    L.append(f"  · 裸买策略退出分布: {_exit_dist(nore)}")
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
    L.append(f"  · 持有上限 {res['hold_days']} 天, 实际纪律为'触止损/目标即退出', 长持由上限截断。")
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
    ap.add_argument("--hold", type=int, default=40, help="每买点持有上限交易日数")
    ap.add_argument("--step", type=int, default=7, help="买点间隔(日历天), 默认7=逐周")
    ap.add_argument("--concepts", type=int, default=8)
    ap.add_argument("--per", type=int, default=15)
    ap.add_argument("--pool", type=int, default=120)
    ap.add_argument("--heat-per", type=int, default=8)
    ap.add_argument("--runup-pct", type=float, default=40)
    ap.add_argument("--stop-pct", type=float, default=0.09)
    ap.add_argument("--tp-pct", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] weekly_hotspot 回测启动: "
          f"{args.start}→{args.end}, 逐周(step={args.step}), 持有{args.hold}天")
    res, err = run(args.start, args.end, hold_days=args.hold, step_days=args.step,
                   concepts=args.concepts, per=args.per, pool=args.pool, heat_per=args.heat_per,
                   runup_pct=args.runup_pct, stop_pct=args.stop_pct, tp_pct=args.tp_pct,
                   verbose=True)
    if err:
        print(f"  ⚠ {err}")
        return
    report = build_report(res)
    print("\n" + report)

    out = os.path.join(DATA_DIR, f"backtest_weekly_hotspot_{args.start}_{args.end}.md")
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
