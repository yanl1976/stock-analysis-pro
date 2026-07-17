# -*- coding: utf-8 -*-
"""企业微信推送 (群机器人 Webhook 模式 A)

配置 (.env, 二选一):
  - WECOM_WEBHOOK_KEY=xxxx            # 仅填 Webhook URL 中 key= 后的部分
  - WECOM_WEBHOOK_URL=https://...     # 或直接填完整 Webhook URL (优先)
用法:
    from notify.wecom import push_text
    push_text("报告内容...")
"""
import os
import sys
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

# 企微 text 消息 content 上限 2048 字节, 留余量分段
MAX_BYTES = 1900


def load_env(path=ENV_PATH):
    """简单解析 .env (KEY=VALUE, 忽略注释/空行/引号)。不覆盖系统环境变量。"""
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
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_webhook_url():
    """返回完整 Webhook URL。支持填完整 URL 或仅 key (含容错)。"""
    env = load_env()
    # 1) 完整 Webhook URL (优先)
    url = env.get("WECOM_WEBHOOK_URL") or os.environ.get("WECOM_WEBHOOK_URL", "")
    if url:
        return url
    # 2) 仅 key (容错: 若用户把完整 URL 填进了 KEY 字段, 也直接可用)
    key = env.get("WECOM_WEBHOOK_KEY") or os.environ.get("WECOM_WEBHOOK_KEY", "")
    if not key:
        return ""
    if "key=" in key or "://" in key:
        return key
    return f"{WEBHOOK_URL}?key={key}"


def _split_by_bytes(text, max_bytes=MAX_BYTES):
    """按行切分, 保证每段 <= max_bytes (utf-8 字节), 尽量不截断行。"""
    chunks = []
    cur = []
    cur_b = 0
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


def _post(url, payload):
    resp = requests.post(url, json=payload, timeout=15)
    data = resp.json()
    if data.get("errcode") != 0:
        msg = data.get("errmsg", "")
        if data.get("errcode") == 93000:
            msg += " (请检查 WECOM_WEBHOOK_KEY: 应从群机器人 Webhook 地址中 key= 后复制, " \
                  "且群机器人未被删除/禁用; 本库解析出的 key 长度异常时多为复制多了内容)"
        raise RuntimeError(f"errcode={data.get('errcode')} {msg}")
    return data


def push_text(text, url=None):
    """把文本推送到企微群 (自动按 1900 字节分段)。无配置时仅告警不抛错。"""
    url = url or get_webhook_url()
    if not url:
        print("[WECOM] 未配置 WECOM_WEBHOOK_KEY/URL, 跳过推送", file=sys.stderr)
        return
    text = (text or "").strip()
    if not text:
        return
    for chunk in _split_by_bytes(text):
        _post(url, {"msgtype": "text", "text": {"content": chunk}})


def push_markdown(md, url=None):
    """推送 markdown (上限 4096 字节, 支持有限标签: 标题/加粗/链接/引用/颜色)。"""
    url = url or get_webhook_url()
    if not url:
        print("[WECOM] 未配置 WECOM_WEBHOOK_KEY/URL, 跳过推送", file=sys.stderr)
        return
    md = (md or "").strip()
    if not md:
        return
    for chunk in _split_by_bytes(md, 3800):
        _post(url, {"msgtype": "markdown", "markdown": {"content": chunk}})


def push_file(filepath, url=None):
    """上传本地文件并以「文件消息」推送到企微群 (适合发送 HTML 报告等)。

    群机器人 Webhook 文件消息上限 20MB，先 upload_media 拿 media_id 再 send。
    无配置时仅告警不抛错。
    """
    url = url or get_webhook_url()
    if not url:
        print("[WECOM] 未配置 WECOM_WEBHOOK_KEY/URL, 跳过推送", file=sys.stderr)
        return
    if not os.path.exists(filepath):
        print(f"[WECOM] 文件不存在, 跳过推送: {filepath}", file=sys.stderr)
        return

    # 1) 上传文件获取 media_id
    key = ""
    if "key=" in url:
        key = url.split("key=", 1)[1].split("&", 1)[0]
    upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={key}&type=file"
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                upload_url,
                files={"media": (os.path.basename(filepath), f)},
                timeout=60,
            )
        up = resp.json()
        if up.get("errcode") != 0:
            raise RuntimeError(f"upload errcode={up.get('errcode')} {up.get('errmsg')}")
        media_id = up["media_id"]
    except Exception as e:
        print(f"[WECOM] 文件上传失败: {e}", file=sys.stderr)
        return

    # 2) 发送文件消息
    _post(url, {"msgtype": "file", "file": {"media_id": media_id}})
