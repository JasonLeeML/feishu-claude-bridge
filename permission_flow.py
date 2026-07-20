"""权限交互流程：在飞书卡片中三选一后继续执行。"""
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from client import send_permission_card

APPROVAL_TIMEOUT_SECONDS = 120


@dataclass
class PermissionRequest:
    request_id: str
    chat_id: str
    requester_id: Optional[str]
    created_at: float
    event: threading.Event
    choice: Optional[str] = None


_pending_requests: Dict[str, PermissionRequest] = {}
_session_permission_mode: Dict[str, str] = {}
_lock = threading.Lock()


def get_session_permission_mode(chat_id: str) -> Optional[str]:
    with _lock:
        return _session_permission_mode.get(chat_id)


def set_session_permission_mode(chat_id: str, mode: str) -> None:
    with _lock:
        if mode:
            _session_permission_mode[chat_id] = mode


def clear_session_permission_mode(chat_id: str) -> None:
    with _lock:
        _session_permission_mode.pop(chat_id, None)


def ask_permission(chat_id: str, requester_id: Optional[str], prompt_preview: str) -> str:
    """发卡片并等待用户选择，返回 allow_once/allow_session/deny/timeout/send_failed。"""
    req = PermissionRequest(
        request_id=str(uuid.uuid4()),
        chat_id=chat_id,
        requester_id=requester_id,
        created_at=time.time(),
        event=threading.Event(),
    )
    with _lock:
        _cleanup_expired_locked()
        _pending_requests[req.request_id] = req

    sent = send_permission_card(chat_id, req.request_id, prompt_preview)
    if not sent:
        with _lock:
            _pending_requests.pop(req.request_id, None)
        return "send_failed"

    done = req.event.wait(APPROVAL_TIMEOUT_SECONDS)
    with _lock:
        _pending_requests.pop(req.request_id, None)
    if not done:
        return "timeout"
    return req.choice or "deny"


def resolve_permission(
    request_id: str,
    action: str,
    operator_id: Optional[str],
) -> Tuple[bool, str]:
    """处理卡片点击。返回 (是否接收, 文案)。"""
    with _lock:
        req = _pending_requests.get(request_id)
        if not req:
            return False, "该权限请求已过期或已处理。"

        if not req.requester_id or not operator_id or req.requester_id != operator_id:
            return False, "仅发起请求的用户可执行该操作。"

        choice = action if action in {"allow_once", "allow_session", "deny"} else "deny"
        req.choice = choice
        if choice == "allow_session":
            _session_permission_mode[req.chat_id] = "bypassPermissions"
        req.event.set()
        return True, _choice_text(choice)


def _choice_text(choice: str) -> str:
    if choice == "allow_once":
        return "已允许本次执行。"
    if choice == "allow_session":
        return "已开启完全授权。"
    return "已拒绝本次执行。"


def _cleanup_expired_locked() -> None:
    now = time.time()
    expired_ids = [
        rid for rid, req in _pending_requests.items() if now - req.created_at > APPROVAL_TIMEOUT_SECONDS
    ]
    for rid in expired_ids:
        req = _pending_requests[rid]
        req.choice = "timeout"
        req.event.set()
        del _pending_requests[rid]
