#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""决策引擎 — 每日操作简报 (选股→跟踪→评级变化→买卖推荐 的决策闭环).

解决"海选一堆成分股、几百只没方向"的问题: 把 stock_pool.json 里累积的
几百只(其中大量 rating=暂避 的噪音)收敛成一份**精简、可操作**的日报, 四张清单:

  🟢 买入推荐  = 核心池(rating∈关注/观察高分) ∩ 未持仓 ∩ 大盘非空头 ∩ 现价临近/突破买点  (≤8只)
  🔴 卖出推荐  = 已持仓 ∩ (触止损 / 触止盈 / 破MA20 / 评级转弱至暂避)                    (全列)
  🟡 持仓跟踪  = 已持仓 ∩ 不在卖出清单 (浮盈 + 当前移动止损位 + 建议)                    (≤15只)
  🔺 评级变化  = 今日 refresh 相比上次评级发生升/降级的                                  (全列)
  📋 核心关注池 = rating∈{关注,观察} 排序 (给"方向", 数十只而非数百只)                   (≤20只)

设计原则:
  - **只输出可操作的**: rating=暂避(占池中绝大多数)一律不进买入/核心池, 只在"评级转弱"卖出里出现。
  - **数量硬上限**: 每张清单限量, 避免"报告一大堆"。
  - 依赖已有数据源: stock_pool(每日 refresh 的关卡/评级/移动止损) + 实时价(盘后收盘价) + market_regime。
  - 盘后(收盘复盘后)运行: 实时价接口返回当日收盘价。

用法:
  python plans/decision_engine.py                 # 打印简报(含企微卡片标记)
  python plans/decision_engine.py --no-quote       # 不取实时价(用最新K线收盘, 离线可跑)
  python plans/decision_engine.py --json           # 结构化输出
"""
import os
import sys
import json
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from plans.stock_pool import load_pool

RATING_ORDER = {"暂避": 0, "观察": 1, "关注": 2, "推荐": 3}

# 买入临近窗口: 现价落在 [买点*(1-NEAR), 买点*(1+CHASE)] 视为"可挂单/刚突破可追"
BUY_NEAR_BELOW = 0.03   # 买点下方 3% 内 → 挂单待突破
BUY_CHASE_ABOVE = 0.03  # 买点上方 3% 内 → 刚突破可追(不追高)

# 清单数量上限
MAX_BUY = 8
MAX_HOLD = 15
MAX_CORE = 20

# 买入推荐放宽口径: 观察评级纳入买入候选的最低评分门槛
BUY_OBSERVE_MIN_SCORE = 50


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _last_close(symbol):
    """从落盘 K 线取最新收盘价 (实时价失败时的兜底)。"""
    try:
        from plans.breakout_scan import _kline_cached
        kl = _kline_cached(symbol)
        if kl:
            return float(kl[-1]["close"])
    except Exception:
        pass
    return None


def _get_quotes(symbols, use_quote=True):
    """取实时价(盘后=收盘价), 失败逐只回退最新K线收盘。返回 {sym: price}。"""
    prices = {}
    if use_quote and symbols:
        try:
            from collectors.quote import batch_quotes_tencent
            q = batch_quotes_tencent(symbols) or {}
            for s in symbols:
                info = q.get(s) or q.get(str(s))
                if info and info.get("price"):
                    prices[s] = float(info["price"])
        except Exception:
            pass
    # 缺失的用 K 线收盘兜底
    for s in symbols:
        if s not in prices:
            c = _last_close(s)
            if c:
                prices[s] = c
    return prices


def _fmt_pct(x):
    return f"{x:+.1f}%" if isinstance(x, (int, float)) else "—"


def _concept_str(e):
    cons = e.get("concepts") or []
    if isinstance(cons, str):
        cons = [cons]
    cons = [c for c in cons if c]
    return "、".join(cons[:2]) if cons else "-"


def build_brief(use_quote=True):
    """构建决策简报, 返回结构化 dict。"""
    pool = load_pool()
    entries = [e for e in pool.get("entries", [])]
    today = _today()

    # 大盘状态门控
    try:
        from plans.weekly_hotspot import market_regime
        regime, rdiff = market_regime()
    except Exception:
        regime, rdiff = "未知", None

    # 实时价 (仅取需要的: 持仓 + 核心池, 避免拉 400 只)
    active = [e for e in entries if not e.get("exited")]
    core_or_held = [e for e in active
                    if e.get("entered") or e.get("rating") in ("关注", "观察", "推荐")]
    syms = list(dict.fromkeys(e["symbol"] for e in core_or_held))
    prices = _get_quotes(syms, use_quote=use_quote)

    buy, sell, hold, rating_chg, core = [], [], [], [], []

    for e in active:
        sym = e["symbol"]
        name = e.get("name", sym)
        rating = e.get("rating")
        score = e.get("score")
        stage = e.get("stage")
        price = prices.get(sym)
        buy_level = e.get("buy_level")
        stop_level = e.get("stop_level") or e.get("stop_base")
        tp_level = e.get("tp_level")
        entered = bool(e.get("entered"))
        entered_price = e.get("entered_price") or e.get("price_entry")
        ma20 = None
        tr = e.get("trend") or {}
        if isinstance(tr, dict):
            ma20 = tr.get("ma20")

        # ── 评级变化(今日) ──
        if e.get("rating_change_date") == today and e.get("rating_change"):
            rating_chg.append({
                "symbol": sym, "name": name,
                "from": e.get("prev_rating"), "to": rating,
                "dir": e.get("rating_change"),
                "score": score,
            })

        # ── 已持仓: 卖出判断 / 持仓跟踪 ──
        if entered:
            pnl = None
            if price and entered_price:
                pnl = (price - entered_price) / entered_price * 100
            reason = None
            if price and stop_level and price <= stop_level:
                reason = f"触止损{stop_level:.2f}"
            elif price and tp_level and price >= tp_level:
                reason = f"触止盈{tp_level:.2f}"
            elif rating == "暂避":
                reason = "评级转弱→了结"
            elif price and ma20 and price < ma20 and (pnl is not None and pnl > 0):
                reason = f"破MA20({ma20:.2f})→保盈离场"
            if reason:
                sell.append({
                    "symbol": sym, "name": name, "price": price,
                    "cost": entered_price, "pnl": pnl, "reason": reason,
                    "stage": stage, "rating": rating,
                })
            else:
                hold.append({
                    "symbol": sym, "name": name, "price": price,
                    "cost": entered_price, "pnl": pnl,
                    "stop": stop_level, "tp": tp_level,
                    "rating": rating, "stage": stage,
                    "concept": _concept_str(e),
                })
            continue

        # ── 未持仓: 核心池 + 买入推荐 ──
        if rating in ("关注", "观察", "推荐"):
            hot_type = e.get("hot_type") or "无概念"
            core.append({
                "symbol": sym, "name": name, "rating": rating, "score": score,
                "stage": stage, "price": price, "buy": buy_level,
                "stop": stop_level, "tp": tp_level,
                "concept": _concept_str(e),
                "position": e.get("position"),
                "hot_type": hot_type,
            })
            # 买入推荐: 关注/推荐 直接纳入; 观察 需评分≥50 才放宽纳入(大盘非空头 + 临近买点)
            # 热度门控: 衰减/退潮/一日游 的票不进买入推荐(热点在退, 不追)
            buy_eligible = False
            hot_ok = hot_type in ("持续发酵", "新兴", "波段上涨", "首次出现", "无概念")
            if regime != "空头" and buy_level and price and hot_ok:
                if rating in ("关注", "推荐"):
                    buy_eligible = True
                elif rating == "观察" and isinstance(score, (int, float)) and score >= BUY_OBSERVE_MIN_SCORE:
                    buy_eligible = True
            if buy_eligible:
                lo = buy_level * (1 - BUY_NEAR_BELOW)
                hi = buy_level * (1 + BUY_CHASE_ABOVE)
                if lo <= price <= hi:
                    status = "刚突破可追" if price >= buy_level else "临近待挂单"
                    buy.append({
                        "symbol": sym, "name": name, "score": score, "stage": stage,
                        "price": price, "buy": buy_level, "stop": stop_level,
                        "tp": tp_level, "position": e.get("position"),
                        "concept": _concept_str(e), "status": status,
                        "hot_type": hot_type,
                        "stop_pct": (buy_level - stop_level) / buy_level * 100
                                     if (stop_level and buy_level) else None,
                    })

    # ── 概念热度标签: 买入排序优先持续发酵/新兴, 排斥衰减/退潮 ──
    HOT_PRIORITY = {"持续发酵": 0, "新兴": 1, "波段上涨": 2, "衰减": 3, "退潮": 4, "一日游": 5, "首次出现": 6, "无概念": 7}

    # 买入推荐: 衰减/退潮/一日游 的票降级到末尾; 持续发酵/新兴 优先
    for b in buy:
        ht = b.get("hot_type", "无概念")
        b["hot_priority"] = HOT_PRIORITY.get(ht, 7)

    # 排序 + 限量 (买入: 热度优先 → 评分)
    buy.sort(key=lambda x: (x.get("hot_priority", 7), -(x.get("score") or 0)))
    buy = buy[:MAX_BUY]
    hold.sort(key=lambda x: (x.get("pnl") if x.get("pnl") is not None else -999), reverse=True)
    hold = hold[:MAX_HOLD]
    core.sort(key=lambda x: (RATING_ORDER.get(x.get("rating"), 0), x.get("score") or 0), reverse=True)
    core = core[:MAX_CORE]
    # 卖出置顶止损, 评级变化先降级
    sell.sort(key=lambda x: (0 if "止损" in (x.get("reason") or "") else 1))
    rating_chg.sort(key=lambda x: 0 if x.get("dir") == "down" else 1)

    return {
        "date": today,
        "regime": regime,
        "regime_diff": rdiff,
        "pool_total": len(entries),
        "pool_active": len(active),
        "held": sum(1 for e in active if e.get("entered")),
        "buy": buy, "sell": sell, "hold": hold,
        "rating_change": rating_chg, "core": core,
    }


def render_brief(brief):
    """渲染为企微/控制台友好的 markdown 文本 (含卡片标记 + #NO_PUSH# 兜底)。"""
    regime = brief["regime"]
    rd = brief.get("regime_diff")
    rd_s = f"{rd:+.2f}%" if isinstance(rd, (int, float)) else "—"
    regime_emoji = {"多头": "🟢", "震荡": "🟡", "空头": "🔴", "未知": "⚪"}.get(regime, "⚪")

    buy, sell, hold = brief["buy"], brief["sell"], brief["hold"]
    rc, core = brief["rating_change"], brief["core"]

    L = []
    L.append("<<<WECHAT_CARD_START>>>")
    L.append(f"## 📅 每日操作简报 {brief['date']}")
    L.append(f"{regime_emoji} 大盘 **{regime}** (MA20/MA60 {rd_s}) ｜ "
             f"持仓 {brief['held']} ｜ 核心池 {len(core)} ｜ 池 {brief['pool_active']}")
    if regime == "空头":
        L.append("> ⚠️ 空头行情: **停止新开仓**, 仅执行卖出/持仓保护。")

    # 🔴 卖出 (最高优先, 放最前) — 拆"紧急"(止损/止盈/破位, 逐条) 与"评级转弱"(汇总, 避免刷屏)
    L.append("")
    urgent = [s for s in sell if "评级转弱" not in (s.get("reason") or "")]
    weak = [s for s in sell if "评级转弱" in (s.get("reason") or "")]
    if sell:
        L.append(f"### 🔴 卖出推荐 ({len(sell)})")
        for s in urgent:
            pnl = _fmt_pct(s.get("pnl"))
            px = f"{s['price']:.2f}" if s.get("price") else "—"
            L.append(f"- **{s['name']}**({s['symbol']}) 现{px} 浮盈{pnl} → {s['reason']}")
        if weak:
            names = "、".join(f"{s['name']}({s['symbol']})" for s in weak[:8])
            more = f" 等{len(weak)}只" if len(weak) > 8 else ""
            L.append(f"- ⬇️ **评级转弱建议了结 {len(weak)} 只**: {names}{more}")
    else:
        L.append("### 🔴 卖出推荐: 无")

    # 🟢 买入
    L.append("")
    if regime == "空头":
        L.append("### 🟢 买入推荐: 空仓观望(大盘空头)")
    elif buy:
        L.append(f"### 🟢 买入推荐 ({len(buy)})")
        for b in buy:
            px = f"{b['price']:.2f}" if b.get("price") else "—"
            stopp = f"止损{b['stop']:.2f}" if b.get("stop") else "止损—"
            stpct = f"(-{b['stop_pct']:.1f}%)" if b.get("stop_pct") else ""
            tp = f"止盈{b['tp']:.2f}" if b.get("tp") else ""
            pos = f" 仓位{b['position']}" if b.get("position") else ""
            L.append(f"- **{b['name']}**({b['symbol']}) [{b['status']}] 现{px} "
                     f"买点{b['buy']:.2f} {stopp}{stpct} {tp}{pos}")
            L.append(f"    形态{b.get('stage','-')} 评分{b.get('score','-')} ｜ {b.get('concept','-')}")
    else:
        L.append("### 🟢 买入推荐: 无(核心池暂无临近买点标的)")

    # 🔺 评级变化
    if rc:
        L.append("")
        L.append(f"### 🔺 评级变化 ({len(rc)})")
        for r in rc:
            arrow = "⬆️上调" if r["dir"] == "up" else "⬇️下调"
            L.append(f"- {arrow} **{r['name']}**({r['symbol']}) {r.get('from','?')}→{r.get('to','?')}")

    L.append("<<<WECHAT_CARD_END>>>")

    # ── 以下为详细部分(HTML 报告展开, 卡片不含) ──
    # 🔴 卖出明细(全) — 评级转弱清单在卡片里被汇总, 这里逐条列出
    if weak:
        L.append("")
        L.append(f"### 🔴 卖出明细·评级转弱 ({len(weak)})")
        for s in weak:
            pnl = _fmt_pct(s.get("pnl"))
            px = f"{s['price']:.2f}" if s.get("price") else "—"
            cost = f"{s['cost']:.2f}" if s.get("cost") else "—"
            L.append(f"- {s['name']}({s['symbol']}) 现{px}/成本{cost} 浮盈{pnl} → {s['reason']}")

    # 🟡 持仓跟踪
    L.append("")
    L.append(f"### 🟡 持仓跟踪 ({len(hold)})")
    if hold:
        for h in hold:
            pnl = _fmt_pct(h.get("pnl"))
            px = f"{h['price']:.2f}" if h.get("price") else "—"
            cost = f"{h['cost']:.2f}" if h.get("cost") else "—"
            stop = f"止损{h['stop']:.2f}" if h.get("stop") else "止损—"
            L.append(f"- {h['name']}({h['symbol']}) 现{px}/成本{cost} 浮盈{pnl} ｜ {stop} ｜ "
                     f"{h.get('rating','-')}/{h.get('stage','-')}")
    else:
        L.append("- (无持仓)")

    # 📋 核心关注池
    L.append("")
    L.append(f"### 📋 核心关注池 Top{len(core)} (方向, 非全池)")
    if core:
        for c in core:
            px = f"{c['price']:.2f}" if c.get("price") else "—"
            buyp = f"买点{c['buy']:.2f}" if c.get("buy") else "买点—"
            L.append(f"- {c['name']}({c['symbol']}) {c.get('rating','-')}/评分{c.get('score','-')}/"
                     f"{c.get('stage','-')} 现{px} {buyp} ｜ {c.get('concept','-')}")
    else:
        L.append("- (核心池为空)")

    text = "\n".join(L)

    # 无任何可操作项(无买/无卖/无持仓/无评级变化) → 通知层跳过推送
    if not buy and not sell and not hold and not rc:
        text += "\n#NO_PUSH#"
    return text


def main():
    ap = argparse.ArgumentParser(description="每日操作简报(决策闭环)")
    ap.add_argument("--no-quote", action="store_true", help="不取实时价, 用最新K线收盘(离线)")
    ap.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    args = ap.parse_args()

    brief = build_brief(use_quote=not args.no_quote)
    if args.json:
        print(json.dumps(brief, ensure_ascii=False, indent=2))
    else:
        print(render_brief(brief))


if __name__ == "__main__":
    main()
