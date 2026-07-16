# -*- coding: utf-8 -*-
"""每日复盘计划 — 吸收 stock_review 全部逻辑

编排流程:
  1. 指数行情 (collectors/quote.py)
  2. 市场宽度: 涨跌家数 (collectors/breadth.py)
  3. 涨跌停统计 (collectors/breadth.py, akshare)
  4. 概念资金流 Top10 (collectors/em_concept.py)
  5. 持仓股行情 (collectors/quote.py)
  6. 自选股行情 (collectors/quote.py)
  7. 宏观快照 (collectors/macro.py)

用法:
    from plans.daily_report import run, format_report
    data = run(verbose=True)
    html = render(data, "review_report")
"""

import sys
import os
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config
from collectors.quote import batch_quotes_tencent, market_indices
from collectors.breadth import fetch_breadth, fetch_limit_stats
from collectors.em_concept import fetch_concept_list, fetch_concept_stocks
from collectors.macro import global_macro


# ── 配置加载 ──

def _get_portfolio() -> list:
    """从 config.yaml 或 data/portfolio.json 加载持仓"""
    config = load_config()
    portfolio = config.get('portfolio', [])
    if portfolio:
        return portfolio
    # 兜底: data/portfolio.json
    pf_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'portfolio.json')
    if os.path.exists(pf_path):
        with open(pf_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def _get_indices() -> list:
    """从 config.yaml 加载跟踪的指数"""
    config = load_config()
    return config.get('indices', [
        {'code': 'sh000001', 'name': '上证指数'},
        {'code': 'sz399001', 'name': '深证成指'},
        {'code': 'sz399006', 'name': '创业板指'},
        {'code': 'sh000688', 'name': '科创50'},
    ])


def _get_watchlist() -> list:
    """加载自选股"""
    wl_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'watchlist.json')
    if os.path.exists(wl_path):
        with open(wl_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


# ── 主流程 ──

def run(date=None, verbose=True):
    """执行每日复盘

    Args:
        date: YYYYMMDD, 默认今天
        verbose: 打印进度到 stderr

    Returns:
        dict: 完整复盘数据
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    result = {
        'date': date,
        'timestamp': datetime.now().isoformat(),
    }

    # 1. 指数行情
    if verbose:
        print("📊 采集指数行情...", file=sys.stderr)
    indices_cfg = _get_indices()
    index_codes = [i['code'] for i in indices_cfg]
    index_names = {i['code']: i['name'] for i in indices_cfg}
    try:
        idx_quotes = batch_quotes_tencent(index_codes)
        result['indices'] = []
        for code in index_codes:
            q = idx_quotes.get(code, {})
            result['indices'].append({
                'code': code,
                'name': index_names.get(code, q.get('name', code)),
                'price': q.get('price', 0),
                'change_pct': q.get('pct', 0),
                'amount': q.get('amount', 0),
            })
    except Exception as e:
        if verbose:
            print(f"  ⚠️ 指数行情异常: {e}", file=sys.stderr)
        result['indices'] = []

    # 2. 市场宽度 (涨跌家数)
    if verbose:
        print("📈 采集涨跌家数...", file=sys.stderr)
    try:
        result['breadth'] = fetch_breadth()
    except Exception as e:
        if verbose:
            print(f"  ⚠️ 涨跌家数异常: {e}", file=sys.stderr)
        result['breadth'] = {'up': 0, 'down': 0, 'flat': 0, 'limit_up': 0, 'limit_down': 0}

    # 3. 涨跌停统计
    if verbose:
        print("🔴 采集涨跌停统计...", file=sys.stderr)
    try:
        result['limits'] = fetch_limit_stats(date=date)
    except Exception as e:
        if verbose:
            print(f"  ⚠️ 涨跌停统计异常: {e}", file=sys.stderr)
        result['limits'] = {'zt_count': 0, 'dt_count': 0, 'zt_stocks': [], 'dt_stocks': []}

    time.sleep(0.5)

    # 4. 概念资金流 Top10
    if verbose:
        print("💰 采集概念资金流...", file=sys.stderr)
    try:
        concepts = fetch_concept_list(top_n=10)
        result['concepts'] = concepts
    except Exception as e:
        if verbose:
            print(f"  ⚠️ 概念资金流异常: {e}", file=sys.stderr)
        result['concepts'] = []

    time.sleep(0.5)

    # 5. 持仓股行情
    if verbose:
        print("💼 采集持仓行情...", file=sys.stderr)
    portfolio = _get_portfolio()
    if portfolio:
        pf_codes = [p['code'] for p in portfolio]
        try:
            pf_quotes = batch_quotes_tencent(pf_codes)
            result['portfolio'] = []
            for p in portfolio:
                code = p['code']
                q = pf_quotes.get(code, pf_quotes.get(f"sh{code}", pf_quotes.get(f"sz{code}", {})))
                result['portfolio'].append({
                    'code': code,
                    'name': p.get('name', q.get('name', code)),
                    'note': p.get('note', ''),
                    'cost': p.get('cost', 0),
                    'shares': p.get('shares', 0),
                    'price': q.get('price', 0),
                    'change_pct': q.get('pct', 0),
                    'amount': q.get('amount', 0),
                    'turnover': q.get('turnover', 0),
                })
        except Exception as e:
            if verbose:
                print(f"  ⚠️ 持仓行情异常: {e}", file=sys.stderr)
            result['portfolio'] = []
    else:
        result['portfolio'] = []

    # 6. 自选股行情
    if verbose:
        print("👀 采集自选股行情...", file=sys.stderr)
    watchlist = _get_watchlist()
    if watchlist:
        try:
            wl_quotes = batch_quotes_tencent(watchlist)
            result['watchlist'] = []
            for code in watchlist:
                q = wl_quotes.get(code, {})
                result['watchlist'].append({
                    'code': code,
                    'name': q.get('name', code),
                    'price': q.get('price', 0),
                    'change_pct': q.get('pct', 0),
                    'amount': q.get('amount', 0),
                    'turnover': q.get('turnover', 0),
                })
        except Exception as e:
            if verbose:
                print(f"  ⚠️ 自选股行情异常: {e}", file=sys.stderr)
            result['watchlist'] = []
    else:
        result['watchlist'] = []

    # 7. 宏观快照
    if verbose:
        print("🌍 采集宏观数据...", file=sys.stderr)
    try:
        result['macro'] = global_macro()
    except Exception as e:
        if verbose:
            print(f"  ⚠️ 宏观数据异常: {e}", file=sys.stderr)
        result['macro'] = {}

    if verbose:
        print("✅ 每日复盘完成", file=sys.stderr)

    return result


def format_report(data: dict) -> str:
    """格式化为文本报告"""
    lines = []
    sep = "=" * 50

    lines.append(sep)
    lines.append(f"  📋 每日复盘 — {data.get('date', '')}")
    lines.append(sep)

    # 指数
    indices = data.get('indices', [])
    if indices:
        lines.append("\n【指数概览】")
        for idx in indices:
            pct = idx.get('change_pct', 0)
            arrow = "🔴" if pct < 0 else "🟢" if pct > 0 else "⚪"
            amount_yi = idx.get('amount', 0) / 1e8 if idx.get('amount') else 0
            lines.append(f"  {arrow} {idx['name']}: {idx['price']:.2f} ({pct:+.2f}%) 成交{amount_yi:.0f}亿")

    # 市场宽度
    breadth = data.get('breadth', {})
    if breadth.get('up') or breadth.get('down'):
        lines.append(f"\n【市场宽度】")
        lines.append(f"  上涨: {breadth['up']}  下跌: {breadth['down']}  平盘: {breadth.get('flat', 0)}")
        lines.append(f"  涨停: {breadth.get('limit_up', 0)}  跌停: {breadth.get('limit_down', 0)}")

    # 涨跌停
    limits = data.get('limits', {})
    if limits.get('zt_count') or limits.get('dt_count'):
        lines.append(f"\n【涨跌停统计】(akshare)")
        lines.append(f"  涨停: {limits['zt_count']}家  跌停: {limits['dt_count']}家")
        zt = limits.get('zt_stocks', [])
        if zt:
            lines.append(f"  涨停TOP5:")
            for s in zt[:5]:
                lb = f" 连板{s['lianban']}" if s.get('lianban', 1) > 1 else ""
                lines.append(f"    {s['name']}({s['code']}) {s['change_pct']:+.1f}%{lb}")

    # 概念资金流
    concepts = data.get('concepts', [])
    if concepts:
        lines.append(f"\n【概念资金流 Top10】")
        for i, c in enumerate(concepts, 1):
            inflow = c.get('net_inflow', 0) / 1e8 if c.get('net_inflow') else 0
            pct = c.get('change_pct', 0)
            lines.append(f"  {i:2d}. {c['name']:<8s} 涨跌{pct:+.2f}% 净流入{inflow:+.2f}亿")

    # 持仓
    portfolio = data.get('portfolio', [])
    if portfolio:
        lines.append(f"\n【持仓明细】")
        total_cost = 0
        total_mv = 0
        for p in portfolio:
            price = p.get('price', 0)
            shares = p.get('shares', 0)
            cost = p.get('cost', 0)
            mv = price * shares
            pnl = (price - cost) * shares if cost and shares else 0
            total_mv += mv
            total_cost += cost * shares if cost else 0
            pct = p.get('change_pct', 0)
            lines.append(f"  {p['name']}({p['code']}) ¥{price:.2f} {pct:+.2f}% 持仓{shares}股 {'| ' + p['note'] if p.get('note') else ''}")

    # 自选股
    watchlist = data.get('watchlist', [])
    if watchlist:
        lines.append(f"\n【自选股】")
        for w in watchlist[:10]:
            pct = w.get('change_pct', 0)
            lines.append(f"  {w['name']}({w['code']}) ¥{w.get('price', 0):.2f} {pct:+.2f}%")

    lines.append(f"\n{sep}")
    return "\n".join(lines)
