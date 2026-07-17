# -*- coding: utf-8 -*-
"""热点选股逻辑回测 — 历史时点还原 + 区间结算

核心方法 (解决"实时榜单无法历史回溯"问题):
  实时板块榜单接口 (新浪/东财) 只能返回当前时刻, 无法直接取得 7/6 当天热点。
  但个股历史日K线含 7/6, 且板块热度可由其成分股在 7/6 的涨幅反推。
  因此:
    1. 取新浪全量概念板块 (过滤风格/地域/市值类) 作为板块池
    2. 对每个板块, 用其成分股 (当前快照, 一周内变化极小) 在 7/6 的历史涨幅
       求平均 → 板块 7/6 热度, 排序取 Top N = 7/6 热点板块
    3. 对热点板块成分股, 将K线截断到 7/6, 跑 classify_stage 选股
       (与 weekly_hotspot 完全一致的逻辑 + 形态成功率/评级/买点)
    4. 用 7/10 收盘价 vs 7/6 收盘价 结算区间收益, 验证选股/评级有效性

用法:
  python plans/backtest_hotspot.py                         # 默认 7/6 买, 7/10 卖, 8 热点, 每板 15 股
  python plans/backtest_hotspot.py --buy 2026-07-06 --sell 2026-07-10 --concepts 8 --per 15
  python plans/backtest_hotspot.py --no-score-filter       # 不过滤低分, 全量候选
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")

from collectors.concept import concept_rank_sina, fetch_concept_stocks_sina
from plans.breakout_scan import _kline_cached, _bare
from analysis.breakout import classify_stage, STAGE_LABELS
from plans.concept_analysis import filter_concepts
from plans.weekly_hotspot import _estimate_win_rate, _rating, _build_plan


# ───────────────── 历史K线工具 ─────────────────
def kline_upto(kl, date):
    """返回 date 及之前的K线 (用于 7/6 选股视角截断)"""
    return [b for b in kl if b["date"] <= date]


def price_on(kl, date):
    """取不晚于 date 的最近收盘价 (容忍停牌/缺失)"""
    for b in reversed(kl):
        if b["date"] <= date:
            return b["close"]
    return None


def change_on(kl, date):
    """取 date 当日涨跌幅 (需前一根)"""
    for i, b in enumerate(kl):
        if b["date"] == date and i > 0:
            prev = kl[i - 1]["close"]
            return (b["close"] - prev) / prev * 100 if prev else 0.0
    return None


# ───────────────── 回测主流程 ─────────────────
def restore_hotspots(buy_date, pool_size=120, heat_per=8, verbose=True):
    """还原 buy_date 当天热点板块 (成分股历史涨幅反推)

    板块池取新浪全量概念板块 (limit 调大), 而非仅当前榜单,
    以捕获 buy_date 当天领涨、但当前已冷的板块。
    heat_per: 每个板块取前 N 只成分股算 7/6 平均涨幅 (代表板块热度)。
    """
    raw = concept_rank_sina(limit=400)
    pool = filter_concepts([{
        "name": c["name"], "bk_code": c["code"],
        "change_pct": c.get("change_pct", 0),
    } for c in raw])[:pool_size]

    board_heat = {}
    for c in pool:
        try:
            stocks = fetch_concept_stocks_sina(c["bk_code"], c["name"], limit=heat_per)
        except Exception:
            continue
        chgs = []
        for s in stocks:
            sym = _bare(s["symbol"])
            try:
                kl = _kline_cached(sym)
                ch = change_on(kl, buy_date)
                if ch is not None:
                    chgs.append(ch)
            except Exception:
                continue
        if chgs:
            board_heat[c["name"]] = {
                "heat": round(sum(chgs) / len(chgs), 2),
                "up_ratio": round(sum(1 for x in chgs if x > 0) / len(chgs) * 100, 1),
                "n": len(chgs),
                "bk_code": c["bk_code"],
            }
        if verbose:
            print(f"    {c['name']}: 7/6 均涨 {board_heat.get(c['name'],{}).get('heat','-')}%")

    ranked = sorted(board_heat.items(), key=lambda x: -x[1]["heat"])
    return ranked


def _is_garbage(name):
    """过滤 ST / 退市 / 风险警示股 (本就不该入选, 且数据不可靠)"""
    u = (name or "").upper()
    return ("ST" in u) or ("退" in u) or ("*" in u)


def select_on(buy_date, hotspots, top_n, per, score_filter, verbose=True):
    """对热点板块成分股, 截K线到 buy_date 跑 classify_stage 选股"""
    picks = {}
    for name, info in hotspots[:top_n]:
        try:
            stocks = fetch_concept_stocks_sina(info["bk_code"], name, limit=per)
        except Exception:
            continue
        for s in stocks:
            if _is_garbage(s.get("name", "")):
                continue
            sym = _bare(s["symbol"])
            try:
                kl = _kline_cached(sym)
            except Exception:
                continue
            kl_b = kline_upto(kl, buy_date)
            if len(kl_b) < 40:
                continue
            price_b = price_on(kl, buy_date)
            chg_b = change_on(kl, buy_date) or 0
            if price_b is None:
                continue
            res = classify_stage(
                [b["close"] for b in kl_b],
                [b["high"] for b in kl_b],
                [b["low"] for b in kl_b],
                [b["volume"] for b in kl_b],
                price=price_b,
            )
            if score_filter and res["score"] < 45:
                continue
            if sym in picks:
                if name not in picks[sym]["concepts"]:
                    picks[sym]["concepts"].append(name)
                continue
            wr = _estimate_win_rate(res["stage"], res["signals"])
            rating = _rating(res["score"], wr, res["stage"])
            buy, stop, stop_pct, position = _build_plan(
                {"price": price_b, "stage": res["stage"], "change_pct": chg_b},
                wr, rating)
            picks[sym] = {
                "symbol": sym, "name": s.get("name") or sym,
                "price_b": price_b, "chg_b": chg_b,
                "limit_up_buy": chg_b >= 9.5,  # 7/6已涨停/接近涨停, 实际应等回踩不追
                "stage": res["stage"], "score": res["score"],
                "signals": res["signals"], "details": res["details"],
                "win_rate": wr, "rating": rating,
                "buy_point": buy, "stop_loss": stop,
                "stop_pct": stop_pct, "position": position,
                "concepts": [name],
            }
    return sorted(picks.values(), key=lambda x: -x["score"])


def settle(picks, sell_date, verbose=True):
    """用 sell_date 收盘价结算区间收益"""
    for p in picks:
        try:
            kl = _kline_cached(p["symbol"])
            p["price_s"] = price_on(kl, sell_date)
            if p["price_b"] and p["price_s"]:
                p["return_pct"] = round((p["price_s"] - p["price_b"]) / p["price_b"] * 100, 2)
            else:
                p["return_pct"] = None
        except Exception:
            p["price_s"] = None
            p["return_pct"] = None
    return picks


# ───────────────── 大盘基准 ─────────────────
def get_benchmark(buy_date, sell_date):
    """上证指数 同期涨跌幅 (基准对比)"""
    try:
        kl = _kline_cached("000001")
        pb = price_on(kl, buy_date)
        ps = price_on(kl, sell_date)
        if pb and ps:
            return round((ps - pb) / pb * 100, 2)
    except Exception:
        pass
    return None


# ───────────────── 报告 ─────────────────
def build_report(buy_date, sell_date, hotspots, top_n, picks, benchmark=None):
    valid = [p for p in picks if p["return_pct"] is not None]
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  📊 热点选股回测  (买 {buy_date} → 卖 {sell_date})")
    lines.append(f"{'='*60}")
    lines.append(f"\n【一、回测设定】")
    lines.append(f"  买点视角: {buy_date} 盘后 (K线截断到当日, 用当日收盘选股)")
    lines.append(f"  卖点视角: {sell_date} 收盘 (持有约 4 个交易日)")
    lines.append(f"  热点还原: 成分股 {buy_date} 历史涨幅反推板块热度, Top {top_n}")
    lines.append(f"  选股逻辑: classify_stage 形态识别 + 形态成功率/评级/买点 (与 weekly_hotspot 一致)")

    lines.append(f"\n【二、{buy_date} 热点板块还原 (Top {top_n})】")
    for i, (name, info) in enumerate(hotspots[:top_n], 1):
        lines.append(f"  {i}. {name}  7/6 均涨 {info['heat']:+.2f}%  上涨占比 {info['up_ratio']}% ({info['n']}只)")

    lines.append(f"\n【三、{buy_date} 选股结果 ({len(picks)} 只)】")
    for i, p in enumerate(picks[:15], 1):
        wr = f"成功率{p['win_rate']*100:.0f}%" if isinstance(p['win_rate'], (int,float)) else ""
        lines.append(f"  {i}. 【{p['rating']}】{p['name']}({p['symbol']}) "
                     f"¥{p['price_b']} {p['chg_b']:+.2f}% | {STAGE_LABELS.get(p['stage'])} "
                     f"评分{p['score']} {wr}")
        lines.append(f"     热点:{'、'.join(p['concepts'])} | {' | '.join(p['signals'][:3])}")

    lines.append(f"\n【四、{sell_date} 结算分析】")
    if not valid:
        lines.append("  (无有效结算数据)")
    else:
        for i, p in enumerate(picks[:15], 1):
            if p["return_pct"] is None:
                lines.append(f"  {i}. {p['name']}({p['symbol']}) 无结算价")
                continue
            hit = "✅" if p["return_pct"] > 0 else "❌"
            tag = " 🚫涨停不追" if p.get("limit_up_buy") else ""
            lines.append(f"  {i}. {hit}【{p['rating']}】{p['name']}({p['symbol']}) "
                         f"¥{p['price_b']}→¥{p['price_s']}  "
                         f"收益 {p['return_pct']:+.2f}%  (成功率{p['win_rate']*100:.0f}%){tag}")

    # 五、策略有效性统计
    lines.append(f"\n【五、策略有效性统计】")
    bench_str = f"{benchmark:+.2f}%" if benchmark is not None else "无数据"
    lines.append(f"  同期上证指数: {bench_str} (基准)")
    if valid:
        avg = sum(p["return_pct"] for p in valid) / len(valid)
        win = sum(1 for p in valid if p["return_pct"] > 0)
        excess = (avg - benchmark) if benchmark is not None else None
        ex_str = f" | 超额 {excess:+.2f}%" if excess is not None else ""
        lines.append(f"  有效样本 {len(valid)} 只 | 平均收益 {avg:+.2f}% | 胜率 {win/len(valid)*100:.0f}% | 跑赢大盘 {'是' if (excess is not None and excess>0) else '否'}{ex_str}")
        # 剔涨停股后的"可实际操作"收益
        tradable = [p for p in valid if not p.get("limit_up_buy")]
        if tradable:
            ta = sum(p["return_pct"] for p in tradable) / len(tradable)
            tw = sum(1 for p in tradable if p["return_pct"] > 0)
            tex = (ta - benchmark) if benchmark is not None else None
            lines.append(f"  · 剔除7/6涨停股(应等回踩不追)后 {len(tradable)}只 | 平均 {ta:+.2f}% | 胜率 {round(tw/len(tradable)*100)}%" + (f" | 超额 {tex:+.2f}%" if tex is not None else ""))
        for r in ("重点", "关注", "观察", "暂避"):
            grp = [p for p in valid if p["rating"] == r]
            if grp:
                ga = sum(p["return_pct"] for p in grp) / len(grp)
                gw = sum(1 for p in grp if p["return_pct"] > 0)
                lines.append(f"  · {r}: {len(grp)}只 平均 {ga:+.2f}% 胜率 {gw/len(grp)*100:.0f}%")
        # 形态分组
        for st in ("breakout", "trending", "platform", "about_to_launch", "running", "falling"):
            grp = [p for p in valid if p["stage"] == st]
            if grp:
                ga = sum(p["return_pct"] for p in grp) / len(grp)
                lines.append(f"  · 形态 {STAGE_LABELS.get(st, st)}: {len(grp)}只 平均 {ga:+.2f}%")
    else:
        lines.append("  (无有效样本)")
    lines.append(f"\n{'='*60}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="热点选股逻辑回测")
    ap.add_argument("--buy", default="2026-07-06", help="买点日期 (选股视角)")
    ap.add_argument("--sell", default="2026-07-10", help="卖点日期 (结算视角)")
    ap.add_argument("--concepts", type=int, default=8, help="热点板块数")
    ap.add_argument("--per", type=int, default=15, help="每板块成分股数")
    ap.add_argument("--pool", type=int, default=120, help="板块池大小 (全量概念板块候选)")
    ap.add_argument("--heat-per", type=int, default=8, help="每板块取前N只成分股算7/6热度")
    ap.add_argument("--no-score-filter", action="store_true", help="不过滤低分候选")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 回测启动: 买 {args.buy} → 卖 {args.sell}")

    print("  ▶ 步骤1: 还原热点板块 (成分股历史涨幅反推)...")
    hotspots = restore_hotspots(args.buy, pool_size=args.pool, heat_per=args.heat_per)

    print(f"  ▶ 步骤2: 对 Top {args.concepts} 热点板块成分股, 截K线到 {args.buy} 选股...")
    picks = select_on(args.buy, hotspots, args.concepts, args.per,
                      score_filter=not args.no_score_filter)

    print(f"  ▶ 步骤3: 用 {args.sell} 收盘价结算...")
    picks = settle(picks, args.sell)

    print(f"  ▶ 步骤4: 计算上证基准 ({args.buy}→{args.sell})...")
    benchmark = get_benchmark(args.buy, args.sell)

    report = build_report(args.buy, args.sell, hotspots, args.concepts, picks, benchmark)

    if args.json:
        print(json.dumps({
            "buy": args.buy, "sell": args.sell,
            "hotspots": [{"name": n, **i} for n, i in hotspots[:args.concepts]],
            "picks": picks,
        }, ensure_ascii=False, indent=2))
    else:
        print("\n" + report)

    # 落盘
    out = os.path.join(DATA_DIR, f"backtest_hotspot_{args.buy}_{args.sell}.md")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n  📄 报告已保存: {out}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
