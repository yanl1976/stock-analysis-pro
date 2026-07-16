# -*- coding: utf-8 -*-
"""自动获取东方财富匿名会话 Cookie 并写入 config/config.yaml

东财 push2 概念板块接口无需登录账号，只需浏览器访问时自动生成的
匿名会话 Cookie（qgqp_b_id 等）。本脚本用 Playwright 无头浏览器
访问东财页面，抓取其自动 set-cookie 的值并保存到配置文件。

用法:
    python scripts/get_em_cookie.py
"""

import os
import sys
import asyncio

# 让脚本能 import 到项目根下的 collectors / config
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from collectors.em_concept import set_cookie, get_cookie  # noqa: E402

# 依次访问这些页面，累积东财下发的匿名 Cookie
_WARMUP_URLS = [
    'https://quote.eastmoney.com/bk/',
    'https://data.eastmoney.com/bkzj/gn.html',
]

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36')


async def _grab_cookie() -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()

        for url in _WARMUP_URLS:
            try:
                print(f'  访问 {url} ...')
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                # 等待页面 JS 触发接口，东财会补发更多 cookie
                await page.wait_for_timeout(3000)
            except Exception as e:
                print(f'  (忽略) 访问异常: {e}')

        cookies = await context.cookies()
        await browser.close()

    # 仅保留 eastmoney 域的 cookie，拼成 "k=v; k=v" 形式
    parts = []
    for c in cookies:
        domain = c.get('domain', '')
        if 'eastmoney' in domain:
            parts.append(f"{c['name']}={c['value']}")
    return '; '.join(parts)


def main():
    print('[get_em_cookie] 正在用无头浏览器获取东方财富匿名 Cookie...')
    cookie = asyncio.run(_grab_cookie())

    if not cookie:
        print('[get_em_cookie] ❌ 未抓到任何 eastmoney Cookie，请检查网络/Playwright 安装')
        sys.exit(1)

    set_cookie(cookie)
    saved = get_cookie()
    preview = saved[:80] + ('...' if len(saved) > 80 else '')
    print(f'[get_em_cookie] ✅ 已获取并保存 Cookie ({len(saved)} 字符)')
    print(f'  预览: {preview}')
    print('  现在可运行: python core/cli.py concept')


if __name__ == '__main__':
    main()
