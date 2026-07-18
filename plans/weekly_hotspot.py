# -*- coding: utf-8 -*-
"""本周热点选股流水线

流程:
  1. 分析本周热点版块 (概念板块实时排名, 过滤风格/地域/市值类)
  2. 自选股 = 步骤3策略选出的候选池 (热点板块内按 classify_stage 形态识别, 不再取"涨幅前2")
  3. 按新选股逻辑 (analysis/breakout.classify_stage 形态识别) 在热点版块内选股
  4. 生成报告并推送到企业微信智能机器人 (aibot 通道, 与用户沟通的唯一通道)

用法:
  python plans/weekly_hotspot.py                       # 默认 8 个版块, 自选股=策略候选池, 突破扫描每版块 15 只
  python plans/weekly_hotspot.py --concepts 10 --per 20
  python plans/weekly_hotspot.py --no-push              # 只生成报告不推送
  python plans/weekly_hotspot.py --json                 # 输出 JSON
  python plans/weekly_hotspot.py --runup-days 20 --runup-pct 40   # 前期大涨不追阈值(前N日涨幅>%剔除)

策略微调 (与 backtest_hotspot 回测结论一致):
  · 大盘三状态路由 (实战核心): 先判上证 MA20 vs MA60 得 多头/震荡/空头,
    再蒸馏不同选股策略 (见 REGIME_STRATEGY / apply_regime_strategy):
      - 多头(MA20>MA60): 顺势低吸 = 回调低吸(S5) ∪ 强趋势低波动(S6) ∪ 箱体突破(S9)
      - 震荡(MA20≈MA60): 高胜率共振(S3) ∪ 箱体突破(S9), 薄边降仓(0.6×)
      - 空头(MA20<MA60): 空仓观望, 不出股 (回测无可靠策略, 避免买点窗口虚假信号)
  · 箱体突破(S9): 长期盘整(箱体)后放量向上突破箱顶——量能(量比≥1.5)+价位(新鲜突破)+
    共振(金叉/多头)三重确认, 只买"刚突破"不追"已突破大涨"(详见报告【三b】)。
  · 前期大涨不追: 截至今天前 runup_days 日累计涨幅 > runup_pct% 直接剔除推荐 (等回踩不追高)
  · 每只推荐自带纪律买卖: 买点 / 止损价 / 目标价(take_profit) / 仓位 / 卖出提示(移动止损锁利)
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
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")

from collectors.concept import concept_rank_sina, merge_duplicate_concepts
from plans.concept_analysis import filter_concepts
from plans.breakout_scan import run as run_breakthrough, format_report as fmt_breakthrough, _kline_cached
from core.cli import save_watchlist
from analysis.breakout import STAGE_LABELS


# ───────────────── 形态成功率 / 买入计划 ─────────────────
# 科学退出参数 (与 backtest_weekly_hotspot.settle_smart 同源口径):
#   初始硬止损 -7% → 涨+12%减仓半仓锁利 → 减仓后破MA20(趋势线)或回撤10%清仓, 止损上移保本。
SMART_EXIT = {
    "stop_pct": 0.07,        # 初始硬止损 (突破-7% / 其他-8%)
    "scale_out_pct": 0.12,   # 涨幅达 +12% 触发减仓 (平衡: 不过早减仓)
    "scale_out_frac": 0.5,   # 减仓比例 (半仓)
    "trailing_pct": 0.10,    # 减仓后移动止损 (从峰值回撤 10%, 让利润奔跑)
    "use_ma20": True,        # 破 MA20 = 破趋势线 → 清仓
}


def _estimate_win_rate(stage, signals):
    """依据形态状态 + 信号共振, 估算形态成功率 (0~1)。

    信号组合 → 成功率 经验映射 (非历史回测, 供参考):
      - 突破启动 + 放量 + 均线多头 + 突破平台 : 0.68
      - 趋势中 + VCP + MACD金叉 + KDJ金叉      : 0.60
      - 趋势中 + 布林收缩 + 双金叉              : 0.56
      - 趋势中 + VCP + 单KDJ                   : 0.55
      - 趋势中 + 均线多头(仅)                  : 0.50
      - 趋势中 + 布林收缩(仅)                  : 0.48
    """
    sig = " ".join(signals or [])
    has = lambda k: k in sig
    if stage == "breakout":
        if has("放量") and has("均线多头") and has("突破平台"):
            return 0.68
        return 0.60
    rate = 0.45
    if has("VCP"):
        rate += 0.07
    if has("MACD金叉"):
        rate += 0.05
    if has("KDJ金叉"):
        rate += 0.04
    if has("布林带极致收缩"):
        rate += 0.04
    if has("均线多头"):
        rate += 0.04
    if has("放量"):
        rate += 0.03
    return round(min(rate, 0.65), 2)


def _rating(score, win_rate, stage):
    """综合评级: 重点 / 关注 / 观察 / 暂避。"""
    if stage == "breakout" and win_rate >= 0.60:
        return "重点"
    if win_rate >= 0.55:
        return "关注"
    if win_rate >= 0.50:
        return "观察"
    return "暂避"


def _build_plan(c, win_rate, rating):
    """生成买点 / 止损 / 仓位 (规则化, 单位: 元 / %)。"""
    price = float(c.get("price") or 0)
    stage = c.get("stage")
    chg = float(c.get("change_pct") or 0)
    if stage == "breakout":
        if chg >= 9.5:
            buy = "次日高开不破分时均线轻仓试，或回踩5日线≈¥%.2f低吸" % (price * 0.94)
        else:
            buy = "突破回踩5日线≈¥%.2f低吸，放量过前高加仓" % (price * 0.95)
        stop = price * 0.93        # 突破: 初始止损 -7%
    else:
        buy = "缩量回踩¥%.2f–%.2f低吸，突破信号确认再加仓" % (price * 0.95, price * 0.97)
        stop = price * 0.92        # 其他: 初始止损 -8%
    stop_pct = round((stop / price - 1) * 100, 1) if price else 0
    position = {"重点": 8, "关注": 8, "观察": 6, "暂避": 5}.get(rating, 5)
    return buy, round(stop, 2), stop_pct, position


def _prior_runup(symbol, lookback=20, ref_date=None):
    """截至 ref_date(默认最新交易日) 前 lookback 个交易日累计涨幅 — 捕捉'前期已大幅上涨'。

    返回涨幅% 或 None (无数据/历史不足)。与 backtest_hotspot.prior_runup 同源逻辑。
    """
    try:
        kl = _kline_cached(symbol)
    except Exception:
        return None
    if not kl:
        return None
    if ref_date is None:
        ref_date = kl[-1]["date"][:10]
    idx = None
    for i, b in enumerate(kl):
        if b["date"][:10] <= ref_date:
            idx = i
        else:
            break
    if idx is None:
        return None
    base_i = max(0, idx - lookback)
    base = kl[base_i]["close"]
    price = kl[idx]["close"]
    if base and price:
        return round((price - base) / base * 100, 2)
    return None


def _sell_hint(buy_price, stage, stop_loss):
    """生成卖出提示 (科学止盈/止损): 涨+10%减仓半仓锁利; 减仓后破MA20(趋势线)
    或回撤8%清仓; 破止损价离场 (减仓后止损上移保本)。"""
    so = SMART_EXIT["scale_out_pct"]
    scale_price = round(buy_price * (1 + so), 2)
    tp_pct = round(so * 100)
    tr = int(SMART_EXIT["trailing_pct"] * 100)
    hint = (f"涨+{tp_pct}%减仓半仓(¥{scale_price})锁利; 减仓后破MA20(趋势线)或回撤{tr}%清仓; "
            f"破¥{stop_loss}止损(减仓后上移保本)")
    return scale_price, hint


def enrich_candidates(breakthrough, runup_days=20, runup_pct=40):
    """对突破候选去重 (同股票跨多热点) + 叠加 win_rate / rating / plan 字段。

    策略微调 (与回测一致):
      · 前期大涨不追: 截至今天前 runup_days 日累计涨幅 > runup_pct% 视为'前期已大幅上涨,
        从推荐列表剔除, 计入 excluded (不追)。
      · 每只推荐附带 take_profit(目标价) + sell_hint(卖出提示: 目标价+移动止损)。
    """
    if not breakthrough or "candidates" not in breakthrough or "error" in breakthrough:
        return breakthrough
    seen = {}
    excluded = []
    for c in breakthrough["candidates"]:
        sym = c.get("symbol")
        if not sym:
            continue
        # ── 前期大涨不追 ──
        runup = _prior_runup(sym, lookback=runup_days)
        if runup is not None and runup >= runup_pct:
            excluded.append({
                "symbol": sym, "name": c.get("name", sym),
                "price": c.get("price"), "prior_runup": runup,
                "concepts": [c.get("concept")] if c.get("concept") else [],
            })
            continue
        if sym in seen:
            # 合并概念 (同一股票出现在多个热点版块)
            if c.get("concept") and c["concept"] not in seen[sym]["concepts"]:
                seen[sym]["concepts"].append(c["concept"])
            continue
        # 取K线供蒸馏策略(strat_*)使用: 回调低吸需 ma20/价格, 与回测同源
        try:
            kl = _kline_cached(sym)
        except Exception:
            kl = None
        wr = _estimate_win_rate(c.get("stage"), c.get("signals", []))
        rating = _rating(c.get("score", 0), wr, c.get("stage"))
        buy, stop, stop_pct, position = _build_plan(c, wr, rating)
        tp, sell_hint = _sell_hint(float(c.get("price") or 0), c.get("stage"), stop)
        seen[sym] = {
            **c,
            "kl": kl,
            "price_b": float(c.get("price") or 0),
            "concepts": [c.get("concept")],
            "win_rate": wr,
            "rating": rating,
            "buy_point": buy,
            "stop_loss": stop,
            "stop_pct": stop_pct,
            "position": position,
            "prior_runup": runup,
            "take_profit": tp,
            "sell_hint": sell_hint,
        }
    enriched = sorted(seen.values(), key=lambda x: -x["score"])
    return {**breakthrough, "candidates": enriched, "count": len(enriched),
            "excluded": excluded, "excluded_count": len(excluded)}


# ───────────────── 大盘三状态 × 蒸馏策略路由 ─────────────────
# 来自 backtest_hotspot walk-forward 蒸馏 (2024-06→2026-07, 按大盘三状态拆胜率):
#   多头(MA20>MA60): S5 趋势回调低吸 77%/+6.96% ⊕ S6 强趋势低波动 76%/+6.52%
#   震荡(MA20≈MA60): S3 高胜率共振 56%/+2.97% (唯一正期望, 薄边降仓)
#   空头(MA20<MA60): 无可靠策略(最佳仅53%且样本小, 多数40-50%) → 空仓
REGIME_NEUTRAL_BAND = 0.03  # |MA20-MA60|/MA60 <= 3% 视为纠缠(震荡)

REGIME_STRATEGY = {
    "多头": {
        "label": "顺势低吸(回调+低波)",
        "desc": "大盘多头排列, 顺势做多。实证: 回调低吸胜率77%/均收益+6.96%, "
                "强趋势低波动76%/+6.52% (S5∪S6)。",
        "position_scale": 1.0,
    },
    "震荡": {
        "label": "高胜率共振",
        "desc": "大盘纠缠震荡, 仅高胜率共振(S3)有正期望(56%/+2.97%), 边薄。降仓位、严止损。",
        "position_scale": 0.6,
    },
    "空头": {
        "label": "空仓观望",
        "desc": "大盘空头排列, 无可靠策略(最佳仅53%且样本小, 多数40-50%)。"
                "空仓不出股, 规避买点窗口虚假信号。",
        "position_scale": 0.0,
    },
    "未知": {
        "label": "未知(按震荡处理)",
        "desc": "大盘数据不足, 保守按震荡处理。",
        "position_scale": 0.6,
    },
}


def market_regime():
    """判定当前大盘(上证000001)三状态: 多头/震荡/空头。基于 MA20 vs MA60。

    返回 (状态, MA20相对MA60偏离% 或 None)。与回测 _market_weak 同源口径。
    """
    try:
        kl = _kline_cached("000001", days=250)
        closes = [b["close"] for b in kl if b.get("close")]
        if len(closes) < 60:
            return "未知", None
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        diff = (ma20 - ma60) / ma60
        if diff > REGIME_NEUTRAL_BAND:
            return "多头", round(diff * 100, 2)
        if diff < -REGIME_NEUTRAL_BAND:
            return "空头", round(diff * 100, 2)
        return "震荡", round(diff * 100, 2)
    except Exception:
        return "未知", None


def apply_regime_strategy(candidates, regime, buy_date):
    """按大盘状态路由到蒸馏策略, 返回最终推荐候选 (空头返回空列表=空仓)。

    直接复用 backtest_hotspot 的 strat_* 函数 (与回测同源, 保证口径一致)。
    """
    from plans.backtest_hotspot import strat_pullback, strat_steady, strat_highwr, strat_box_breakout
    if regime == "空头":
        return []
    if regime == "多头":
        # 顺势低吸: 回调低吸(S5) ∪ 强趋势低波动(S6) ∪ 箱体突破(S9), 去重
        pb = [c for c in candidates if c.get("kl")]
        picks = strat_pullback(pb, runup_pct=40, buy_date=buy_date)
        picks += strat_steady(candidates, runup_pct=40, buy_date=buy_date)
        picks += strat_box_breakout(candidates, runup_pct=40, buy_date=buy_date)
    else:  # 震荡 / 未知 → 高胜率共振(S3) ∪ 箱体突破(S9)
        picks = strat_highwr(candidates, runup_pct=40, buy_date=buy_date)
        picks += strat_box_breakout(candidates, runup_pct=40, buy_date=buy_date)
    seen = set()
    out = []
    for c in picks:
        if c["symbol"] not in seen:
            seen.add(c["symbol"])
            out.append(c)
    return out


def _bare(symbol: str) -> str:
    """成分股 symbol 常带 sh/sz 前缀, 自选股统一存 6 位裸码。"""
    return symbol[2:] if symbol[:2] in ("sh", "sz") else symbol


def get_hotspots(top_n: int = 8) -> list:
    """取本周热点版块 (过滤后 Top N)。

    返回: [{name, code, change_pct, amount, leader, leader_code, leader_pct}, ...]
    """
    raw = concept_rank_sina(limit=top_n * 3)
    mapped = [{
        "name": c["name"],
        "code": c["code"],
        "change_pct": c.get("change_pct", 0),
        "amount": c.get("amount", 0),
        "leader": c.get("leader_name", ""),
        "leader_code": c.get("leader_code", ""),
        "leader_pct": c.get("leader_pct", 0),
    } for c in raw]
    return merge_duplicate_concepts(filter_concepts(mapped))[:top_n]


def build_watchlist_from_candidates(breakthrough: dict, verbose: bool = True) -> list:
    """自选股 = 最终推荐的买 list (breakthrough["final"])。

    不再取"涨幅前2" (那会把已涨停的票塞进监控池, 与策略"不追已大涨"相悖),
    也不再跨板块去重丢弃 (跨板块共振=双重确认, 是最强信号, 应保留其全部所属板块)。
    final 是经大盘三状态蒸馏 (apply_regime_strategy) 后的最终买 list:
      多头→全量候选; 震荡→薄边降仓; 空头→空 list。
    若 final 缺失则回退到 candidates (形态识别候选池), 不回退到更早的"涨幅前2"。
    enrich_candidates 已对候选按 symbol 合并概念 (concepts 字段含全部所属板块),
    final 元素同样带 concepts 字段。

    返回: [{code, name, pct, concept}, ...] (concept 为"板块1、板块2"合并串)
    """
    final = breakthrough.get("final")
    if not final:
        final = breakthrough.get("candidates", []) or []
    seen = set()
    picks = []
    for c in final:
        sym = _bare(c.get("symbol", ""))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        cons = c.get("concepts") or ([c.get("concept")] if c.get("concept") else [])
        picks.append({
            "code": sym,
            "name": c.get("name", sym),
            "pct": round(float(c.get("change_pct", 0) or 0), 2),
            "concept": "、".join(cons),
        })
    wl_codes = [p["code"] for p in picks]
    save_watchlist(wl_codes)
    if verbose:
        print(f"  ✓ 自选股已重建(最终推荐买 list): {len(wl_codes)} 只")
    return picks


def build_report(date_str: str, hotspots: list, watchlist_picks: list,
                 breakthrough: dict, regime: str = None) -> str:
    regime = regime or breakthrough.get("regime", "未知")
    rinfo = REGIME_STRATEGY.get(regime, REGIME_STRATEGY["未知"])
    final = breakthrough.get("final", breakthrough.get("candidates", []))
    is_bear = (regime == "空头")
    rdiff = breakthrough.get("regime_diff")
    lines = [f"\n{'='*50}",
             f"  📊 本周热点选股报告 ({date_str})",
             f"{'='*50}"]

    # 〇、大盘行情状态与策略路由
    lines.append(f"\n【〇、大盘行情状态与策略路由】")
    rdiff_s = f"{rdiff:+.2f}%" if isinstance(rdiff, (int, float)) else "—"
    lines.append(f"  大盘(上证): {regime}  (MA20相对MA60 {rdiff_s})")
    lines.append(f"  采用策略: 【{rinfo['label']}】")
    lines.append(f"  策略说明: {rinfo['desc']}")
    if is_bear:
        lines.append(f"  ⚠️ 空头排列 → 本期空仓观望, 不出股 (候选池 {breakthrough.get('count',0)} 只全部放弃)")
    else:
        lines.append(f"  蒸馏精选: 候选池 {breakthrough.get('count',0)} 只 → 精选 {len(final)} 只")

    # 一、热点版块
    lines.append(f"\n【一、本周热点版块 Top {len(hotspots)}】")
    for i, c in enumerate(hotspots, 1):
        amt_yi = round(c.get("amount", 0) / 1e8, 1) if c.get("amount") else 0
        lead = f" 龙头:{c['leader']}({c['leader_pct']:+.1f}%)" if c.get("leader") else ""
        lines.append(f"  {i}. {c['name']} {c['change_pct']:+.2f}%  成交{amt_yi}亿{lead}")

    # 二、自选股 (最终推荐买 list)
    lines.append(f"\n【二、自选股更新 (最终推荐买 list · 经大盘三状态蒸馏)】")
    lines.append(f"  共 {len(watchlist_picks)} 只, 已写入 data/watchlist.json:")
    # 按版块分组展示
    by_concept = {}
    for p in watchlist_picks:
        by_concept.setdefault(p["concept"], []).append(p)
    for concept, picks in by_concept.items():
        parts = [f"{p['name']}({p['code']}) {p['pct']:+.1f}%" for p in picks]
        lines.append(f"  · {concept}: {' / '.join(parts)}")

    # 三、蒸馏策略精选
    lines.append(f"\n【三、蒸馏策略精选 ({rinfo['label']}, 大盘{regime})】")
    if "error" in breakthrough:
        lines.append(f"  ❌ 突破扫描失败: {breakthrough['error']}")
    elif is_bear:
        lines.append(f"  🛑 空头排列行情 → 空仓观望, 不出股。")
        lines.append(f"  · 理由: {rinfo['desc']}")
        lines.append(f"  · 候选池仍有 {breakthrough.get('count',0)} 只, 但空头无可靠策略, 全部放弃。")
    else:
        lines.append(f"  候选池 {breakthrough.get('count',0)} 只 (已剔除前期大涨不追 "
                     f"{breakthrough.get('excluded_count', 0)} 只) → 蒸馏精选 {len(final)} 只:")
        for i, c in enumerate(final[:15], 1):
            sig = " | ".join(c.get("signals", [])[:3])
            wr = c.get("win_rate")
            wr_str = f" 成功率{wr*100:.0f}%" if isinstance(wr, (int, float)) else ""
            ru = c.get("prior_runup")
            ru_s = f" 前期{ru:+.0f}%" if isinstance(ru, (int, float)) else ""
            lines.append(
                f"  {i}. {c['name']}({c['symbol']}) ¥{c['price']} {c['change_pct']:+.2f}% "
                f"| {STAGE_LABELS.get(c['stage'], c['stage'])} 评分{c['score']}{wr_str}{ru_s}")
            lines.append(f"     热点:{'、'.join(c.get('concepts', [c.get('concept')]))} | {sig}")
        # 被'前期大涨不追'剔除的清单
        exc = breakthrough.get("excluded") or []
        if exc:
            lines.append(f"\n  🚫 前期大涨不追 (已剔除, 等回踩不追高):")
            for e in exc[:15]:
                ru = e.get("prior_runup")
                ru_s = f"{ru:+.0f}%" if isinstance(ru, (int, float)) else "—"
                lines.append(f"    · {e['name']}({e['symbol']}) ¥{e.get('price','—')} 前期{ru_s} "
                             f"热点:{'、'.join(e.get('concepts', []))}")

    # 三b、箱体突破原理与选股逻辑
    lines.append(f"\n【三b、箱体突破原理与选股逻辑 (S9)】")
    lines.append(f"  · 长期盘整(箱体): 股价在相对狭窄区间(箱底支撑~箱顶阻力)内上下波动, "
                 f"波动逐步收敛、成交量萎缩——这是供需再平衡、浮筹沉淀的过程, 也是主力吸筹的常见形态。")
    lines.append(f"  · 向上突破: 放量站上箱顶(阻力位)=买方力量压倒卖方, 突破临界点; "
                 f"量能确认突破有效性(无量上冲多为假突破, 易回落箱体内)。")
    lines.append(f"  · 选股门槛(量价共振): ①箱体已建成(平台波动<15%) ②新鲜突破(近15日刚站上箱顶, "
                 f"非已大涨) ③放量确认(量比≥1.5) ④均线多头或MACD/KDJ金叉共振。")
    lines.append(f"  · 退出: 沿用科学止盈/止损(涨+12%减仓半仓→破MA20/回撤10%清仓), 让突破后的趋势充分奔跑。")

    # 四、买入建议与计划
    lines.append(f"\n【四、买入建议与计划 (形态成功率 + 综合评级 + 纪律买卖)】")
    if "error" in breakthrough:
        lines.append("  (无数据)")
    elif is_bear:
        lines.append("  🛑 空仓观望: 本期不给出买入计划。")
        lines.append("  · 空头排列下任何买点胜率均不足 (回测最佳仅53%且样本小), 持币等待多头信号。")
    else:
        lines.append("  评级: 重点 > 关注 > 观察 > 暂避 | 仓位为单标的上限建议"
                     + (" (震荡薄边已按0.6×降仓)" if regime == "震荡" else ""))
        lines.append("  纪律(科学止盈/止损): 破止损价离场; 涨+12%减仓半仓锁利; "
                     "减仓后破MA20(趋势线)或回撤10%清仓, 减仓后止损上移保本")
        for i, c in enumerate(final[:12], 1):
            wr = c.get("win_rate")
            wr_str = f"{wr*100:.0f}%" if isinstance(wr, (int, float)) else "—"
            lines.append(
                f"  {i}. 【{c.get('rating','—')}】{c['name']}({c['symbol']}) "
                f"成功率{wr_str} 模型分{c['score']} 仓位{c.get('position','—')}%")
            lines.append(f"     📍 买点: {c.get('buy_point','—')}")
            lines.append(f"     🛡 止损: ¥{c.get('stop_loss','—')} ({c.get('stop_pct','—')}%)")
            lines.append(f"     🎯 目标: ¥{c.get('take_profit','—')}  | {c.get('sell_hint','—')}")

    lines.append(f"\n{'='*50}")
    lines.append("报告生成完毕")
    return "\n".join(lines)


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="本周热点选股流水线")
    ap.add_argument("--concepts", type=int, default=8, help="热点版块数量 (默认8)")
    ap.add_argument("--per", type=int, default=15, help="突破扫描每版块成分股数 (默认15)")
    ap.add_argument("--runup-days", type=int, default=20, help="前期涨幅回看交易日数 (默认20)")
    ap.add_argument("--runup-pct", type=float, default=40, help="前期涨幅超此%则'不追'剔除 (默认40)")
    ap.add_argument("--no-push", action="store_true", help="仅生成报告, 不推送微信")
    ap.add_argument("--html", action="store_true", help="生成 HTML 报告文件 (输出 HTML_REPORT:<path>, 供 bot 附带发送)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{date_str}] 本周热点选股流水线启动...", flush=True)

    # 1. 热点版块
    print("  ▶ 步骤1: 分析本周热点版块...", flush=True)
    hotspots = get_hotspots(top_n=args.concepts)
    if not hotspots:
        print("  ❌ 无法获取热点版块 (网络异常或休市)", flush=True)
        return 1
    print(f"    热点版块 {len(hotspots)} 个: " + "、".join(c["name"] for c in hotspots), flush=True)

    # 2. 自选股改由步骤3(策略选股)后的候选池重建, 此处不再单独取"涨幅前2"

    # 3. 新选股逻辑筛选 (突破扫描)
    print("  ▶ 步骤3: 按新选股逻辑 (classify_stage) 在热点版块内选股...", flush=True)
    breakthrough = run_breakthrough(
        top_concepts=args.concepts,
        top_per_concept=args.per,
        verbose=True,
    )
    # 3b. 去重 + 叠加 形态成功率 / 综合评级 / 买入计划 + 前期大涨不追过滤
    breakthrough = enrich_candidates(breakthrough,
                                     runup_days=args.runup_days,
                                     runup_pct=args.runup_pct)
    # 2(延后). 自选股 = 策略候选池 (热点板块内按形态识别选出, 跨板块共振保留)
    watchlist_picks = build_watchlist_from_candidates(breakthrough, verbose=True)

    print(f"    选入 {breakthrough.get('count', 0)} 只, "
          f"剔除前期大涨不追 {breakthrough.get('excluded_count', 0)} 只 "
          f"(前{args.runup_days}日>{args.runup_pct}%不追)", flush=True)

    # 3c. 大盘三状态判定 + 蒸馏策略路由 (实战核心: 不同行情用不同选股策略)
    regime, regime_diff = market_regime()
    rinfo = REGIME_STRATEGY.get(regime, REGIME_STRATEGY["未知"])
    final = apply_regime_strategy(breakthrough.get("candidates", []), regime, date_str)
    scale = rinfo["position_scale"]
    if scale and scale != 1.0:
        for c in final:
            try:
                c["position"] = max(1, round((c.get("position") or 5) * scale))
            except Exception:
                pass
    breakthrough["regime"] = regime
    breakthrough["regime_diff"] = regime_diff
    breakthrough["strategy_label"] = rinfo["label"]
    breakthrough["strategy_desc"] = rinfo["desc"]
    breakthrough["final"] = final
    breakthrough["final_count"] = len(final)
    print(f"    大盘状态: {regime} (MA20-MA60 {regime_diff:+.2f}%) → 策略[{rinfo['label']}] "
          f"精选 {len(final)} 只 (候选池 {breakthrough.get('count',0)} 只)", flush=True)

    # 4. 报告
    report = build_report(date_str, hotspots, watchlist_picks, breakthrough, regime=regime)

    # 4b. HTML 报告 (统一机制: core/html_renderer.render + HTML_REPORT:<path> 约定)
    if args.html:
        try:
            from core.html_renderer import render
            by_concept = {}
            for p in watchlist_picks:
                by_concept.setdefault(p["concept"], []).append(p)
            watchlist_grouped = [{"concept": k, "picks": v} for k, v in by_concept.items()]
            html_path = render(
                {
                    "date": date_str,
                    "hotspots": hotspots,
                    "watchlist": watchlist_picks,
                    "watchlist_grouped": watchlist_grouped,
                    "breakthrough": breakthrough,
                },
                "weekly_hotspot_report",
                filename=f"weekly_hotspot_report_{date_str}.html",
            )
            print(f"HTML_REPORT:{html_path}")
        except Exception as e:
            print(f"[HTML] 报告生成失败: {e}", file=sys.stderr)

    # 报告落盘 (无论是否推送都保留交付物)
    report_path = os.path.join(DATA_DIR, f"weekly_hotspot_report_{date_str}.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        report_path = None

    if args.json:
        out = {
            "date": date_str,
            "hotspots": hotspots,
            "watchlist": watchlist_picks,
            "breakthrough": breakthrough,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(report)

    if report_path:
        print(f"\n[REPORT] 报告已保存: {report_path}", flush=True)

    # 5. 推送微信 (仅经智能机器人 aibot 通道, 与用户沟通)
    if not args.no_push:
        try:
            from notify.wecom_bot import push_markdown_via_bot
            ok = push_markdown_via_bot(report)
            if ok:
                print("\n[AIBOT] 已推送报告到企业微信智能机器人", flush=True)
            else:
                print("\n[AIBOT] 推送未成功 (详见上方错误), 报告已落盘。",
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f"\n[AIBOT] 推送异常: {e}", file=sys.stderr, flush=True)
            print("       报告已落盘, 配置好智能机器人后可重跑或手动发送。",
                  file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
