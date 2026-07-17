# -*- coding: utf-8 -*-
"""Req2 概念板块分析计划 — 全景扫描 → 趋势定性 → 深度分析 → 选股"""
import sys
import os
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.concept import concept_news, clear_kline_cache
from analysis.concept import analyze_board_trend, analyze_concept_deep
from analysis.concept_rank import rank_concepts

# ── 缓存 ──
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
CACHE_TTL = 600  # 10 分钟

def _cache_get(key: str):
    """读取缓存，过期返回 None"""
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        if time.time() - mtime > CACHE_TTL:
            return None
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def _cache_set(key: str, data):
    """写入缓存"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{key}.json")
    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

FILTER_KEYWORDS = [
    '昨日', '两融', '融资融券', '证金', '社保', '预盈', '预增', '破净',
    '股权激励', '新股', '次新', '含H', '含B', 'AB股', '基金重仓',
    '社保重仓', 'QFII', '保险重仓', '券商重仓', '外资重仓', '信托重仓',
    '央企50', '上证380', '深成500', '沪深300', '中证500', '中证1000',
    'MSCI中国', '央视50', '上证50', '上证180', '业绩预升', '业绩预降', '高送转',
    '整体上市', '重组', '超大盘', '中盘', '小盘', '创业', '科创', 'ST',
    '摘帽', '高市净', '低价', '高价', '高市盈率', '低市盈率', '破发',
    '高商誉', '减持', '增持', '员工持股', '高送转预期', '参股新三板',
    '沪股通', '深股通', '区域', '板块',
    # 新增: 风格/市值类概念
    '风格', '红利', '大盘', '小盘', '中盘', '权重', '破增发', '超跌',
    '新高', '趋势', '反转', '题材', '预减', '扭亏',
]

REGIONS = [
    '北京', '上海', '深圳', '广东', '江苏', '浙江', '山东', '福建',
    '安徽', '四川', '湖北', '湖南', '西部', '东北', '长三角', '珠三角',
    '京津冀', '雄安', '海南', '重庆', '天津', '成渝', '特区', '海峡',
    '中部', '西北', '西南', '粤港澳', '自贸区'
]


def filter_concepts(concepts):
    """过滤宽泛/地域/风格类概念"""
    res = []
    for c in concepts:
        name = c['name']
        if any(kw in name for kw in FILTER_KEYWORDS):
            continue
        if len(name) < 2 or len(name) > 10:
            continue
        if any(r in name for r in REGIONS):
            continue
        res.append(c)
    return res


def run(target_count=10, verbose=True, use_cache=True, stage='all'):
    """
    执行概念板块分析 (含深度选股分析)

    支持两步式执行 (推荐, 避免单次运行过久):
    - stage='list'   : 快速模式, 仅抓取概念榜单 (不拉成分股, 秒级)
    - stage='detail' : 慢速模式, 读取榜单缓存, 再逐个拉成分股做深度分析
    - stage='all'    : 一次性完成 (默认, 兼容旧调用)

    流程:
    1. 概念排名 (映射表+腾讯行情) → Top N
    2. 新闻归因 (东财搜索API)
    3. 深度分析 (成分股K线+选股)
    4. 板块趋势定性

    返回: {date, concepts: [{name, ..., deep_analysis}, ...]}
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    list_cache_key = f"concept_list_{date_str}"
    deep_cache_key = f"concept_deep_{date_str}_{target_count}"

    if verbose:
        print(f"[{date_str}] 概念板块分析启动 (stage={stage})...")

    # 清空K线缓存
    clear_kline_cache()

    # === 第一步: 仅抓概念榜单 (快速) ===
    if stage in ('list', 'all'):
        if use_cache and stage == 'list':
            cached_list = _cache_get(list_cache_key)
            if cached_list:
                if verbose:
                    print(f"  [缓存命中] {list_cache_key}")
                return cached_list

        if verbose:
            print("  Step 1: 获取概念榜单 (新浪源, 仅列表, 秒级)...")
        from collectors.concept import concept_rank_sina

        # 新浪源 (无需 Cookie/Playwright; 当前网络环境东财 push2 不可用)
        raw = concept_rank_sina(limit=target_count * 3)
        mapped = [{
            'name': c['name'],
            'bk_code': c['code'],
            'change_pct': c.get('change_pct', 0),
            'net_inflow': 0,
            'up_count': 0,
            'down_count': 0,
            'leader': c.get('leader_name', ''),
            'leader_code': c.get('leader_code', ''),
        } for c in raw]
        top = filter_concepts(mapped)[:target_count]

        list_out = {
            "date": date_str,
            "stage": "list",
            "concepts": [
                {
                    'name': c['name'],
                    'bk_code': c['bk_code'],
                    'change_pct': c.get('change_pct', 0),
                    'net_inflow': c.get('net_inflow', 0),
                    'up_count': c.get('up_count', 0),
                    'down_count': c.get('down_count', 0),
                    'leader': c.get('leader', ''),
                    'leader_code': c.get('leader_code', ''),
                }
                for c in top
            ],
            "count": len(top),
        }
        _cache_set(list_cache_key, list_out)

        if stage == 'list':
            return list_out

    # === 第二步: 拉成分股 + 深度分析 (慢速) ===
    if stage == 'detail':
        cached_list = _cache_get(list_cache_key) if use_cache else None
        if not cached_list:
            print("\n  ⚠️ 未找到榜单缓存，请先运行 stage='list' (python core/cli.py concept --stage list)")
            return {"error": "未找到榜单缓存，请先运行快速模式", "date": date_str}
        top = cached_list.get('concepts', [])
        if verbose:
            print(f"  [读取榜单缓存] {len(top)} 个概念, 开始拉成分股...")

    from collectors.concept import fetch_concept_stocks_sina

    # 拉成分股 (新浪源, 逐个板块)
    if verbose:
        print("  Step 2: 拉取成分股 (新浪源, 逐个板块)...")
    stocks_map = {}
    for c in top:
        bk_code = c['bk_code']
        stocks = fetch_concept_stocks_sina(bk_code, name=c['name'], limit=100)
        stocks_map[bk_code] = stocks
        if verbose:
            print(f"    {c['name']}: {len(stocks)}只成分股")
        time.sleep(0.3)

    if not top:
        print("\n  ⚠️ 无法获取概念排行！可能原因：")
        print("  1. 新浪接口网络异常 → 检查是否能访问 vip.stock.finance.sina.com.cn")
        print("  2. 当日无交易数据 (休市)")
        print("  💡 如有离线缓存将自动降级使用\n")
        return {"error": "无法获取概念排行", "date": date_str}

    if verbose:
        print(f"  → 过滤后{len(top)}个概念")

    for c in top:
        bk_code = c['bk_code']
        stocks = stocks_map.get(bk_code, [])
        stock_details = []
        for s in stocks:
            try:
                pct_val = float(s.get('change_pct', 0) or 0)
            except (ValueError, TypeError):
                pct_val = 0
            try:
                amount_val = float(s.get('amount', 0) or 0)
            except (ValueError, TypeError):
                amount_val = 0
            try:
                turnover_val = float(s.get('turnover', 0) or 0)
            except (ValueError, TypeError):
                turnover_val = 0
            stock_details.append({
                'symbol': s['symbol'],
                'name': s.get('name', ''),
                'pct': round(pct_val, 2),
                'price': s.get('price', 0),
                'amount_yi': round(amount_val / 1e8, 2),
                'turnover': turnover_val,
            })
        stock_details.sort(key=lambda x: -x['pct'])
        c['stocks'] = stock_details
        if stock_details:
            c['total'] = len(stock_details)
            c['up_count'] = sum(1 for s in stock_details if s['pct'] > 0)
            c['down_count'] = sum(1 for s in stock_details if s['pct'] < 0)
            c['up_ratio'] = round(c['up_count'] / c['total'] * 100, 1)
            c['avg_pct'] = round(sum(s['pct'] for s in stock_details) / len(stock_details), 2)
            c['total_amount_yi'] = round(sum(s['amount_yi'] for s in stock_details), 1)
    top.sort(key=lambda x: -x['avg_pct'])

    results = []

    for i, c in enumerate(top):
        name = c['name']

        if verbose:
            print(f"  [{i+1}/{len(top)}] {name} 均涨{c['avg_pct']:+.2f}%")

        # 找龙头 (涨幅最高的成分股)
        leader_stock = c['stocks'][0] if c['stocks'] else {}
        entry = {
            'name': name,
            'code': c.get('bk_code', name),  # 东财板块代码
            'change_pct': c['avg_pct'],
            'amount_yi': c['total_amount_yi'],
            'net_inflow': c.get('net_inflow', 0),
            'stock_count': c['total'],
            'up_count': c['up_count'],
            'up_ratio': c['up_ratio'],
            'leader': leader_stock.get('name', ''),
            'leader_code': leader_stock.get('symbol', ''),
            'leader_pct': leader_stock.get('pct', 0),
            'source': c.get('source', 'unknown'),
        }

        # === Step 2: 新闻归因 ===
        time.sleep(0.3)
        news = concept_news(name, max_items=5)
        entry['news'] = news[:3]

        # === Step 3: 深度分析 ===
        # 将 rank_concepts 的成分股转换为 analyze_concept_deep 的格式
        deep_stocks = []
        for s in c['stocks']:
            deep_stocks.append({
                'symbol': s['symbol'],
                'name': s['name'],
                'changepercent': s['pct'],
                'turnoverratio': s.get('turnover', 0),
                'amount': s.get('amount_yi', 0) * 1e8,  # 亿 → 元
            })

        if deep_stocks:
            if verbose:
                print(f"    深度分析: {len(deep_stocks)}只成分股...")
            deep = analyze_concept_deep(deep_stocks, c['total_amount_yi'] * 1e8, verbose=verbose)
            entry['deep'] = deep

            # === Step 4: 板块级趋势定性 ===
            trend = analyze_board_trend(deep)
            entry['trend'] = trend
        else:
            entry['deep'] = {"error": "无法获取成分股"}
            entry['trend'] = {"status": "unknown", "reason": "无法获取成分股"}

        results.append(entry)

    # === Step 5: 按资金流入排序（保持东财排序） ===
    results.sort(key=lambda x: x.get('net_inflow', 0) or 0, reverse=True)

    output = {"date": date_str, "concepts": results, "count": len(results)}

    if use_cache and stage != 'list':
        _cache_set(deep_cache_key, output)

    return output


def format_report(data: dict) -> str:
    """格式化输出报告 (文本版)

    兼容两种数据源:
    - stage='all'/'detail' (深度分析): 含 amount_yi / leader_pct / deep / trend / news
    - stage='list' (轻量榜单): 仅 change_pct / net_inflow / leader / leader_code
    缺失深度字段时自动降级展示, 不抛 KeyError。
    """
    if "error" in data:
        return f"❌ 错误: {data['error']}"

    is_list = all('amount_yi' not in c and 'deep' not in c for c in data.get('concepts', []))
    title = "概念板块榜单 (快速)" if is_list else "概念板块深度分析报告"

    lines = []
    lines.append(f"📊 {title} ({data['date']})")
    lines.append(f"{'='*50}")

    if not data.get('concepts'):
        lines.append("\n⚠️ 当前未能获取概念榜单数据。")
        lines.append("可能原因：新浪概念接口网络异常 / 当日休市无数据。")
        lines.append("→ 稍后重试。")
        lines.append("\n报告生成完毕")
        return '\n'.join(lines)

    for i, c in enumerate(data['concepts'], 1):
        lines.append(f"\n{'─'*50}")
        deep = c.get('deep', {})
        score = deep.get('score', {})
        score_label = score.get('label', '--')
        score_val = score.get('total', 0)

        # 成交额: 深度模式用 amount_yi, 榜单模式用 net_inflow (净流入) 兜底
        if 'amount_yi' in c:
            amount_str = f"成交{c['amount_yi']}亿"
        elif c.get('net_inflow') is not None:
            amount_str = f"净流入{c['net_inflow']}亿"
        else:
            amount_str = ""
        lines.append(f"【{i}】{c['name']}  {c['change_pct']:+.2f}%  {amount_str}")

        # 深度模式才有的评分
        if not is_list:
            lines.append(f"    评分: {score_val}分 {score_label}")

        # 龙头: 榜单模式可能无 leader_pct
        leader = c.get('leader', '')
        leader_code = c.get('leader_code', '')
        if 'leader_pct' in c:
            lines.append(f"    龙头: {leader}({leader_code}) {c['leader_pct']:+.2f}%")
        elif leader:
            lines.append(f"    龙头: {leader}({leader_code})")


        # 趋势 (仅深度模式有 trend 字段; 榜单模式 t 为空 dict, 跳过)
        t = c.get('trend') or {}
        t_status = t.get('status')
        if t_status and t_status != 'unknown':
            status_map = {
                'breakout': '🚀 金叉启动', 'strong': '🔥 主升浪',
                'rising': '📈 上升期', 'weak_rise': '↗️ 弱上升',
                'weak': '↔️ 震荡', 'falling': '📉 下跌',
            }
            label = status_map.get(t_status, t_status)
            lines.append(f"    趋势: {label} — {t.get('reason', '')}")

        # 深度分析
        dist = deep.get('distribution', {})
        mom = deep.get('momentum', {})
        rep = deep.get('representativeness', {})

        if rep:
            lines.append(f"    代表性: 采样{rep['top100_amount_yi']}亿 / 总计{rep['total_amount_yi']}亿 ({rep['ratio']}%)")

        if dist:
            lines.append(f"    涨幅分布: >7%={dist['above_7']}只 3-7%={dist['between_3_7']}只 0-3%={dist['between_0_3']}只 <0%={dist['below_0']}只")

        if mom:
            lines.append(f"    持续性: 连涨3天+={mom['consecutive_3plus']}只 2天={mom['consecutive_2']}只 刚启动={mom['just_started']}只 下跌={mom['falling']}只")

        # 连涨股
        strong = deep.get('strong_stocks', [])
        if strong:
            lines.append(f"    🔥 连涨3天+:")
            for s in strong:
                lines.append(f"      {s['symbol']} {s['name']} 涨{s['pct']:+.2f}% 连涨{s['consecutive_days']}天 量比{s['vol_ratio']}")

        # 突破股
        breakout = deep.get('breakout_stocks', [])
        if breakout:
            lines.append(f"    🚀 新突破 (涨>5%且刚启动):")
            for s in breakout[:5]:
                rise = s.get('rise_from_low', 0)
                lines.append(f"      {s['symbol']} {s['name']} 涨{s['pct']:+.2f}% 距月低{rise:+.1f}% 量比{s['vol_ratio']}")

        # 涨停
        lu = deep.get('limit_up', {})
        if lu.get('count', 0) > 0:
            boards = lu.get('consecutive_boards', [])
            lines.append(f"    💥 涨停{lu['count']}只" + (f" 连板{len(boards)}只" if boards else ""))

        # 新闻
        news = c.get('news', [])
        if news:
            lines.append(f"    📰 新闻:")
            for n in news[:2]:
                date = n.get('date', '')[:10]
                title = n.get('title', '')[:50]
                lines.append(f"      [{date}] {title}")

    lines.append(f"\n{'='*50}")
    lines.append("报告生成完毕")
    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='概念板块深度分析')
    parser.add_argument('--count', type=int, default=10, help='分析概念数量')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    args = parser.parse_args()

    data = run(target_count=args.count, verbose=not args.json)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_report(data))
