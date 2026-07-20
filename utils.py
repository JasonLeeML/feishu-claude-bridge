"""工具函数模块"""
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from config import config

# 任务/卡片通用状态文案
STATUS_LABEL_MAP = {
    "queued": "排队中",
    "running": "执行中",
    "success": "已完成",
    "error": "失败",
    "canceled": "已取消",
}

_allowed_roots_cache: Optional[List[str]] = None
_allowed_roots_cache_ts: float = 0.0
_ALLOWED_ROOTS_CACHE_TTL: float = 30.0


def get_allowed_roots() -> List[str]:
    """读取允许访问的根目录（带短期缓存，避免每次路径校验都重新解析）。"""
    global _allowed_roots_cache, _allowed_roots_cache_ts
    now = time.time()
    if _allowed_roots_cache is not None and (now - _allowed_roots_cache_ts) < _ALLOWED_ROOTS_CACHE_TTL:
        return _allowed_roots_cache
    roots = config.get("paths", {}).get("allowed_roots", [str(Path.home())])
    _allowed_roots_cache = [str(Path(p).expanduser().resolve()) for p in roots]
    _allowed_roots_cache_ts = now
    return _allowed_roots_cache


def bust_allowed_roots_cache() -> None:
    """使 allowed_roots 缓存失效（配置热更新场景）。"""
    global _allowed_roots_cache, _allowed_roots_cache_ts
    _allowed_roots_cache = None
    _allowed_roots_cache_ts = 0.0


def is_path_allowed(path: str) -> bool:
    """检查路径是否在 allowed_roots 内"""
    target = str(Path(path).expanduser().resolve())
    allowed_roots = get_allowed_roots()
    for root in allowed_roots:
        if target == root or target.startswith(root + os.sep):
            return True
    return False


def extract_text(message) -> str:
    """从飞书消息中提取文本内容"""
    try:
        content = json.loads(message.content)
    except Exception:
        return ""
    text = content.get("text", "")
    if isinstance(text, str):
        return text.strip()
    return ""


def get_sender_id(event) -> Optional[str]:
    """尽量提取发送者 ID，方便做白名单"""
    try:
        sender = event.sender
        sender_id = sender.sender_id
        return (
            getattr(sender_id, "open_id", None)
            or getattr(sender_id, "user_id", None)
            or getattr(sender_id, "union_id", None)
        )
    except Exception:
        return None


def is_message_from_bot(event) -> bool:
    """判断消息是否来自机器人，避免机器人回复自己造成循环"""
    try:
        sender = event.sender
        sender_type = getattr(sender, "sender_type", "")
        return sender_type == "app"
    except Exception:
        return False


def is_allowed_sender(event) -> bool:
    """用户白名单。config.yaml 里 allowed_user_ids 为空时，默认允许所有人。"""
    allowed_user_ids = config.get("security", {}).get("allowed_user_ids", [])
    if not allowed_user_ids:
        return True
    sender_id = get_sender_id(event)
    return sender_id in allowed_user_ids
