# -*- coding: utf-8 -*-
"""市场宽度采集 — 涨跌家数 + 涨跌停统计

数据源:
  1. Playwright 拦截 ulist.np API (上证+深证合并)
  2. akshare 涨跌停池 (备选)

用法:
    from collectors.breadth import fetch_breadth, fetch_limit_stats
    breadth = fetch_breadth()  # {up, down, flat, limit_up, limit_down}
    limits = fetch_limit_stats(date='20260716')  # {zt_count, dt_count, zt_stocks: [...]}
"""

import asyncio
import json
from datetime import datetime
from typing import Optional


def fetch_breadth() -> dict:
    """获取涨跌家数 (Playwright 拦截 ulist.np API)"""
    try:
        return asyncio.run(_fetch_breadth_async())
    except Exception as e:
        print(f"[breadth] Playwright异常: {e}")
        return {'up': 0, 'down': 0, 'flat': 0, 'limit_up': 0, 'limit_down': 0}


async def _fetch_breadth_async() -> dict:
    """拦截东财 center 页面的 ulist.np API 响应"""
    from playwright.async_api import async_playwright

    breadth_api_data = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def on_response(response):
            url = response.url
            if response.status != 200:
                return
            if 'ulist.np/get' in url or ('push2' in url and 'f104' in url):
                try:
                    body = await response.json()
                    breadth_api_data['raw'] = body
                except:
                    pass

        page.on('response', on_response)

        try:
            await page.goto(
                'https://quote.eastmoney.com/center.html',
                wait_until='domcontentloaded',
                timeout=20000,
            )
            # 等待 API 响应到达
            try:
                await page.wait_for_event(
                    'response',
                    lambda r: 'ulist.np' in r.url or ('push2' in r.url and 'f104' in r.url),
                    timeout=10000,
                )
            except:
                pass
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[breadth] 页面加载异常: {e}")
        finally:
            await browser.close()

    result = {'up': 0, 'down': 0, 'flat': 0, 'limit_up': 0, 'limit_down': 0}

    if 'raw' in breadth_api_data:
        items = breadth_api_data['raw'].get('data', {}).get('diff', [])
        for item in items:
            secid = item.get('f1')
            if secid in (0, 1):  # 上证 or 深证
                result['up'] += item.get('f104', 0) or 0
                result['down'] += item.get('f105', 0) or 0
                result['flat'] += item.get('f106', 0) or 0
                result['limit_up'] += item.get('f107', 0) or 0
                result['limit_down'] += item.get('f108', 0) or 0

    return result


def fetch_limit_stats(date: Optional[str] = None) -> dict:
    """涨跌停统计 (akshare)

    Args:
        date: YYYYMMDD, 默认今天

    Returns:
        {
            'zt_count': int,     # 涨停家数
            'dt_count': int,     # 跌停家数
            'zt_stocks': [...],  # 涨停股票列表
            'dt_stocks': [...],  # 跌停股票列表
        }
    """
    import akshare as ak

    if not date:
        date = datetime.now().strftime("%Y%m%d")

    result = {
        'zt_count': 0,
        'dt_count': 0,
        'zt_stocks': [],
        'dt_stocks': [],
        'date': date,
    }

    # 涨停池
    try:
        zt_df = ak.stock_zt_pool_em(date=date)
        if zt_df is not None and not zt_df.empty:
            result['zt_count'] = len(zt_df)
            for _, row in zt_df.head(30).iterrows():
                result['zt_stocks'].append({
                    'code': str(row.get('代码', '')),
                    'name': str(row.get('名称', '')),
                    'change_pct': float(row.get('涨跌幅', 0)),
                    'amount': float(row.get('成交额', 0)),
                    'first_time': str(row.get('首次封板时间', '')),
                    'last_time': str(row.get('最后封板时间', '')),
                    'reason': str(row.get('所属行业', '')),
                    'lianban': int(row.get('连板数', 1)),
                })
    except Exception as e:
        print(f"[breadth] 涨停池获取异常: {e}")

    # 跌停池
    try:
        dt_df = ak.stock_zt_pool_dtgc_em(date=date)
        if dt_df is not None and not dt_df.empty:
            result['dt_count'] = len(dt_df)
            for _, row in dt_df.head(20).iterrows():
                result['dt_stocks'].append({
                    'code': str(row.get('代码', '')),
                    'name': str(row.get('名称', '')),
                    'change_pct': float(row.get('涨跌幅', 0)),
                    'amount': float(row.get('成交额', 0)),
                })
    except Exception as e:
        print(f"[breadth] 跌停池获取异常: {e}")

    return result


if __name__ == '__main__':
    print("=== 涨跌家数 ===")
    b = fetch_breadth()
    print(json.dumps(b, ensure_ascii=False))

    print("\n=== 涨跌停统计 ===")
    ls = fetch_limit_stats()
    print(f"涨停: {ls['zt_count']}, 跌停: {ls['dt_count']}")
    for s in ls['zt_stocks'][:5]:
        print(f"  {s['name']}({s['code']}) {s['change_pct']:+.1f}% 连板{s['lianban']}")
