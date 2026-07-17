# -*- coding: utf-8 -*-
"""企业微信「智能机器人」对接 (Bot ID + Secret, WebSocket 长连接模式)

智能机器人走官方 aibot WebSocket 长连接：
  - 用户在企微 @机器人 说 "分析 600519" / "复盘" / "概念" 等，机器人实时回分析
  - 无需公网/回调 URL/加解密，开箱即用

依赖: pip install wecom-aibot-python-sdk   (import: from aibot import ...)
配置 (.env):
  WECOM_BOT_ID=xxx
  WECOM_BOT_SECRET=xxx
  WECOM_AIBOT_ENABLED=1   # 置 1 启动机器人 (run_bot.py)

运行: python notify/wecom_bot.py
"""
import os
import sys
import io
import asyncio
import base64
import hashlib
import subprocess

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
CHATID_PATH = os.path.join(BASE_DIR, "data", "wecom_chatid.txt")

# 主动推送时单条 markdown 上限 20480 字节, 留余量分段
MAX_BYTES = 18000


def load_env(path=ENV_PATH):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            # 去掉行内注释 (# 之后) 及引号/首尾空白
            v = v.split("#", 1)[0].strip().strip('"').strip("'")
            env[k.strip()] = v
    return env


def is_enabled():
    env = load_env()
    return (env.get("WECOM_AIBOT_ENABLED", os.environ.get("WECOM_AIBOT_ENABLED", "0")) or "0") == "1"


def get_bot_creds():
    env = load_env()
    bot_id = env.get("WECOM_BOT_ID") or os.environ.get("WECOM_BOT_ID", "")
    secret = env.get("WECOM_BOT_SECRET") or os.environ.get("WECOM_BOT_SECRET", "")
    return bot_id, secret


# ---------------------------------------------------------------------------
# 命令解析: 把用户自然语言映射为 core/cli.py 的参数
# ---------------------------------------------------------------------------
def parse_command(text):
    """返回 (args_list, label) 或 (None, help_text) 表示需要直接回复帮助。"""
    t = (text or "").strip()
    if not t:
        return None, _HELP

    low = t.lower()

    # 帮助
    if low in ("help", "帮助", "菜单", "?", "？", "命令"):
        return None, _HELP

    # 自选股批量分析 (放在"持仓/自选"之前, 避免被精确匹配抢先)
    if low in ("自选分析", "分析全部", "批量分析", "analyze-all", "自选股分析", "全部分析"):
        return ["analyze-all"], "自选股批量分析"

    # 清空自选股
    if low in ("清空自选", "清空自选股", "clear", "清空"):
        return ["clear"], "清空自选股"

    # 加自选股: "加自选 600519" / "添加 600000" / "加入自选 600519" / "加 600519" / "add 600519"
    # 支持批量: "批量加自选 600519 600000 300750" / "加自选 600519 600000"
    import re as _re
    _ADD_KW = ("加自选", "添加", "加入自选", "自选加", "加自选股", "批量加自选", "批量添加", "批量加入")
    if any(k in low for k in _ADD_KW) or low.startswith("加 ") or low.startswith("add "):
        _codes = _re.findall(r"\d{4,6}", t)
        if _codes:
            return ["add"] + _codes, f"加自选 {len(_codes)} 只"
        return None, "请提供股票代码，例如：加自选 600519 600000"

    # 持仓 / 自选
    if low in ("持仓", "自选", "portfolio", "watchlist", "自选股"):
        return ["portfolio"], "持仓"

    # 复盘
    if low in ("复盘", "review", "每日复盘", "收盘复盘"):
        return ["review"], "每日复盘"

    # 概念板块 (含 "分析热点"/"看题材" 等组合词)
    if any(k in low for k in ("概念", "concept", "热点", "题材")):
        return ["concept", "--stage", "list", "--top", "10"], "概念板块扫描"

    # 期权
    if low in ("期权", "options", "etf期权", "etf"):
        return ["options"], "ETF 期权扫描"

    # 大盘
    if low in ("大盘", "market", "行情"):
        return ["market"], "大盘概览"

    # 分析: "分析 600519" / "600519" / 直接 6 位数字
    # 注意: 必须校验提取出的 code 是合法股票代码, 否则不进入分析分支
    # (否则 "分析热点" 会被 startswith("分析") 命中, 截出 "热点" 当代码而崩溃)
    code = None
    if low.startswith("分析") or low.startswith("analyze"):
        code = t.split(None, 1)[1].strip() if " " in t else t[2:].strip()
    elif low.startswith("查") or low.startswith("看看"):
        code = t.split(None, 1)[1].strip() if " " in t else ""
    else:
        # 纯数字视为股票代码
        maybe = t.replace(" ", "")
        if maybe.isdigit() and 4 <= len(maybe) <= 6:
            code = maybe

    if code:
        code = code.split()[0].strip()
        if _is_valid_code(code):
            # 支持 "600519 --brief" 之类尾随参数
            extra = []
            if "--brief" in low:
                extra.append("--brief")
            if "--json" in low:
                extra.append("--json")
            # 机器人场景跳过 Playwright 浏览器采集，改走直连 (快且 Windows 不崩)
            return ["analyze", code, "--no-browser"] + extra, f"分析 {code}"

    return None, f"未能识别指令：「{t}」\n\n{_HELP}"


def _is_valid_code(code: str) -> bool:
    """校验是否为合法股票代码: 6 位数字, 或 sh/sz 前缀 + 6 位数字"""
    if not code:
        return False
    c = code.strip().lower()
    if c[:2] in ("sh", "sz"):
        c = c[2:]
    return c.isdigit() and len(c) == 6


_HELP = """📈 **Stock Analysis Pro 使用指南**
直接 @我 或发下列指令：

• `分析 600519` 或 `600519` —— 个股 6 维分析
• `加自选 600519` / `添加 600000` —— 加入自选股
• `自选分析` / `分析全部` —— 自选股批量分析(汇总报告)
• `清空自选` —— 清空自选股列表
• `概念` / `热点` —— 概念板块资金榜
• `复盘` / `review` —— 每日复盘
• `期权` / `options` —— ETF 期权机会扫描
• `大盘` / `market` —— 大盘概览
• `持仓` / `自选` —— 持仓/自选股
• `帮助` —— 显示本菜单

示例：分析 600519"""


# ---------------------------------------------------------------------------
# 调用 core/cli.py 并捕获输出
# ---------------------------------------------------------------------------
async def run_cli(args):
    """运行 core/cli.py, 返回 (stdout_text, error_text)。带超时兜底，避免子进程卡死。"""
    cmd = [sys.executable, "core/cli.py"] + args
    # 概念/复盘/批量类可能较重, 给更长超时; 个股分析较快
    timeout = 600 if args and args[0] == "analyze-all" else (300 if args and args[0] in ("concept", "review", "options") else 150)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=BASE_DIR,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "⏱️ 分析超时（已限制 %d 秒）。请稍后再试，或先发「概念 / 大盘」等轻量指令。" % timeout, ""
    except Exception as e:
        return "", f"执行失败: {e}"
    text = out.decode("utf-8", errors="replace").strip()
    errtext = err.decode("utf-8", errors="replace").strip()
    return text, errtext


def _split_by_bytes(text, max_bytes=MAX_BYTES):
    chunks, cur, cur_b = [], [], 0
    for line in text.split("\n"):
        lb = len(line.encode("utf-8"))
        if cur and cur_b + lb + 1 > max_bytes:
            chunks.append("\n".join(cur))
            cur, cur_b = [], 0
        cur.append(line)
        cur_b += lb + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [""]


# ---------------------------------------------------------------------------
# chat_id 记录 (供主动推送复用)
# ---------------------------------------------------------------------------
def save_chat_id(chat_id):
    if not chat_id:
        return
    try:
        os.makedirs(os.path.dirname(CHATID_PATH), exist_ok=True)
        with open(CHATID_PATH, "w", encoding="utf-8") as f:
            f.write(chat_id)
    except Exception:
        pass


def load_chat_id():
    if os.path.exists(CHATID_PATH):
        with open(CHATID_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def extract_target(frame):
    """从回调帧提取会话标识 (群 chatid 优先, 否则单聊用户 userid)。

    真实回调结构 (实测):
      body.chattype = 'single' | 'group'
      body.chatid            # 群聊会话 id
      body.from.userid       # 发送者 userid (单聊主动推送目标)
    """
    body = frame.get("body", {}) if isinstance(frame, dict) else {}
    chat_id = body.get("chatid") or body.get("chat_id") or body.get("chatId")
    if chat_id:
        return chat_id
    frm = body.get("from", {})
    if isinstance(frm, dict) and frm.get("userid"):
        return frm.get("userid")
    # 兜底: 兼容可能的扁平字段
    return body.get("from_userid") or body.get("fromUserid") or ""


# ---------------------------------------------------------------------------
# 机器人主体
# ---------------------------------------------------------------------------
_GEN_REQ_ID = None  # 由 build_client 初始化


def build_client():
    global _GEN_REQ_ID
    from aibot import WSClient, WSClientOptions, generate_req_id

    bot_id, secret = get_bot_creds()
    if not bot_id or not secret:
        raise RuntimeError("未配置 WECOM_BOT_ID / WECOM_BOT_SECRET (.env)")

    _GEN_REQ_ID = generate_req_id
    client = WSClient(WSClientOptions(bot_id=bot_id, secret=secret))

    @client.on("authenticated")
    def on_auth():
        print("[WECOM-BOT] 认证成功, 机器人已上线", flush=True)

    @client.on("message.text")
    async def on_text(frame):
        await handle_message(client, frame)

    @client.on("event.enter_chat")
    async def on_enter(frame):
        await client.reply_welcome(frame, {
            "msgtype": "markdown",
            "markdown": {"content": _HELP},
        })

    @client.on("error")
    def on_err(e):
        print(f"[WECOM-BOT] 错误: {e}", file=sys.stderr, flush=True)

    return client


async def handle_message(client, frame):
    content = frame.get("body", {}).get("text", {}).get("content", "")
    target = extract_target(frame)
    if target:
        save_chat_id(target)

    args, label = parse_command(content)
    stream_id = _GEN_REQ_ID("stream")

    if args is None:
        # 直接回复 (帮助/无法识别)
        await client.reply_stream(frame, stream_id, label, True)
        return

    # 支持 HTML 报告的指令: 自动追加 --html, 生成报告文件后一并发送
    _HTML_CMDS = {"market", "review", "concept", "options", "analyze", "analyze-all"}
    # 机器人场景跳过 Playwright 浏览器采集 (快且 Windows 不崩); 概念走 list 阶段本就无需浏览器, 不加
    _NO_BROWSER_CMDS = {"market", "review", "options", "analyze", "analyze-all"}
    run_args = list(args)
    if args and args[0] in _HTML_CMDS:
        run_args.append("--html")
    if args and args[0] in _NO_BROWSER_CMDS:
        run_args.append("--no-browser")

    # 先发"分析中"状态 (无论 append/replace 语义都自然)
    try:
        await client.reply_stream(frame, stream_id, f"🔍 正在{label}，请稍候…", False)
    except Exception:
        pass

    text, err = await run_cli(run_args)
    if err and not text:
        text = f"⚠️ 分析失败：\n{err[:1500]}"

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

    chunks = _split_by_bytes(summary_text or "(无输出)")
    if len(chunks) == 1:
        await client.reply_stream(frame, stream_id, chunks[0], True)
    else:
        # 超长: 改用主动推送分条发送 (避免流式分段歧义)
        for i, ch in enumerate(chunks):
            await client.send_message(target, {
                "msgtype": "markdown",
                "markdown": {"content": f"（{i+1}/{len(chunks)}）\n{ch}"},
            })

    # 附上 HTML 报告文件 (复用当前连接)
    if html_path and os.path.exists(html_path):
        ok = await send_file_on_client(client, html_path, target)
        if not ok:
            # 文件发送失败不该静默: 在对话里给出可见提示, 便于排查
            try:
                await client.reply_stream(
                    frame, _GEN_REQ_ID("stream"),
                    "⚠️ HTML 报告文件发送失败（详见服务端 stderr 日志），上方摘要已正常发送。",
                    True,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 经 aibot 发送本地文件 (file 消息, 需先走 WebSocket 三步上传拿 media_id)
# ---------------------------------------------------------------------------
_CHUNK_SIZE = 512 * 1024  # base64 前单分片上限 (企微限制 512KB)


async def _upload_media(client, filepath: str, msg_type: str = "file") -> str:
    """三步分片上传 (init -> chunk*N -> finish) 拿 media_id。

    企业微信 aibot 的 image/file/voice/video 消息均需 media_id, 不能 base64
    直发; 当前官方 SDK(1.0.0)未封装上传, 这里用底层 send_reply 直接走
    WebSocket 上传协议 (aibot_upload_media_init/chunk/finish)。
    """
    from aibot import generate_req_id

    with open(filepath, "rb") as f:
        data = f.read()
    total_size = len(data)
    if total_size < 5:
        raise RuntimeError("文件过小, 无法上传 (需 >=5 字节)")
    md5 = hashlib.md5(data).hexdigest()
    chunks = [data[i:i + _CHUNK_SIZE] for i in range(0, total_size, _CHUNK_SIZE)]
    total_chunks = len(chunks)

    # 1) init -> upload_id
    init_ack = await client._ws_manager.send_reply(
        generate_req_id("aibot_upload_media_init"),
        {
            "type": msg_type,
            "filename": os.path.basename(filepath),
            "total_size": total_size,
            "total_chunks": total_chunks,
            "md5": md5,
        },
        "aibot_upload_media_init",
    )
    upload_id = (init_ack or {}).get("body", {}).get("upload_id")
    if not upload_id:
        raise RuntimeError(f"上传初始化失败, 回执: {init_ack}")

    # 2) chunk (串行, 每片等 ack)
    for idx, chunk in enumerate(chunks):
        await client._ws_manager.send_reply(
            generate_req_id("aibot_upload_media_chunk"),
            {
                "upload_id": upload_id,
                "chunk_index": idx,
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            },
            "aibot_upload_media_chunk",
        )

    # 3) finish -> media_id
    finish_ack = await client._ws_manager.send_reply(
        generate_req_id("aibot_upload_media_finish"),
        {"upload_id": upload_id},
        "aibot_upload_media_finish",
    )
    media_id = (finish_ack or {}).get("body", {}).get("media_id")
    if not media_id:
        raise RuntimeError(f"上传完成失败, 回执: {finish_ack}")
    return media_id


def send_file_via_bot(filepath: str, chatid: str = None) -> bool:
    """经 aibot 通道把本地文件作为 file 消息推送到指定会话 (默认 YanLang)。

    返回 True/False。文件消息在微信里显示为可点击下载/打开的文件。
    """
    if not os.path.exists(filepath):
        print(f"[AIBOT] 文件不存在, 跳过: {filepath}", file=sys.stderr)
        return False
    chat = chatid or load_chat_id() or "YanLang"
    client = build_client()

    async def go():
        await client.connect()
        await asyncio.sleep(3)  # 等待认证完成
        media_id = await _upload_media(client, filepath, "file")
        await client.send_message(chat, {"msgtype": "file", "file": {"media_id": media_id}})
        client.disconnect()
        print(f"[AIBOT] 文件已发送 chat={chat} media_id={media_id}")

    try:
        asyncio.run(go())
        return True
    except Exception as e:
        print(f"[AIBOT] 发送失败: {e}", file=sys.stderr)
        return False


async def send_file_on_client(client, filepath: str, chatid: str) -> bool:
    """复用已连接的 client 把本地文件作为 file 消息发送 (无需重连)。

    与 send_file_via_bot 区别: 不新建连接, 直接在当前会话 client 上上传并发送,
    适合 handle_message / wecom_push 在已连状态下附送 HTML 报告。
    """
    if not os.path.exists(filepath):
        print(f"[AIBOT] 文件不存在, 跳过: {filepath}", file=sys.stderr)
        return False
    try:
        media_id = await _upload_media(client, filepath, "file")
        await client.send_message(chatid, {"msgtype": "file", "file": {"media_id": media_id}})
        print(f"[AIBOT] 文件已发送 chat={chatid} media_id={media_id}")
        return True
    except Exception as e:
        print(f"[AIBOT] 发送失败: {e}", file=sys.stderr)
        return False


def main():
    if not is_enabled():
        print("[WECOM-BOT] WECOM_AIBOT_ENABLED 未置 1, 跳过启动。", flush=True)
        return
    client = build_client()
    print("[WECOM-BOT] 正在连接企业微信智能机器人…", flush=True)
    client.run()


if __name__ == "__main__":
    main()
