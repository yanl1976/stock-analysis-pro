#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盘中自选股 + 大盘异动监控 (供 scheduler.py 盘中任务调用)

每 30 分钟(由调度器在 09:30-11:30 / 13:00-15:00 窗口内重复触发)扫描:
  - 大盘三大指数(上证/深证/创业板)涨跌
  - 自选股(data/watchlist.json)实时行情, 标记异动:
      涨停 / 跌停 / 创日内新高 / 涨跌幅超阈值 / 高换手率
输出 markdown 摘要到 stdout, 调度器在 notify=True 时自动推送到企微。

用法:
  python plans/intraday_watch.py --threshold 3 --top 10
"""
import os
import sys
import json
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from collectors.quote import batch_quotes_tencent, market_indices


def load_watchlist() -> list:
    p = os.path.join(BASE_DIR, "data", "watchlist.json")
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8")) or []
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser(description="盘中自选股+大盘异动监控")
    ap.add_argument("--threshold", type=float, default=3.0, help="涨跌幅预警阈值(%%), 默认 3")
    ap.add_argument("--top", type=int, default=10, help="最多展示异动条数, 默认 10")
    args = ap.parse_args()

    lines = []
    now = __import__("datetime").datetime.now().strftime("%H:%M")

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

    # ---- 自选股 ----
    wl = load_watchlist()
    if not wl:
        lines.append("（自选股列表 data/watchlist.json 为空, 跳过个股监控）")
        print("\n".join(lines))
        return

    q = batch_quotes_tencent(wl)
    if not q:
        lines.append("（行情获取失败, 自选股无数据）")
        print("\n".join(lines))
        return

    movers = []
    for sym, info in q.items():
        pct = info.get("pct", 0)
        price = info.get("price", 0)
        high = info.get("high", 0)
        turnover = info.get("turnover", 0)
        name = info.get("name", "")
        flags = []
        if pct >= 9.9:
            flags.append("涨停")
        elif pct <= -9.9:
            flags.append("跌停")
        if high > 0 and price >= high * 0.999:
            flags.append("创日内新高")
        if abs(pct) >= args.threshold:
            flags.append(f"±{abs(pct):.1f}%")
        if turnover >= 5:
            flags.append(f"换手{turnover:.1f}%")
        if flags:
            movers.append((abs(pct), f"{name}({sym}) {price:.2f} {pct:+.2f}%  {' '.join(flags)}"))

    movers.sort(reverse=True)
    if movers:
        lines.append(f"🔔【自选异动】共 {len(movers)} 只 (阈值±{args.threshold:.0f}%):")
        for _, s in movers[: args.top]:
            lines.append("  • " + s)
    else:
        lines.append(f"😌【自选异动】无 (阈值±{args.threshold:.0f}%, 监控 {len(q)} 只)")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
