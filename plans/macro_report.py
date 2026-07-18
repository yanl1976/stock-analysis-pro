#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""宏观分析统一入口 — 供调度器 / 企微推送调用。

采集(国际宏观 + 国内经济 + 涨停池) → 分析(环境 / 周期 / 情绪) → 综合研判 → 格式化输出。
支持 --json / --html(生成 HTML 报告供企微推送) / --date(涨停池日期, 默认自动)。

用法:
  python plans/macro_report.py                 # 文本报告
  python plans/macro_report.py --json          # JSON
  python plans/macro_report.py --html          # 生成 HTML 报告(输出 HTML_REPORT:<path>)
  python plans/macro_report.py --date 20260717 # 指定涨停池日期(YYYYMMDD)
"""
import os
import sys
import json
import html
import argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from collectors.macro import global_macro, domestic_macro, zt_pool
from analysis.macro import analyze_global, analyze_domestic, analyze_event, synthesize


def _auto_date():
    """涨停池日期: 盘前用最近交易日(跳过周末), 盘后用今天。"""
    now = datetime.now()
    if now.hour < 15:
        d = now - timedelta(days=1)
        while d.weekday() >= 5:  # 回退到周五
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")
    return now.strftime("%Y%m%d")


def run(date=None):
    if date is None:
        date = _auto_date()
    g_raw = global_macro()
    d_raw = domestic_macro()
    e_raw = zt_pool(date)
    g = analyze_global(g_raw)
    d = analyze_domestic(d_raw)
    e = analyze_event(e_raw)
    syn = synthesize(g, d, e)
    return {
        "date": date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "global": g,
        "domestic": d,
        "event": e,
        "synthesis": syn,
    }


def format_report(data: dict) -> str:
    syn = data["synthesis"]
    g = data["global"]
    d = data["domestic"]
    e = data["event"]
    L = []
    L.append("=" * 52)
    L.append(f"  🌐 宏观研判   (涨停池日期: {data['date']})")
    L.append("=" * 52)
    L.append(f"\n【综合研判】{syn['outlook']}  "
             f"(评分 {syn['score']:+d}, 利多{syn['positive_factors']}/利空{syn['negative_factors']})")
    L.append(f"  操作建议: {syn['action']}")
    if syn.get("signals"):
        L.append("  ✅ " + " | ".join(syn["signals"][:8]))
    if syn.get("warnings"):
        L.append("  ⚠️ " + " | ".join(syn["warnings"][:8]))

    L.append(f"\n【国际环境】{g.get('environment', '-')}")
    us = g.get("us_10y_yield", {})
    if us.get("value") is not None:
        L.append(f"  美债10Y: {us['value']}%  | 联邦基金利率: {g.get('fed_rate', {}).get('value')}")
    for c in g.get("commodities", []):
        L.append(f"  {c['name']}: {c['price']} ({c.get('change_pct', '-')})")

    L.append(f"\n【国内经济】周期={d.get('cycle', '-')}  流动性={d.get('liquidity', '-')}")
    L.append(f"  CPI={d.get('cpi', {}).get('value')}  "
             f"PMI={d.get('pmi_manufacturing', {}).get('value')}  "
             f"M2={d.get('m2_yoy', {}).get('value')}%")
    lpr = d.get("lpr", {})
    if lpr:
        L.append(f"  LPR: 1Y={lpr.get('1y')}  5Y={lpr.get('5y')}")

    L.append(f"\n【市场情绪】{e.get('sentiment', '-')}  "
             f"涨停={e.get('zt_count', 0)}只  最高连板={e.get('max_height', 0)}")
    if e.get("hot_sectors"):
        top = list(e["hot_sectors"].items())[:3]
        L.append("  涨停集中: " + ", ".join(f"{s}({c}只)" for s, c in top))
    if e.get("continuation"):
        c = e["continuation"]
        L.append(f"  昨日涨停今日: 均涨{c.get('avg_pct')}%  上涨占比{c.get('up_ratio')}%")

    L.append("")
    return "\n".join(L)


def _write_html(data: dict) -> str:
    """生成简单 HTML 报告, 返回路径(供企微推送)。"""
    text = format_report(data)
    out_dir = os.path.join(BASE_DIR, "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"macro_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html")
    body = html.escape(text).replace("\n", "<br>\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<html><body style='font-family:monospace;white-space:pre-wrap'>{body}</body></html>")
    return path


def main():
    ap = argparse.ArgumentParser(description="宏观分析统一入口")
    ap.add_argument("--date", help="涨停池日期 YYYYMMDD (默认自动: 盘前用昨交易日)")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--html", action="store_true", help="生成 HTML 报告(输出 HTML_REPORT:<path>)")
    args = ap.parse_args()

    data = run(date=args.date)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.html:
        print(format_report(data))
        print(f"HTML_REPORT:{_write_html(data)}")
    else:
        print(format_report(data))


if __name__ == "__main__":
    main()
