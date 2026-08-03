# -*- coding: utf-8 -*-
"""自动 walk-forward 重蒸馏 · 参数建议脚本 (只建议, 不改代码)

目的:
  定期(如每月/每季)用最近一段历史重跑 walk-forward 回测, 按大盘三状态
  (多头/震荡/空头)拆分各策略胜率/均收益, 与当前实盘 REGIME_STRATEGY 路由对比,
  产出「参数建议报告」(markdown + JSON), 由人工审核后再决定是否修改实盘策略。
  —— 对应 OPRO/自动参数优化思路: 机器算建议; 带护栏的【自适应路由】在建议组合连续
  CONFIRM_WEEKS 周一致且样本达标时自动写回实盘(无需人工干预); 加 --no-auto 退回纯建议。

与实盘的对应关系 (plans/weekly_hotspot.py):
  多头 → S5 趋势回调低吸 ∪ S6 强趋势低波动 ∪ S9 箱体突破
  震荡 → S3 高胜率共振(金叉已放宽, 即本脚本的 S3b) ∪ S9 箱体突破, 仓位×0.6
  空头 → 空仓
  本脚本额外注册 S3b(高胜率·金叉放宽) 与回测原版 S3(严格金叉) 同场对比,
  用数据持续验证"放宽金叉"是否成立。

用法:
  python scripts/redistill.py                          # 默认最近13个月, 持有20天, 间隔20天
  python scripts/redistill.py --start 2025-06-01 --end 2026-07-01
  python scripts/redistill.py --months 6 --hold-days 15 --step-days 15
  python scripts/redistill.py --push                   # 结果摘要推送企微
  python scripts/redistill.py --sweep                  # 附加 止损/止盈/移动止损 小网格扫描(耗时↑)

输出:
  data/reports/redistill_<start>_<end>.md              # 完整建议报告
  data/redistill_suggestions.json                      # 结构化建议(供 self_check/agent 消费)

⚠ 数据口径限制(与 backtest_hotspot 相同):
  历史热点用"当前成分股在买点当日涨幅反推"近似; 板块池为该买点首跑日的实时榜快照;
  单笔等权、未计手续费。结论用于方向性参数建议, 非精确收益预测。
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import plans.backtest_hotspot as bh
from plans.weekly_hotspot import REGIME_NEUTRAL_BAND, REGIME_STRATEGY

# ── 注册 S3b: 高胜率共振·金叉放宽 (与实盘震荡分支同口径), 与原 S3(严格) 同场对比 ──
S3_NAME = "S3 高胜率共振"
S3B_NAME = "S3b 高胜率(金叉放宽)"


def _strat_highwr_relaxed(pool, runup_pct=40, buy_date=None):
    return bh.strat_highwr(pool, runup_pct=runup_pct, buy_date=buy_date, require_gc=False)


if not any(n == S3B_NAME for n, _ in bh.STRATEGIES):
    bh.STRATEGIES.append((S3B_NAME, _strat_highwr_relaxed))

# 当前实盘路由 (用于"现状 vs 建议"对比; 与 weekly_hotspot.apply_regime_strategy 一致)
LIVE_ROUTING = {
    "多头": ["S5 趋势回调低吸", "S6 强趋势低波动", "S9 箱体突破"],
    "震荡": ["S2 突破/即将启动", "S6 强趋势低波动"],
    "空头": [],
}

# ── 自适应路由(带护栏): 连续 N 周一致且样本达标 → 自动写回实盘路由配置 ──
ROUTING_CONFIG_PATH = os.path.join(DATA_DIR, "regime_routing.json")
ROUTING_STATE_PATH = os.path.join(DATA_DIR, "regime_routing_state.json")
CONFIRM_WEEKS = 3     # 建议组合连续 N 周一致才自动应用(防单次噪声过拟合)
MIN_BUY_DATES = 4     # regime 买点数门槛(防小样本误切; 空头豁免)
ADAPTIVE_DEFAULT = {
    "多头": ["S5 趋势回调低吸", "S6 强趋势低波动", "S9 箱体突破"],
    "震荡": ["S2 突破/即将启动", "S6 强趋势低波动"],
    "空头": [],
}


def _load_routing():
    try:
        with open(ROUTING_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return dict(ADAPTIVE_DEFAULT)


def _save_routing(cfg):
    with open(ROUTING_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _load_routing_state():
    try:
        with open(ROUTING_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_routing_state(st):
    with open(ROUTING_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def apply_adaptive_routing(sugg, regime_buy_counts, min_n):
    """带护栏的自适应路由: 某 regime 建议组合连续 CONFIRM_WEEKS 周一致
    且 建议组合内各策略最低样本≥min_n(空头豁免)→ 自动写回实盘路由配置。
    返回 (applied_changes: {regime: [策略名]}, new_state: dict)。

    护栏(2026-07-31 修正口径):
      · 仅当建议组合连续 N 周完全一致才应用(避免单次波动误切);
      · 门槛由"买点数≥MIN_BUY_DATES"改为"建议组合内各策略最低样本≥min_n"(空头豁免)。
        理由: 固定窗口内多头买点恒为少数(如2个), 用买点数门槛会永久锁死多头自适应;
        而策略样本数(S6=16/S7=24)已足以支撑统计判断, 故多头能公平参与自适应;
      · "空仓观望"→空组合, 若某 regime 建议转空仓(胜率崩)连续 N 周也会自动空仓。
    """
    current = _load_routing()
    state = _load_routing_state()
    applied = {}
    for rg in ("多头", "震荡", "空头"):
        s = sugg.get(rg, {})
        rec = s.get("recommended", [])
        rec_set = sorted(x for x in rec if x != "空仓观望")  # "空仓观望"→空组合
        top = {t["strategy"]: t for t in s.get("top", [])}
        min_sample = min((top[x]["n"] for x in rec_set if x in top), default=0)
        buy_n = regime_buy_counts.get(rg, 0)
        # 护栏(2026-07-31 修正): 用"建议组合内各策略最低样本≥min_n"门槛替代"买点数≥MIN_BUY_DATES",
        # 空头豁免。固定窗口内多头买点恒为少数→买点数门槛会永久锁死多头自适应, 改样本数门槛后可公平参与。
        if rg == "空头":
            # 安全护栏: 空头只允许"保持空仓"(建议明确为空仓观望时幂等写回),
            # 绝不自动转多——避免单次数据漂移(如某窗口空头样本巧合正收益)误取消空仓。
            sample_ok = (len(rec_set) == 0)
        elif not rec_set:
            sample_ok = False  # 非空仓 regime 不允许被建议为空组合(除非真有空仓信号)
        else:
            sample_ok = min_sample >= min_n
        st = state.get(rg, {"last_rec": None, "streak": 0})
        if sorted(st.get("last_rec") or []) == rec_set:
            st["streak"] = st.get("streak", 0) + 1
        else:
            st["last_rec"] = rec_set
            st["streak"] = 1
        if rec_set == sorted(current.get(rg, [])):
            pass  # 已与实盘一致, 保持 streak 作监控, 不重复写
        elif st["streak"] >= CONFIRM_WEEKS and sample_ok:
            current[rg] = rec_set
            applied[rg] = rec_set
            st["streak"] = 0  # 应用后重置(避免每周重写, 幂等无害)
        st["last_buy_n"] = buy_n
        st["last_min_sample"] = min_sample
        state[rg] = st
    _save_routing(current)
    _save_routing_state(state)
    return applied, state


def regime_on(buy_date, band=REGIME_NEUTRAL_BAND):
    """buy_date 时点的大盘三状态 (上证 MA20 vs MA60, 与实盘 market_regime 同口径)。"""
    try:
        kl = bh._kl("000001")
        kl_b = bh.kline_upto(kl, buy_date)
        closes = [b["close"] for b in kl_b if b.get("close")]
        if len(closes) < 60:
            return "未知"
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        diff = (ma20 - ma60) / ma60
        if diff > band:
            return "多头"
        if diff < -band:
            return "空头"
        return "震荡"
    except Exception:
        return "未知"


def split_by_regime(agg_results):
    """把 walk-forward 聚合逐笔按买点 regime 拆分:
    返回 {regime: {strategy: stats}}, 以及 {strategy: overall_stats}。"""
    by_regime = {}
    overall = {}
    for name, picks, st in agg_results:
        overall[name] = st
        for p in picks:
            rg = regime_on(p["buy_date"])
            by_regime.setdefault(rg, {}).setdefault(name, []).append(p)
    out = {}
    for rg, m in by_regime.items():
        out[rg] = {name: bh._stats(pk, None) for name, pk in m.items()}
    return out, overall


def rank_regime(stats_map, min_n):
    """单一 regime 内策略排名: 样本≥min_n 优先, 胜率降序、均收益次之。"""
    rows = [(name, st) for name, st in stats_map.items() if st["n"] > 0]
    qualified = [r for r in rows if r[1]["n"] >= min_n]
    pool = qualified or rows
    pool.sort(key=lambda x: (-x[1]["win"], -(x[1]["avg"] or -999)))
    return pool


def _fmt_st(st):
    avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
    ex = f"{st['excess']:+.2f}%" if st["excess"] is not None else "—"
    return f"样本{st['n']:>4} 胜率{st['win']:>3.0f}% 均收益{avg:>8} 超额{ex:>8}"


def build_suggestions(by_regime, min_n):
    """产出结构化建议: 每个 regime 的 top 策略 vs 当前实盘路由, 标注差异。"""
    sugg = {}
    for rg in ("多头", "震荡", "空头"):
        stats_map = by_regime.get(rg, {})
        ranked = rank_regime(stats_map, min_n)
        top = [{"strategy": n, "n": st["n"], "win": round(st["win"], 1),
                "avg": round(st["avg"], 2) if st["avg"] is not None else None,
                "excess": round(st["excess"], 2) if st["excess"] is not None else None}
               for n, st in ranked[:5]]
        live = LIVE_ROUTING.get(rg, [])
        # 建议路由: 取 胜率≥55% 且 均收益>0 且 样本≥min_n 的前2 (空头常无 → 维持空仓)
        rec = [n for n, st in ranked
               if st["n"] >= min_n and st["win"] >= 55 and (st["avg"] or 0) > 0][:2]
        changed = sorted(rec) != sorted([x for x in live if x in stats_map]) if rec else bool(live)
        sugg[rg] = {
            "top": top,
            "live_routing": live,
            "recommended": rec if rec else ["空仓观望"],
            "differs_from_live": changed,
        }
    # S3 严格 vs 放宽 专项对比 (震荡窗口)
    s3_cmp = {}
    osc = by_regime.get("震荡", {})
    for key, name in (("strict", S3_NAME), ("relaxed", S3B_NAME)):
        st = osc.get(name)
        if st:
            s3_cmp[key] = {"n": st["n"], "win": round(st["win"], 1),
                           "avg": round(st["avg"], 2) if st["avg"] is not None else None}
    sugg["_s3_gc_comparison"] = s3_cmp
    return sugg


def build_report(start, end, hold_days, step_days, buy_dates, window_summ,
                 by_regime, overall, sugg, min_n, sweep_lines=None,
                 applied=None, routing_state=None):
    L = []
    L.append("=" * 66)
    L.append("  🔁 walk-forward 重蒸馏 · 参数建议报告 (机器建议, 人工拍板)")
    L.append(f"  周期 {start} → {end} | 持有{hold_days}天 | 间隔~{step_days}天 | {len(buy_dates)} 买点")
    L.append(f"  生成 {datetime.now():%Y-%m-%d %H:%M}")
    L.append("=" * 66)

    # 窗口与 regime 分布
    rg_cnt = {}
    for bd in buy_dates:
        rg_cnt[regime_on(bd)] = rg_cnt.get(regime_on(bd), 0) + 1
    L.append("\n【一、买点窗口 regime 分布】")
    L.append("  " + "  ".join(f"{k}:{v}个买点" for k, v in rg_cnt.items()))

    L.append("\n【二、全策略总体聚合 (跨全部买点)】")
    for name, st in sorted(overall.items(), key=lambda x: (-x[1]["win"], -(x[1]["avg"] or -999))):
        L.append(f"  {name:<24} {_fmt_st(st)}")

    L.append("\n【三、按大盘三状态拆分 (实盘路由的依据)】")
    for rg in ("多头", "震荡", "空头", "未知"):
        stats_map = by_regime.get(rg)
        if not stats_map:
            continue
        L.append(f"\n  ▼ {rg}")
        for name, st in rank_regime(stats_map, min_n):
            mark = " ←实盘在用" if name in LIVE_ROUTING.get(rg, []) else ""
            L.append(f"    {name:<24} {_fmt_st(st)}{mark}")

    L.append("\n【四、S3 金叉门控专项: 严格 vs 放宽 (震荡窗口)】")
    cmp_ = sugg.get("_s3_gc_comparison", {})
    if cmp_.get("strict") and cmp_.get("relaxed"):
        s, r = cmp_["strict"], cmp_["relaxed"]
        L.append(f"  严格(须金叉):   样本{s['n']:>4} 胜率{s['win']:>5.1f}% 均收益{s['avg']:+.2f}%")
        L.append(f"  放宽(免金叉):   样本{r['n']:>4} 胜率{r['win']:>5.1f}% 均收益{r['avg']:+.2f}%")
        dwin = r["win"] - s["win"]
        L.append(f"  → 放宽后 样本{r['n']-s['n']:+d} / 胜率{dwin:+.1f}pct; "
                 + ("胜率下滑明显, 建议回收金叉门控" if dwin < -5 else
                    "胜率基本持平且样本增加, 放宽成立" if r["n"] > s["n"] and dwin > -5 else
                    "差异不显著, 维持观察"))
    else:
        L.append("  (震荡窗口样本不足, 无法对比 — 拉长 --months 重跑)")

    L.append("\n【五、建议 vs 实盘现状】")
    for rg in ("多头", "震荡", "空头"):
        s = sugg[rg]
        flag = "⚠ 与实盘不同" if s["differs_from_live"] else "✓ 与实盘一致"
        L.append(f"  {rg}: 实盘={s['live_routing'] or ['空仓']} → 建议={s['recommended']}  {flag}")
    L.append("  · 以上仅为数据建议; 修改实盘路由(REGIME_STRATEGY/apply_regime_strategy)需人工确认。")

    if sweep_lines:
        L.append("\n【七、止损/止盈/移动止损 小网格 (最优组合 Top10)】")
        L.extend(sweep_lines)

    L.append("\n【六、自适应路由应用记录】")
    if applied:
        L.append("  ✅ 本次自动应用了以下 regime 的实盘路由(已写回 regime_routing.json):")
        for rg, combo in applied.items():
            L.append(f"    {rg}: {'/'.join(combo)}")
    else:
        L.append(f"  · 本周无自动应用(护栏未触发: 需连续{CONFIRM_WEEKS}周一致 且 样本/买点达标)")
    L.append("  · 各 regime 连续一致周数 / 建议组合 / 最低样本 / 买点数:")
    for rg in ("多头", "震荡", "空头"):
        st = (routing_state or {}).get(rg, {})
        rec = sugg.get(rg, {}).get("recommended", [])
        L.append(f"    {rg}: 连续{st.get('streak', 0)}周 建议={'/'.join(rec)} "
                 f"最低样本{st.get('last_min_sample', '-')} 买点{st.get('last_buy_n', '-')}")

    L.append("\n【风险与局限】")
    L.append("  · 历史热点为当前成分股近似还原; 板块池为买点首跑日实时榜快照; 未计费用滑点。")
    L.append("  · 样本受周期内行情结构影响, 建议按月滚动重跑观察建议稳定性, 而非单次采纳。")
    L.append("=" * 66)
    return "\n".join(L)


def run_sweep(start, end, hold_days, step_days, args):
    """可选: 小网格扫描 止损×止盈×移动止损 (runup 固定40), 返回报告行。"""
    results, meta = bh.walk_forward_sweep(
        start, end, hold_list=(hold_days,), step_days=step_days,
        runup_list=(40,), stop_list=(0.07, 0.09, 0.12),
        tp_list=(0.12, 0.16), trailing_list=(None, 0.12),
        concepts=args.concepts, per=args.per, pool=args.pool,
        heat_per=8, min_n=args.min_n, verbose=True)
    if not results:
        return ["  (无有效结果)"]
    ranked = bh._rank_sweep(results, min_n=args.min_n)
    lines = [f"  {'策略':<24}{'止损':>5}{'止盈':>5}{'移动':>5}{'样本':>6}{'胜率':>6}{'均收益':>9}{'超额':>9}"]
    for r in ranked[:10]:
        hold, runup, stop, tp, tr = r["combo"]
        trs = f"{tr*100:.0f}%" if tr else "—"
        avg = f"{r['avg']:+.2f}%" if r["avg"] is not None else "—"
        ex = f"{r['excess']:+.2f}%" if r["excess"] is not None else "—"
        lines.append(f"  {r['strategy']:<24}{stop*100:>4.0f}%{tp*100:>4.0f}%{trs:>5}"
                     f"{r['n']:>6}{r['win']:>5.0f}%{avg:>9}{ex:>9}")
    return lines


def main():
    ap = argparse.ArgumentParser(description="walk-forward 重蒸馏参数建议 (只建议不改代码)")
    ap.add_argument("--start", default=None, help="回测起始日 (默认=今天-months)")
    ap.add_argument("--end", default=None, help="回测结束日 (默认=今天-hold_days*1.6天, 留结算期)")
    ap.add_argument("--months", type=int, default=13, help="未指定 start 时回看月数 (默认13)")
    ap.add_argument("--hold-days", type=int, default=20, help="每买点持有交易日数 (默认20)")
    ap.add_argument("--step-days", type=int, default=20, help="买点间隔日历天 (默认20)")
    ap.add_argument("--concepts", type=int, default=8)
    ap.add_argument("--per", type=int, default=15)
    ap.add_argument("--pool", type=int, default=120)
    ap.add_argument("--min-n", type=int, default=15, help="regime 内策略入选最小样本 (默认15)")
    ap.add_argument("--sweep", action="store_true", help="附加 止损/止盈 小网格扫描 (耗时↑)")
    ap.add_argument("--push", action="store_true", help="摘要推送企微")
    ap.add_argument("--no-auto", action="store_true", help="关闭自适应路由(只建议不自动应用)")
    args = ap.parse_args()

    today = datetime.now()
    end = args.end or (today - timedelta(days=int(args.hold_days * 1.6))).strftime("%Y-%m-%d")
    start = args.start or (today - timedelta(days=args.months * 30)).strftime("%Y-%m-%d")

    # K线回看覆盖 start 之前的形态识别历史
    need = int((today - datetime.strptime(start, "%Y-%m-%d")).days / 365 * 250) + 250
    if need > bh._KLINE_DAYS:
        bh._KLINE_DAYS = need

    print(f"[{today:%Y-%m-%d %H:%M}] 重蒸馏启动: {start} → {end} "
          f"(持有{args.hold_days}天, 间隔{args.step_days}天, K线回看{bh._KLINE_DAYS}天)")

    agg_results, buy_dates, window_summ, window_picks = bh.walk_forward(
        start, end, hold_days=args.hold_days, step_days=args.step_days,
        concepts=args.concepts, per=args.per, pool=args.pool,
        min_n=args.min_n, verbose=True)
    if not agg_results:
        print("  ⚠ 无有效买点/数据, 退出")
        return 1

    by_regime, overall = split_by_regime(agg_results)
    sugg = build_suggestions(by_regime, args.min_n)

    # ── 自适应路由(带护栏): 连续N周一致+样本达标 → 自动写回实盘路由 ──
    global LIVE_ROUTING
    LIVE_ROUTING = _load_routing()  # 以真实配置文件为对比基准(非硬编码副本)
    # 各 regime 买点数(护栏用)
    rg_cnt = {}
    for bd in buy_dates:
        rg_cnt[regime_on(bd)] = rg_cnt.get(regime_on(bd), 0) + 1
    applied, routing_state = ({}, {})
    if not args.no_auto:
        applied, routing_state = apply_adaptive_routing(sugg, rg_cnt, args.min_n)
        LIVE_ROUTING = _load_routing()  # 应用后重读, 反映最新实盘(供后续比对/下次运行)

    sweep_lines = run_sweep(start, end, args.hold_days, args.step_days, args) if args.sweep else None

    report = build_report(start, end, args.hold_days, args.step_days, buy_dates,
                          window_summ, by_regime, overall, sugg, args.min_n, sweep_lines,
                          applied=applied, routing_state=routing_state)
    print("\n" + report)

    out_md = os.path.join(REPORTS_DIR, f"redistill_{start}_{end}.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n  📄 建议报告已保存: {out_md}")

    out_json = os.path.join(DATA_DIR, "redistill_suggestions.json")
    payload = {
        "generated_at": today.strftime("%Y-%m-%d %H:%M:%S"),
        "period": {"start": start, "end": end,
                   "hold_days": args.hold_days, "step_days": args.step_days,
                   "n_buy_dates": len(buy_dates)},
        "suggestions": sugg,
        "applied": bool(applied),
        "applied_detail": applied,
        "routing_state": {
            rg: {"streak": routing_state.get(rg, {}).get("streak", 0),
                 "last_min_sample": routing_state.get(rg, {}).get("last_min_sample"),
                 "last_buy_n": routing_state.get(rg, {}).get("last_buy_n")}
            for rg in ("多头", "震荡", "空头")
        },
        "note": "机器建议; 带护栏自适应路由已按连续一致周数+样本达标自动应用(除非 --no-auto)。",
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  📦 结构化建议已保存: {out_json}")

    if args.push:
        try:
            from notify.wecom_bot import push_markdown_via_bot
            card = [f"## 🔁 重蒸馏参数建议 · {today:%Y-%m-%d}",
                    f"> 周期 {start}→{end} | {len(buy_dates)} 买点"]
            for rg in ("多头", "震荡", "空头"):
                s = sugg[rg]
                flag = "⚠差异" if s["differs_from_live"] else "✓一致"
                card.append(f"> **{rg}** 建议 {'/'.join(s['recommended'])} ({flag})")
            cmp_ = sugg.get("_s3_gc_comparison", {})
            if cmp_.get("strict") and cmp_.get("relaxed"):
                card.append(f"> S3金叉: 严格{cmp_['strict']['win']}% vs 放宽{cmp_['relaxed']['win']}%")
            if applied:
                card.append("> ✅ 已自动应用: " +
                            ", ".join(f"{rg}={'/'.join(c)}" for rg, c in applied.items()))
            else:
                card.append("> 本周未自动应用(护栏未触发); 详见 redistill 报告")
            push_markdown_via_bot("\n".join(card))
            print("  [AIBOT] 摘要已推送")
        except Exception as e:
            print(f"  [AIBOT] 推送失败: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
