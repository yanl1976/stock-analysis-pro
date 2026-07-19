#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盘中监控 (供 scheduler.py 盘中任务调用)

每 15 分钟(调度器在 09:30-11:30 / 13:00-15:00 窗口内重复触发)扫描:
  - 大盘三大指数(上证/深证/创业板)涨跌
  - 人工自选股(data/watchlist.json)实时行情 -> 异动标记
  - 策略股票池(data/stock_pool.json)实时行情 ->
        * 交易条件触发: 破买点 / 触止损 / 触止盈 (数值关卡, 每日 refresh 更新)
          - 首次命中即写回池标记 entered/exited, 之后不再重复提醒
          - 已 exited 的票不再监控(直到 TTL 清理)
        * 异动标记: 涨停/跌停/创日内新高/涨跌幅超阈值/高换手

输出分两段, 中间以 SPLIT_SENTINEL 分隔:
  段1 = 策略池(交易信号 + 异动)
  段2 = 人工自选股(异动)
调度器 notify 据分隔符拆成两条企微消息(分群推送)。

用法:
  python plans/intraday_watch.py --threshold 3 --top 10 [--no-writeback]
"""
import os
import sys
import json
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from collectors.quote import batch_quotes_tencent, market_indices
from plans.stock_pool import load_pool, save_pool

# 与 scheduler.notify 约定的分组分隔符: 据此拆成两条企微消息
SPLIT_SENTINEL = "<<<SPLIT>>>"


def load_watchlist() -> list:
    p = os.path.join(BASE_DIR, "data", "watchlist.json")
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8")) or []
    except Exception:
        return []


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _detect_anomaly(pct, price, high, turnover) -> list:
    """返回异动标记列表 (复用原自选股逻辑)."""
    flags = []
    if pct >= 9.9:
        flags.append("涨停")
    elif pct <= -9.9:
        flags.append("跌停")
    if high > 0 and price >= high * 0.999:
        flags.append("创日内新高")
    return flags


def main():
    ap = argparse.ArgumentParser(description="盘中自选股+策略池异动/交易信号监控")
    ap.add_argument("--threshold", type=float, default=3.0, help="涨跌幅预警阈值(%%), 默认 3")
    ap.add_argument("--top", type=int, default=10, help="最多展示条数, 默认 10")
    ap.add_argument("--no-writeback", action="store_true",
                    help="不把 entered/exited 写回股票池(仅本次提醒, 用于调试)")
    args = ap.parse_args()

    today = _today()
    lines = []
    now = datetime.now().strftime("%H:%M")

    # ---- 大盘指数 ----
    idx = market_indices()
    if idx:
        parts = []
        for name, info in idx.items():
            arrow = "▲" if info["change_pct"] >= 0 else "▼"
            parts.append(f"{name} {info['price']:.2f} {arrow}{abs(info['change_pct']):.2f}%")
        lines.append(f"📊【大盘 {now}】 " + "  |  ".join(parts))
    else:
        lines.append(f"📊【大盘 {now}】 (指数获取失败)")

    # ---- 数据准备: 人工自选股 + 策略股票池 ----
    wl = load_watchlist()
    pool = load_pool()
    pool_entries = pool.get("entries", [])

    all_syms = list(dict.fromkeys([str(c) for c in wl] +
                                  [e["symbol"] for e in pool_entries]))
    q = batch_quotes_tencent(all_syms) if all_syms else {}
    if not q and (wl or pool_entries):
        lines.append("（行情获取失败, 无数据）")
        print("\n".join(lines))
        return

    # ---- 策略股票池: 交易信号(写回) + 异动 ----
    sig_triggers = []   # 交易条件触发
    pool_movers = []    # 异动
    changed = False
    for e in pool_entries:
        if e.get("exited"):
            continue  # 已退出不再监控
        sym = e["symbol"]
        info = q.get(sym)
        if not info:
            continue
        pct = info.get("pct", 0)
        price = info.get("price", 0)
        high = info.get("high", 0)
        turnover = info.get("turnover", 0)
        name = info.get("name") or e.get("name") or sym

        # 交易条件触发 (数值关卡) + 首次命中写回池
        buy = e.get("buy_level")
        stop = e.get("stop_level")
        tp = e.get("tp_level")
        if buy and price >= buy and not e.get("entered") and not e.get("exited"):
            sig_triggers.append(
                f"{name}({sym}) 现价{price:.2f} >= 买点{buy:.2f} -> 买点触发")
            e["entered"] = True
            e["entered_date"] = today
            e["entered_price"] = round(price, 2)
            changed = True
        if stop and price <= stop and e.get("entered") and not e.get("exited"):
            sig_triggers.append(
                f"{name}({sym}) 现价{price:.2f} <= 止损{stop:.2f} -> 止损触发")
            e["exited"] = True
            e["exited_date"] = today
            e["exited_price"] = round(price, 2)
            changed = True
        elif tp and price >= tp and e.get("entered") and not e.get("exited"):
            sig_triggers.append(
                f"{name}({sym}) 现价{price:.2f} >= 止盈{tp:.2f} -> 止盈触发")
            e["exited"] = True
            e["exited_date"] = today
            e["exited_price"] = round(price, 2)
            changed = True

        # 异动
        flags = _detect_anomaly(pct, price, high, turnover)
        if abs(pct) >= args.threshold:
            flags.append(f"±{abs(pct):.1f}%")
        if turnover >= 5:
            flags.append(f"换手{turnover:.1f}%")
        if flags:
            pool_movers.append(
                (abs(pct), f"{name}({sym}) {price:.2f} {pct:+.2f}%  {' '.join(flags)}"))

    if changed and not args.no_writeback:
        save_pool(pool)

    active_pool = [e for e in pool_entries if not e.get("exited")]
    pool_movers.sort(reverse=True)

    # ---- 组装摘要卡片 (不堆文字: 交易信号列出, 异动只给计数+Top3) ----
    pool_lines = [f"🤖 策略池 · 监控 {len(active_pool)} 只"]
    if sig_triggers:
        pool_lines.append(f"🔔 交易信号 {len(sig_triggers)} 只:")
        for s in sig_triggers[:5]:
            pool_lines.append("   • " + s)
        if len(sig_triggers) > 5:
            pool_lines.append(f"   • …另 {len(sig_triggers) - 5} 只")
    else:
        pool_lines.append("😌 交易信号: 无")
    if pool_movers:
        pool_lines.append(f"📈 异动 {len(pool_movers)} 只 (±{args.threshold:.0f}%):")
        for _, s in pool_movers[:3]:
            pool_lines.append("   • " + s)
        if len(pool_movers) > 3:
            pool_lines.append(f"   • …另 {len(pool_movers) - 3} 只 (完整见 HTML 周报)")
    else:
        pool_lines.append(f"😌 异动: 无 (±{args.threshold:.0f}%)")

    # ---- 人工自选股: 异动 ----
    wl_lines = []
    if not wl:
        wl_lines.append("⭐ 自选 · 未配置 (data/watchlist.json 为空)")
    else:
        movers = []
        for sym in wl:
            info = q.get(str(sym))
            if not info:
                continue
            pct = info.get("pct", 0)
            price = info.get("price", 0)
            high = info.get("high", 0)
            turnover = info.get("turnover", 0)
            name = info.get("name", "")
            flags = _detect_anomaly(pct, price, high, turnover)
            if abs(pct) >= args.threshold:
                flags.append(f"±{abs(pct):.1f}%")
            if turnover >= 5:
                flags.append(f"换手{turnover:.1f}%")
            if flags:
                movers.append((abs(pct), f"{name}({sym}) {price:.2f} {pct:+.2f}%  {' '.join(flags)}"))
        movers.sort(reverse=True)
        wl_lines.append(f"⭐ 自选 · 监控 {len(q)} 只")
        if movers:
            wl_lines.append(f"🔔 异动 {len(movers)} 只 (±{args.threshold:.0f}%):")
            for _, s in movers[:3]:
                wl_lines.append("   • " + s)
            if len(movers) > 3:
                wl_lines.append(f"   • …另 {len(movers) - 3} 只")
        else:
            wl_lines.append(f"😌 异动: 无 (±{args.threshold:.0f}%)")

    # 两段用分隔符拼接, 供 scheduler 分群推送
    print("\n".join(lines + pool_lines))
    print(SPLIT_SENTINEL)
    print("\n".join(wl_lines))


if __name__ == "__main__":
    main()
