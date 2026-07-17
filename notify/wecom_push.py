# -*- coding: utf-8 -*-
"""企业微信智能机器人 · 主动推送 (cron 友好)

连接 -> 跑分析 -> 推送到最近一次交互的会话 -> 断开。
用法:
    python notify/wecom_push.py review
    python notify/wecom_push.py concept --stage list --top 10
    python notify/wecom_push.py analyze 600519
    python notify/wecom_push.py "复盘" --target <chat_id>   # 指定会话
"""
import os
import sys
import asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from notify.wecom_bot import (
    build_client, run_cli, _split_by_bytes, load_chat_id, is_enabled,
    send_file_on_client,
)


async def push(args, target=None):
    chat_id = target or load_chat_id()
    if not chat_id:
        print("[WECOM-PUSH] 无 chat_id (先让用户 @机器人 交互一次以记录会话), 退出", file=sys.stderr)
        return 1

    # 支持 HTML 报告的指令: 自动追加 --html, 生成报告文件后一并发送
    _HTML_CMDS = {"market", "review", "concept", "options", "analyze", "analyze-all"}
    run_args = args + (["--html"] if args and args[0] in _HTML_CMDS else [])

    client = build_client()
    await client.connect()
    await asyncio.sleep(2)  # 等待自动认证完成

    text, err = await run_cli(run_args)
    if err and not text:
        text = f"⚠️ 推送内容生成失败：\n{err[:1500]}"

    # 解析 HTML 报告路径 (cli.py --html 末尾输出 HTML_REPORT:<path>)
    html_path = None
    summary_text = text or ""
    if "HTML_REPORT:" in summary_text:
        _lines = summary_text.split("\n")
        _path_lines = [l for l in _lines if l.startswith("HTML_REPORT:")]
        if _path_lines:
            html_path = _path_lines[-1].split("HTML_REPORT:", 1)[1].strip()
            summary_text = "\n".join(
                l for l in _lines if not l.startswith("HTML_REPORT:")
            ).strip()

    for i, ch in enumerate(_split_by_bytes(summary_text or "(无输出)")):
        body = ch if len(_split_by_bytes(summary_text)) == 1 else f"（{i+1}/{len(_split_by_bytes(summary_text))}）\n{ch}"
        await client.send_message(chat_id, {
            "msgtype": "markdown",
            "markdown": {"content": body},
        })

    # 附上 HTML 报告文件 (复用当前连接)
    if html_path and os.path.exists(html_path):
        await send_file_on_client(client, html_path, chat_id)

    client.disconnect()
    print(f"[WECOM-PUSH] 已推送 {len(_split_by_bytes(summary_text))} 条到 {chat_id}", flush=True)
    return 0


def main():
    if not is_enabled():
        print("[WECOM-PUSH] WECOM_AIBOT_ENABLED 未置 1, 退出。", flush=True)
        sys.exit(1)
    # 第一个非选项参数作为指令; 其余透传给 core/cli.py
    raw = sys.argv[1:]
    if not raw:
        print("用法: python notify/wecom_push.py <指令> [额外参数] [--target chat_id]")
        sys.exit(1)
    target = None
    if "--target" in raw:
        idx = raw.index("--target")
        target = raw[idx + 1] if idx + 1 < len(raw) else None
        raw = raw[:idx] + raw[idx + 2:]
    # 把首词映射为 cli 参数
    cmd = raw[0]
    rest = raw[1:]
    mapping = {
        "复盘": ["review"], "review": ["review"],
        "概念": ["concept", "--stage", "list", "--top", "10"],
        "concept": ["concept", "--stage", "list", "--top", "10"],
        "期权": ["options"], "options": ["options"],
        "大盘": ["market"], "market": ["market"],
        "分析": ["analyze"], "analyze": ["analyze"],
        "自选分析": ["analyze-all"], "分析全部": ["analyze-all"],
        "批量分析": ["analyze-all"], "analyze-all": ["analyze-all"],
    }
    if cmd in mapping:
        cli_args = mapping[cmd] + rest
    else:
        cli_args = raw  # 直接透传 (如 analyze 600519)
    sys.exit(asyncio.run(push(cli_args, target)))


if __name__ == "__main__":
    main()
