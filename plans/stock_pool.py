#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略股票池 (stock_pool.json) — 累积 + TTL + 去重 + 每日 refresh.

设计 (与"自选股"解耦):
  - 股票池 = 机器维护: 每日策略选股(突破扫描)产出 → add_entries 累积进池
    (同 symbol 已存在则更新动态字段、保留入选日 entry_date, 不去重丢弃)。
  - TTL: 入选超过 TTL_DAYS 自然日(默认 30≈20 交易日)或已标记 exited 的自动过期。
  - refresh_stock_pool: 每日收盘后用最新 K 线重算数值关卡 + 移动止损 + 重评分,
    写回池, 供 intraday_watch 盘中监控 + weekly_hotspot 报告展示。

自选股 (data/watchlist.json) 改为纯人工维护, 本模块绝不写入它。

用法:
  python plans/stock_pool.py --migrate-watchlist      # 把现有 watchlist.json 移入池
  python plans/stock_pool.py --add symbol,name,概念   # 手动加一只
  python plans/stock_pool.py --refresh               # 每日重算关卡+移动止损
  python plans/stock_pool.py --expire                # 清理过期
  python plans/stock_pool.py --list                  # 查看池
"""
import os
import sys
import json
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
POOL_PATH = os.path.join(DATA_DIR, "stock_pool.json")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
TTL_DAYS = 30  # 自然日, 约 20 交易日


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def load_pool() -> dict:
    if os.path.exists(POOL_PATH):
        try:
            return json.load(open(POOL_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"updated": None, "entries": []}


def save_pool(pool: dict):
    pool["updated"] = _today()
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def _compute_levels(symbol: str) -> dict:
    """用最新 K 线重算该票的形态/评分/数值关卡. 返回 dict (无行情则空).

    复用 analysis.breakout.classify_stage + weekly_hotspot 的 _build_plan/_sell_hint
    (懒加载, 避免 intraday_watch 等轻量调用者被迫加载重型依赖)。
    """
    try:
        from plans.breakout_scan import _kline_cached
        from analysis.breakout import classify_stage
        from plans.weekly_hotspot import (
            _estimate_win_rate, _rating, _build_plan, _sell_hint, SMART_EXIT, _trend_state,
        )
    except Exception as e:
        return {"error": str(e)}

    try:
        kl = _kline_cached(symbol)
    except Exception:
        kl = None
    if not kl:
        return {}
    try:
        closes = [float(b["close"]) for b in kl]
        highs = [float(b["high"]) for b in kl]
        lows = [float(b["low"]) for b in kl]
        vols = [float(b["volume"]) for b in kl]
    except Exception:
        return {}
    if len(closes) < 40:
        return {}
    price = closes[-1]

    res = classify_stage(closes, highs, lows, vols, price)
    stage = res.get("stage", "unknown")
    score = res.get("score", 0)
    signals = res.get("signals", [])
    det = res.get("details", {})

    wr = _estimate_win_rate(stage, signals)
    rating = _rating(score, wr, stage)
    # A+B: 真实均线 + 选股门控(多头排列 或 站稳5日线 才给买点)
    ma = _trend_state(closes, price)
    buy_text, stop_base, stop_pct, position, buy_level = _build_plan(
        {"price": price, "stage": stage, "change_pct": 0,
         "signals": signals, "score": score}, wr, rating, ma)
    tp_base = buy_level if buy_level is not None else price
    tp, sell_hint = _sell_hint(tp_base, stage, stop_base)

    resistance = det.get("resistance")
    scale_out = SMART_EXIT.get("scale_out_pct", 0.10)
    if buy_level is not None:
        tp_level = round(float(tp), 2) if tp else round(buy_level * (1 + scale_out), 2)
    else:
        # 不可买(破位/弱势): 止损/止盈 置空, 仅保留 等收复MA5 提示
        tp_level = None

    return {
        "stage": stage,
        "score": score,
        "rating": rating,
        "signals": signals,
        "price": round(price, 2),
        "buy_level": buy_level,
        "stop_base": round(float(stop_base), 2) if buy_level is not None else None,
        "tp_level": tp_level,
        "buy_point": buy_text,
        "stop_pct": stop_pct if buy_level is not None else 0,
        "position": position,
        "take_profit": tp_level,
        "sell_hint": sell_hint,
        "resistance": round(float(resistance), 2) if resistance else None,
        "support": round(float(ma["ma5"]), 2) if ma and ma.get("ma5") else None,
        "trend": {k: ma[k] for k in ("ma5", "ma10", "ma20", "bull", "steady", "above_ma5")}
                  if ma else None,
    }


def add_entries(new_entries: list, reason_default: str = "策略选股") -> int:
    """累积 + 去重加入股票池.

    new_entries: [{symbol, name, concepts, reason?}, ...]
    已存在 symbol → 仅更新动态字段(重算关卡), 保留 entry_date / highest_since_entry。
    返回新增条数。
    """
    pool = load_pool()
    entries = pool["entries"]
    by_sym = {e["symbol"]: e for e in entries}
    added = 0
    today = _today()
    for ne in new_entries:
        sym = str(ne.get("symbol", "")).strip()
        if not sym:
            continue
        lvl = _compute_levels(sym)
        if not lvl or lvl.get("error"):
            # 无行情也允许加入(等 refresh 补), 但关卡留空
            lvl = {}
        if sym in by_sym:
            e = by_sym[sym]
            e.update({
                "name": ne.get("name", e.get("name", sym)),
                "concepts": ne.get("concepts", e.get("concepts", [])),
                "reason_tag": ne.get("reason", e.get("reason_tag", reason_default)),
                "stage": lvl.get("stage", e.get("stage")),
                "score": lvl.get("score", e.get("score")),
                "rating": lvl.get("rating", e.get("rating")),
                "signals": lvl.get("signals", e.get("signals", [])),
                "price_entry": e.get("price_entry") or lvl.get("price"),
                "buy_level": lvl.get("buy_level", e.get("buy_level")),
                "stop_base": lvl.get("stop_base", e.get("stop_base")),
                "tp_level": lvl.get("tp_level", e.get("tp_level")),
                "buy_point": lvl.get("buy_point", e.get("buy_point")),
                "stop_pct": lvl.get("stop_pct", e.get("stop_pct")),
                "position": lvl.get("position", e.get("position")),
                "take_profit": lvl.get("take_profit", e.get("take_profit")),
                "sell_hint": lvl.get("sell_hint", e.get("sell_hint")),
                "resistance": lvl.get("resistance", e.get("resistance")),
                "support": lvl.get("support", e.get("support")),
                "last_refresh": today,
            })
        else:
            e = {
                "symbol": sym,
                "name": ne.get("name", sym),
                "entry_date": today,
                "reason": ne.get("reason", reason_default),
                "reason_tag": ne.get("reason", reason_default),
                "concepts": ne.get("concepts", []),
                "stage": lvl.get("stage"),
                "score": lvl.get("score"),
                "rating": lvl.get("rating"),
                "signals": lvl.get("signals", []),
                "price_entry": lvl.get("price"),
                "buy_level": lvl.get("buy_level"),
                "stop_base": lvl.get("stop_base"),
                "stop_level": lvl.get("stop_base"),
                "tp_level": lvl.get("tp_level"),
                "buy_point": lvl.get("buy_point"),
                "stop_pct": lvl.get("stop_pct"),
                "position": lvl.get("position"),
                "take_profit": lvl.get("take_profit"),
                "sell_hint": lvl.get("sell_hint"),
                "resistance": lvl.get("resistance"),
                "support": lvl.get("support"),
                "highest_since_entry": lvl.get("price"),
                "entered": False,
                "exited": False,
                "last_refresh": today,
            }
            entries.append(e)
            by_sym[sym] = e
            added += 1
    save_pool(pool)
    return added


def refresh_stock_pool(verbose: bool = True) -> int:
    """每日收盘后重算: 数值关卡 + 移动止损 + 重评分. 返回更新条数."""
    try:
        from plans.weekly_hotspot import SMART_EXIT
    except Exception:
        SMART_EXIT = {"trailing_pct": 0.08}
    trailing_pct = SMART_EXIT.get("trailing_pct", 0.08)

    pool = load_pool()
    today = _today()
    updated = 0
    for e in pool["entries"]:
        if e.get("exited"):
            continue
        sym = e["symbol"]
        lvl = _compute_levels(sym)
        if not lvl or lvl.get("error"):
            continue
        # 移动止损: max(初始止损, 入选以来最高价*(1-trailing_pct))
        hh = e.get("highest_since_entry") or lvl.get("price") or 0
        try:
            ed = datetime.strptime(e.get("entry_date", today), "%Y-%m-%d")
            kl = None
            from plans.breakout_scan import _kline_cached
            kl = _kline_cached(sym)
            if kl:
                hh = max(hh, max(
                    float(b["high"]) for b in kl
                    if datetime.strptime(b["date"][:10], "%Y-%m-%d") >= ed))
        except Exception:
            pass
        hh = round(float(hh), 2)
        sb = lvl.get("stop_base")
        stop_level = round(max(sb, hh * (1 - trailing_pct)), 2) if sb is not None else None

        e.update({
            "stage": lvl.get("stage"),
            "score": lvl.get("score"),
            "rating": lvl.get("rating"),
            "signals": lvl.get("signals", []),
            "price_entry": e.get("price_entry") or lvl.get("price"),
            "buy_level": lvl.get("buy_level"),
            "stop_base": lvl.get("stop_base"),
            "stop_level": stop_level,
            "tp_level": lvl.get("tp_level"),
            "buy_point": lvl.get("buy_point"),
            "stop_pct": lvl.get("stop_pct"),
            "position": lvl.get("position"),
            "take_profit": lvl.get("take_profit"),
            "sell_hint": lvl.get("sell_hint"),
            "resistance": lvl.get("resistance"),
            "support": lvl.get("support"),
            "trend": lvl.get("trend"),
            "highest_since_entry": hh,
            "last_refresh": today,
        })
        updated += 1
    save_pool(pool)
    if verbose:
        print(f"  ✓ 股票池已 refresh: 更新 {updated} 只, 移动止损比例 {trailing_pct:.0%}")
    return updated


def expire_pool(verbose: bool = True) -> int:
    """清理超过 TTL 或已 exited 的条目. 返回过期条数."""
    pool = load_pool()
    today = datetime.now()
    kept, expired = [], 0
    for e in pool["entries"]:
        if e.get("exited"):
            expired += 1
            continue
        ed = e.get("entry_date")
        try:
            d = datetime.strptime(ed, "%Y-%m-%d")
            if (today - d).days > TTL_DAYS:
                expired += 1
                continue
        except Exception:
            pass
        kept.append(e)
    pool["entries"] = kept
    save_pool(pool)
    if verbose:
        print(f"  ✓ 股票池过期清理: 移除 {expired} 只 (TTL={TTL_DAYS}天)")
    return expired


def migrate_watchlist(verbose: bool = True) -> int:
    """把现有 data/watchlist.json 内容移入股票池 (人工→机器池), 并清空 watchlist.json."""
    if not os.path.exists(WATCHLIST_PATH):
        if verbose:
            print("  (watchlist.json 不存在, 跳过)")
        return 0
    codes = json.load(open(WATCHLIST_PATH, encoding="utf-8")) or []
    if not codes:
        if verbose:
            print("  (watchlist.json 为空, 跳过)")
        return 0
    # 取名称: 尝试从已有池/行情; 这里仅 symbol, 名称留空由 refresh 补
    new_entries = [{"symbol": str(c).strip(), "name": str(c).strip(),
                   "concepts": [], "reason": "迁移自自选股(人工→策略池)"} for c in codes]
    added = add_entries(new_entries, reason_default="迁移自自选股")
    # 清空人工自选股 (此后由用户自行维护)
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False)
    if verbose:
        print(f"  ✓ 已迁移 {added} 只到股票池, watchlist.json 已清空(改为人工维护)")
    return added


def main():
    ap = argparse.ArgumentParser(description="策略股票池管理")
    ap.add_argument("--migrate-watchlist", action="store_true", help="迁移 watchlist.json → 股票池")
    ap.add_argument("--add", type=str, default="", help="手动加一只: symbol,name,概念1/概念2")
    ap.add_argument("--refresh", action="store_true", help="每日重算关卡+移动止损")
    ap.add_argument("--expire", action="store_true", help="清理过期")
    ap.add_argument("--list", action="store_true", help="查看池")
    args = ap.parse_args()

    if args.migrate_watchlist:
        migrate_watchlist()
    if args.add:
        parts = args.add.split(",")
        sym = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else sym
        cons = parts[2].split("/") if len(parts) > 2 and parts[2] else []
        add_entries([{"symbol": sym, "name": name, "concepts": cons,
                      "reason": "手动加入"}])
        print(f"  ✓ 已加入 {sym}({name})")
    if args.refresh:
        refresh_stock_pool()
    if args.expire:
        expire_pool()
    if args.list or not (args.migrate_watchlist or args.add or args.refresh or args.expire):
        pool = load_pool()
        print(f"股票池更新于 {pool.get('updated')}, 共 {len(pool['entries'])} 只:")
        for e in pool["entries"]:
            print(f"  {e['symbol']} {e.get('name','')} | 入选{e.get('entry_date','')} "
                  f"| {e.get('stage','')} 评分{e.get('score','')} "
                  f"| 买{e.get('buy_level')} 止损{e.get('stop_level')} 盈{e.get('tp_level')}")


if __name__ == "__main__":
    main()
