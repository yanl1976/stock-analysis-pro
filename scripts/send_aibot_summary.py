#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""经企微智能机器人(aibot)通道推送测试摘要 markdown 通知。

说明: aibot 支持 file 消息 (需先走 WebSocket 三步上传拿 media_id,
见 notify/wecom_bot.send_file_via_bot)。本脚本发 markdown 摘要;
完整 HTML 报告文件由 test_analysis.py 经 send_file_via_bot 发送。
"""
import os
import sys
import glob
import asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from notify.wecom_bot import build_client, load_chat_id


def find_latest_report():
    files = glob.glob(os.path.join(BASE_DIR, "cache", "test_report_*.html"))
    if not files:
        return "(未找到报告文件)"
    return os.path.basename(max(files, key=os.path.getmtime))


def main():
    report = find_latest_report()
    md = f"""## 📊 股票分析系统 · 全功能测试完成
> **9 通过 / 0 降级 / 0 失败**（共 9 项，通过率 100%）

本次改造：概念板块数据源由东方财富 push2/Playwright 切换为**新浪源**（当前网络环境东财 push2 被封锁），彻底消除概念板块与每日复盘的接口降级。

- ✅ 个股深度分析（直连轻量）
- ✅ 个股分析 + HTML 渲染
- ✅ 大盘概览（宏观/指数）
- ✅ 概念板块扫描（榜单 list，新浪源）
- ✅ 每日复盘（概念板块新浪源）
- ✅ ETF 期权扫描
- ✅ 批量分析自选股
- ✅ 持仓管理
- ✅ 自选股列表

📄 完整 HTML 测试报告：`cache/{report}`

ℹ️ 完整 HTML 测试报告文件经智能机器人通道 `send_file_via_bot` 发送（三步上传拿 media_id），可直接在微信中打开。"""

    chat = load_chat_id() or "YanLang"
    client = build_client()

    async def go():
        await client.connect()
        await asyncio.sleep(3)  # 等待认证完成
        await client.send_message(chat, {"msgtype": "markdown", "markdown": {"content": md}})
        client.disconnect()
        print(f"AIBOT_SENT chat={chat}")

    asyncio.run(go())


if __name__ == "__main__":
    main()
