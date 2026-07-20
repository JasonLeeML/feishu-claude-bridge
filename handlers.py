"""消息处理模块"""
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Dict

import config
from file_picker import open_file_picker, resolve_selected_dir, resolve_selected_path
from session import get_or_create_session, get_session
from utils import extract_text, get_sender_id, is_allowed_sender, is_message_from_bot, is_path_allowed, STATUS_LABEL_MAP
from client import (
    send_deferred_permission_card,
    send_delete_auth_card,
    send_git_panel_card,
    send_progress_update,
    send_quick_actions_card,
    send_rich_text,
    send_task_status_card,
    send_text,
    send_thinking_indicator,
)
from commands import build_git_reply, handle_bridge_command, set_current_project_dir
from claude_runner import run_claude_with_requester
from permission_flow import clear_session_permission_mode, get_session_permission_mode, resolve_permission, set_session_permission_mode
from config import (
    CLAUDE_MAX_CONCURRENT_RUNS,
    MOBILE_AUTO_STATUS_CARDS,
    MOBILE_QUICK_ACTIONS_AFTER_REPLY,
    MOBILE_SHORT_REPLY_DEFAULT,
    MOBILE_SHORT_REPLY_LINES,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
from task_manager import (
    create_task,
    get_latest_task,
    get_task,
    list_tasks_by_chat,
    mark_canceled,
    mark_error,
    mark_running,
    mark_success,
    request_cancel,
)

logger = logging.getLogger(__name__)

_chat_locks: Dict[str, threading.Lock] = {}
_chat_locks_last_access: Dict[str, float] = {}
_chat_locks_guard = threading.Lock()
_CHAT_LOCK_TTL_SECONDS = 3600
_global_run_semaphore = threading.Semaphore(max(1, int(CLAUDE_MAX_CONCURRENT_RUNS)))
_processed_message_ids: Dict[str, float] = {}
_processed_guard = threading.Lock()
_PROCESSED_TTL_SECONDS = 300
_card_owner_by_chat: Dict[str, tuple] = {}
_card_owner_guard = threading.Lock()
_CARD_OWNER_TTL_SECONDS = 1800
_task_action_cooldown: Dict[str, float] = {}
_TASK_ACTION_COOLDOWN_SECONDS = 3
_TASK_ACTION_COOLDOWN_TTL = 300
_preloaded_context_by_chat: Dict[str, tuple] = {}
_PRELOADED_CONTEXT_TTL_SECONDS = 3600
_deferred_permissions: Dict[str, dict] = {}
_DEFERRED_PERMISSION_TTL_SECONDS = 600

KNOWN_CARD_KINDS = frozenset({
    "quick_action", "file_picker", "task_action", "git_action",
    "path_draft", "deferred_permission", "delete_auth",
})


def get_current_dir(chat_id: str = None) -> str:
    """获取当前工作目录（会话优先）"""
    if chat_id:
        session = get_session(chat_id)
        if session:
            return session["current_dir"]
    return config.CURRENT_PROJECT_DIR


def transform_prompt(text: str, chat_id: str = None) -> str:
    """
    支持简化版 @文件 语法。

    示例：
    @README.md 总结一下这个文件
    @agent/core.py 解释这个文件的执行流程
    """
    text = text.strip()

    if not text.startswith("@"):
        return text

    parts = text.split(maxsplit=1)
    file_part = parts[0][1:].strip()
    question = parts[1].strip() if len(parts) > 1 else ""

    if not file_part:
        return text

    base_dir = get_current_dir(chat_id)
    file_path = Path(base_dir, file_part).resolve()

    if not is_path_allowed(str(file_path)):
        return (
            f"用户想读取文件 {file_part}，但该路径不在允许范围内。"
            f"请拒绝读取，并说明当前安全限制。"
        )

    if not file_path.exists():
        return (
            f"用户提到了当前项目中的路径：{file_part}，但该路径不存在。"
            f"请告诉用户文件不存在，并建议检查路径。"
        )

    if file_path.is_dir():
        try:
            rel = file_path.relative_to(Path(base_dir).resolve()).as_posix()
        except Exception:
            return "该目录不在当前工作目录内，请先用 /cd 切换到对应项目目录。"
        question = question or "请概览这个目录。"
        return (
            f"请重点阅读当前项目中的这个目录：{rel}\n"
            f"当前工作目录是：{base_dir}\n"
            "请先按需查看目录结构和关键文件，不要假设已经完整读取所有文件。\n"
            f"然后回答用户的问题：{question}"
        )

    question = question or "请解释这个文件。"
    return (
        f"请重点阅读当前项目中的这个文件：{file_part}\n"
        f"当前工作目录是：{base_dir}\n"
        f"然后回答用户的问题：{question}"
    )


def handle_message(data) -> None:
    """处理飞书接收消息事件（所有回复通过 send_text 发送，无返回值）"""
    event = data.event
    message = event.message
    chat_id = message.chat_id
    message_id = getattr(message, "message_id", "")
    sender_id = get_sender_id(event)

    if is_message_from_bot(event):
        return

    if message_id and _is_duplicate_message(message_id):
        return

    if not is_allowed_sender(event):
        send_text(chat_id, "你不在白名单中，暂时不能使用这个机器人。")
        return

    text = extract_text(message)

    if not text:
        send_text(chat_id, "我收到了消息，但暂时只支持文本。")
        return

    if text.strip() == "@":
        _remember_card_owner(chat_id, sender_id)
        base_dir = get_current_dir(chat_id)
        ok, err = open_file_picker(chat_id, base_dir, rel_path=".", offset=0, mode="read")
        if not ok:
            send_text(chat_id, f"打开文件选择器失败：{err}")
        return
    if text.strip() == "/cd":
        _remember_card_owner(chat_id, sender_id)
        base_dir = get_current_dir(chat_id)
        ok, err = open_file_picker(chat_id, base_dir, rel_path=".", offset=0, mode="cd")
        if not ok:
            send_text(chat_id, f"打开目录选择器失败：{err}")
        return
    if text.strip() == "/status":
        _remember_card_owner(chat_id, sender_id)
        tasks = list_tasks_by_chat(chat_id, limit=6)
        send_task_status_card(chat_id, tasks)
        return
    if text.strip() == "/git":
        _remember_card_owner(chat_id, sender_id)
        send_git_panel_card(chat_id)
        return
    if text.strip() in ("/delete-auth", "/revoke"):
        _remember_card_owner(chat_id, sender_id)
        if _session_allows_bypass(chat_id):
            _request_delete_auth_card(chat_id, sender_id)
        else:
            send_text(chat_id, "当前会话没有授权，无需删除。")
        return
    if text.strip() == "/test":
        _remember_card_owner(chat_id, sender_id)
        _request_test_permission(chat_id, sender_id)
        return
    if text.strip() == "/resume":
        _start_claude_task(
            chat_id,
            sender_id,
            "请继续最近一次任务，先说明当前进展，再给出下一步可执行动作。",
            continue_session=True,
        )
        return
    if _is_git_test_command(text):
        _remember_card_owner(chat_id, sender_id)
        _request_test_permission(chat_id, sender_id)
        return
    bridge_reply = handle_bridge_command(text, chat_id)
    if bridge_reply is not None:
        send_text(chat_id, bridge_reply)
        return

    continue_session = False
    user_text_for_permission = text  # 默认：权限检查用原文本
    if text.startswith("/continue "):
        continue_session = True
        text = text[len("/continue "):].strip()
        if not text:
            send_text(chat_id, "用法：/continue 你的问题")
            return
    elif _has_preloaded_context(chat_id, sender_id):
        continue_session = True
        # 权限检查应基于用户原话，而非增强后的指令文本（后者含"创建/修改/删除"）
        user_text_for_permission = text
        text = _apply_preloaded_context_to_text(chat_id, sender_id, text)

    if _is_plain_authorize_text(text) and _has_deferred_permission(chat_id, sender_id):
        send_text(chat_id, "请点击上方权限确认卡片完成授权。")
        return

    if _is_plain_authorize_text(text) and _session_allows_bypass(chat_id):
        send_text(chat_id, "当前会话已有完全授权，无需重复确认。")
        return

    if _is_explicit_authorize_text(text):
        set_session_permission_mode(chat_id, "bypassPermissions")
        send_text(chat_id, "已开启本会话完全授权，后续写入操作无需再确认。若需撤销请发送 /delete-auth。")
        return

    if _matches_delete_auth_intent(text) and _session_allows_bypass(chat_id):
        _request_delete_auth_card(chat_id, sender_id)
        send_text(chat_id, "正在处理你的请求，但需先删除已有授权，请在上方卡片确认。")
        return

    if _should_request_write_permission(user_text_for_permission) and not _session_allows_bypass(chat_id):
        _send_deferred_permission(
            chat_id,
            sender_id,
            text,
            prompt_text=text,
            continue_session=continue_session,
            apply_mobile_short=True,
        )
        return

    _start_claude_task(chat_id, sender_id, text, continue_session)


def _run_claude_and_reply(
    task_id: str,
    chat_id: str,
    text: str,
    continue_session: bool,
    chat_lock: threading.Lock,
    requester_id: str,
    apply_mobile_short: bool = True,
    permission_mode_override: str = None,
) -> None:
    """后台执行 Claude 调用并回消息。"""
    reply = ""
    task = get_task(task_id)
    slot_acquired = False
    permission_already_resolved = permission_mode_override is not None
    try:
        if task and task.cancel_event.is_set():
            mark_canceled(task_id)
            reply = "任务已取消。"
            return
        if not _acquire_global_run_slot(task):
            mark_canceled(task_id)
            reply = "任务已取消。"
            return
        slot_acquired = True
        mark_running(task_id)
        # 若 text 是 _build_path_preload_prompt 生成的预读取提示（以 @ 开头且含 "请预读取"），
        # 跳过 transform_prompt，避免其把预读取结构改写为普通 @文件 问答格式
        if text.startswith("@") and "请预读取" in text:
            prompt = text
        else:
            prompt = transform_prompt(text, chat_id)
        if apply_mobile_short:
            prompt = _apply_mobile_short_reply(prompt)

        # 启动进度提示线程：长任务超过 PROGRESS_INTERVAL_SECONDS 后定期通知用户
        progress_stop = threading.Event()
        progress_thread = threading.Thread(
            target=_send_progress_updates,
            args=(chat_id, task_id, progress_stop),
            daemon=True,
        )
        progress_thread.start()

        try:
            reply = run_claude_with_requester(
                prompt,
                chat_id,
                continue_session=continue_session,
                requester_id=requester_id,
                cancel_event=task.cancel_event if task else None,
                permission_mode_override=permission_mode_override,
            )
        finally:
            progress_stop.set()
            progress_thread.join(timeout=1)
        if reply.startswith("Claude 没有返回内容。"):
            reply = _preload_done_reply(text) or reply
        if reply == "任务已取消。":
            mark_canceled(task_id)
        elif _is_error_reply(reply):
            mark_error(task_id, reply)
        else:
            mark_success(task_id, reply)
    except Exception as e:
        logger.error(f"[task] 处理任务 {task_id} 时出错：{e}")
        reply = "处理消息时出错，请稍后重试。"
        mark_error(task_id, reply)
    finally:
        if slot_acquired:
            _global_run_semaphore.release()
        try:
            final_task = get_task(task_id)
            if MOBILE_AUTO_STATUS_CARDS and final_task:
                send_task_status_card(chat_id, [final_task])
            send_rich_text(chat_id, reply)
            if MOBILE_QUICK_ACTIONS_AFTER_REPLY and not _is_error_reply(reply) and reply != "任务已取消。":
                _remember_card_owner(chat_id, requester_id)
                send_quick_actions_card(chat_id, task_id)
            if not permission_already_resolved and _reply_requests_permission(reply):
                _send_deferred_permission(chat_id, requester_id, reply, prompt_text=text)
            elif not permission_already_resolved and _reply_requests_destructive(reply):
                _send_deferred_permission(
                    chat_id,
                    requester_id,
                    reply,
                    prompt_text=text,
                    extra_hint="⚠️ 危险操作：删除文件/目录",
                )
        finally:
            # 续期：防止长任务期间锁被 TTL 清理
            with _chat_locks_guard:
                _chat_locks_last_access[chat_id] = time.time()
            chat_lock.release()


def _get_chat_lock(chat_id: str) -> threading.Lock:
    """获取 chat_id 对应的互斥锁，并清理久未使用的锁。

    不清理当前被持有的锁，防止长任务期间锁被 TTL 误删。"""
    now = time.time()
    with _chat_locks_guard:
        stale = [
            cid for cid, ts in _chat_locks_last_access.items()
            if now - ts > _CHAT_LOCK_TTL_SECONDS
        ]
        for cid in stale:
            lock = _chat_locks.get(cid)
            if lock is not None and lock.locked():
                continue
            _chat_locks.pop(cid, None)
            _chat_locks_last_access.pop(cid, None)

        _chat_locks_last_access[chat_id] = now
        lock = _chat_locks.get(chat_id)
        if lock is None:
            lock = threading.Lock()
            _chat_locks[chat_id] = lock
        return lock


def _is_duplicate_message(message_id: str) -> bool:
    """检测并记录消息 ID，避免事件重放导致重复处理。"""
    now = time.time()
    with _processed_guard:
        expired = [mid for mid, ts in _processed_message_ids.items() if now - ts > _PROCESSED_TTL_SECONDS]
        for mid in expired:
            del _processed_message_ids[mid]

        if message_id in _processed_message_ids:
            return True
        _processed_message_ids[message_id] = now
        return False


def handle_card_action(data) -> P2CardActionTriggerResponse:
    """处理卡片按钮点击事件（权限三选一）。"""
    try:
        action = data.event.action
        value = getattr(action, "value", {}) or {}
        logger.info(f"[card_action] received value={value}")
        operator = data.event.operator
        operator_id = (
            getattr(operator, "open_id", None)
            or getattr(operator, "user_id", None)
            or getattr(operator, "union_id", None)
        )
        chat_id = getattr(data.event.context, "open_chat_id", "")

        # 权限按钮：本地内存操作，快速完成后直接回 toast。
        if (
            not value.get("kind")
            and "request_id" in value
            and value.get("action") in {"allow_once", "allow_session", "deny"}
        ):
            ok, message = resolve_permission(value.get("request_id", ""), value.get("action", ""), operator_id)
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": message if ok else f"操作失败：{message}"}})

        kind = value.get("kind")
        if kind in KNOWN_CARD_KINDS:
            if not _is_authorized_card_operator(chat_id, operator_id):
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": "仅最近发起该会话操作的用户可点击此卡片。"}}
                )

        # 其余动作异步执行，避免卡片回调超时导致 200340。
        # 仅已知 kind 才启动异步线程，防止未知 kind 浪费资源。
        if kind not in KNOWN_CARD_KINDS:
            logger.warning(f"[card_action] 未知卡片动作 kind={kind}，已忽略。")
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "未知操作。"}})
        threading.Thread(
            target=_handle_card_action_async,
            args=(chat_id, operator_id, value),
            daemon=True,
        ).start()
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已收到操作，正在处理。"}})
    except Exception as e:
        logger.error(f"[card_action] 处理失败: {e}")
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "操作接收失败，请重试。"}})


def _handle_card_action_async(chat_id: str, operator_id: str, value: dict) -> None:
    """异步处理卡片动作，避免回调阻塞。"""
    try:
        kind = value.get("kind")
        if kind == "quick_action":
            ok, text = _handle_quick_action(chat_id, operator_id, value.get("action", ""))
            send_text(chat_id, text)
            return
        if kind == "file_picker":
            act = value.get("action", "")
            rel_path = value.get("path", ".")
            mode = value.get("mode", "read")
            offset = int(value.get("offset", 0) or 0)
            ok, text = _handle_file_picker_action(chat_id, operator_id, act, rel_path, offset, mode)
            if text:
                send_text(chat_id, text)
            return
        if kind == "task_action":
            ok, text = _handle_task_action(
                chat_id,
                operator_id,
                value.get("action", ""),
                value.get("task_id", ""),
            )
            send_text(chat_id, text)
            return
        if kind == "git_action":
            ok, text = _handle_git_action(chat_id, operator_id, value.get("action", ""))
            if not ok:
                send_text(chat_id, text)
            return
        if kind == "path_draft":
            ok, text = _handle_path_draft_action(
                chat_id,
                operator_id,
                value.get("action", ""),
                value.get("path", ""),
            )
            if text:
                send_text(chat_id, text)
            return
        if kind == "deferred_permission":
            ok, text = _handle_deferred_permission_action(
                chat_id,
                operator_id,
                value.get("request_id", ""),
                value.get("action", ""),
            )
            if not ok:
                send_text(chat_id, text)
            return
        if kind == "delete_auth":
            ok, text = _handle_delete_auth_action(
                chat_id,
                operator_id,
                value.get("request_id", ""),
                value.get("action", ""),
            )
            send_text(chat_id, text)
            return
    except Exception as e:
        logger.error(f"[card_action_async] 处理失败: {e}")
        if chat_id:
            send_text(chat_id, "卡片操作处理失败，请重试。")


def _start_claude_task(
    chat_id: str,
    requester_id: str,
    text: str,
    continue_session: bool,
    apply_mobile_short: bool = True,
    notify_start: bool = True,
    permission_mode_override: str = None,
) -> tuple:
    """创建并启动 Claude 任务。"""
    get_or_create_session(chat_id)
    chat_lock = _get_chat_lock(chat_id)
    if not chat_lock.acquire(blocking=False):
        latest = get_latest_task(chat_id)
        if latest and latest.status in {"queued", "running"}:
            tail = f"（任务 {latest.task_id} · {STATUS_LABEL_MAP.get(latest.status, latest.status)}）"
            cancel_hint = f" 可发送 /cancel {latest.task_id} 取消。"
        elif latest:
            tail = f"（最近任务 {latest.task_id} · {STATUS_LABEL_MAP.get(latest.status, latest.status)}）"
            cancel_hint = ""
        else:
            tail = ""
            cancel_hint = ""
        parts = [f"上一条消息还在处理中{tail}，请稍等。"]
        if cancel_hint:
            parts.append(cancel_hint)
        parts.append("提示：发送 /status 可查看所有任务状态。")
        msg = " ".join(parts)
        send_text(chat_id, msg)
        return False, msg

    lock_held = True
    try:
        task = create_task(chat_id, requester_id, text, continue_session)
        _remember_card_owner(chat_id, requester_id)
        if notify_start:
            send_thinking_indicator(chat_id, task.task_id)
        if MOBILE_AUTO_STATUS_CARDS:
            send_task_status_card(chat_id, [task])
        threading.Thread(
            target=_run_claude_and_reply,
            args=(
                task.task_id,
                chat_id,
                text,
                continue_session,
                chat_lock,
                requester_id,
                apply_mobile_short,
                permission_mode_override,
            ),
            daemon=True,
        ).start()
        lock_held = False  # transferred to the worker thread
        return True, task.task_id
    finally:
        if lock_held:
            chat_lock.release()


def _acquire_global_run_slot(task) -> bool:
    """等待全局 Claude 执行槽位，同时允许排队任务被取消。

    最多等待 3600 秒（1 小时），超时后返回 False 避免无限等待。"""
    deadline = time.monotonic() + 3600
    while True:
        if task and task.cancel_event.is_set():
            return False
        if time.monotonic() > deadline:
            return False
        if _global_run_semaphore.acquire(timeout=0.2):
            return True


def _apply_mobile_short_reply(prompt: str) -> str:
    """默认短答模式：先给手机友好短结论。"""
    if not MOBILE_SHORT_REPLY_DEFAULT:
        return prompt
    return (
        f"先给≤{MOBILE_SHORT_REPLY_LINES}行简明结论，再补充细节。涉及代码先给关键片段。\n"
        "末尾附：改动文件 / 验证方式 / 风险点 / 下一步\n\n"
        f"{prompt}"
    )


_PROGRESS_INTERVAL_SECONDS = 45
_PROGRESS_FIRST_NOTIFY_SECONDS = 35


def _send_progress_updates(chat_id: str, task_id: str, stop_event: threading.Event) -> None:
    """在后台定期发送进度更新，直到被 stop_event 通知停止。
    首次通知在 _PROGRESS_FIRST_NOTIFY_SECONDS 后，后续每 _PROGRESS_INTERVAL_SECONDS 一次。"""
    task_start = time.monotonic()
    next_notify_at = task_start + _PROGRESS_FIRST_NOTIFY_SECONDS
    while not stop_event.wait(timeout=10):
        now = time.monotonic()
        if now >= next_notify_at:
            elapsed = int(now - task_start)
            send_progress_update(chat_id, elapsed, task_id)
            next_notify_at = now + _PROGRESS_INTERVAL_SECONDS


def _is_error_reply(reply: str) -> bool:
    """检测 bridge 自身产生的错误回复（非 Claude 正常输出）。"""
    if not reply:
        return True
    error_prefixes = (
        "Claude Code 执行失败",
        "Claude Code 执行超时",
        "Claude Code 执行遇到问题",
        "Claude 没有返回内容。",
        "Claude 未产出正文",
        "权限确认超时",
        "权限确认卡片发送失败",
        "权限确认后执行仍失败",
        "已拒绝",
        "处理消息时出错",
        "调用 Claude Code 出错",
        "找不到 claude 命令",
        "当前会话仍在被占用",
    )
    return reply.startswith(error_prefixes)


def _handle_quick_action(chat_id: str, requester_id: str, action: str) -> tuple:
    """处理快捷按钮行为。"""
    if not chat_id:
        return False, "未找到会话，请直接发送文字。"
    if action == "continue":
        text = "请基于上次回答继续推进，给出接下来 3 步可执行动作。"
    elif action == "summary":
        text = "请把你上一次回答总结为 3 点，每点不超过 20 字。"
    elif action == "commands":
        text = "请只输出可执行命令清单，每行一条，必要时附一句用途。"
    elif action == "tests":
        _request_test_permission(chat_id, requester_id)
        return True, "已发送权限确认。"
    elif action == "fix_error":
        text = "请基于上一次错误继续修复。先定位失败原因，再给出最小改动并说明验证方式。"
    elif action == "explain_changes":
        text = "请解释当前工作区改动：改了哪些文件、为什么改、风险是什么。"
    elif action == "files":
        text = "请只输出本轮相关文件列表，每行一个路径，并用短语说明作用。"
    elif action == "commit_msg":
        text = "请查看当前 git diff，生成一条简洁的提交信息和 3 点变更摘要。"
    else:
        return False, "未知快捷操作。"
    ok, info = _start_claude_task(chat_id, requester_id, text, continue_session=True)
    if not ok:
        return False, info
    return True, f"已提交快捷任务 {info}。"


def _handle_file_picker_action(
    chat_id: str,
    requester_id: str,
    action: str,
    rel_path: str,
    offset: int,
    mode: str,
) -> tuple:
    """处理文件浏览器卡片点击。"""
    if not chat_id:
        return False, "未找到会话。"

    base_dir = get_current_dir(chat_id)
    if action == "open":
        ok, err = open_file_picker(chat_id, base_dir, rel_path=rel_path, offset=offset, mode=mode)
        if not ok:
            return False, err
        return True, "已打开目录。"

    if action == "choose_dir" and mode == "cd":
        ok, selected_dir = resolve_selected_dir(base_dir, rel_path)
        if not ok:
            return False, selected_dir
        result = set_current_project_dir(selected_dir, chat_id)
        return True, result

    if action in {"use_path", "select"}:
        if mode == "cd":
            return False, "目录切换模式下请使用「选择当前目录」。"
        return _preload_path_and_start_task(chat_id, requester_id, base_dir, rel_path)

    return False, "未知文件选择动作。"


def _preload_path_and_start_task(chat_id: str, requester_id: str, base_dir: str, rel_path: str) -> tuple:
    """选中路径后的通用流程：校验 → 预读取 → 记住上下文。"""
    ok, rel_path_out, is_dir = resolve_selected_path(base_dir, rel_path)
    if not ok:
        return False, rel_path_out
    prompt = _build_path_preload_prompt(rel_path_out, is_dir)
    created, info = _start_claude_task(
        chat_id,
        requester_id,
        prompt,
        continue_session=False,
        apply_mobile_short=False,
        notify_start=False,
    )
    if not created:
        return False, info
    _remember_preloaded_context(chat_id, requester_id, rel_path_out, is_dir)
    return True, f"已开始预读取 {rel_path_out}。"


def _handle_task_action(chat_id: str, requester_id: str, action: str, task_id: str) -> tuple:
    """处理任务面板卡片动作。"""
    if not chat_id or not task_id:
        return False, "缺少任务信息。"

    # 重试防抖：3 秒内不可重复点击（仅在成功后写入，避免失败重试也被冷却）
    if action == "retry":
        key = f"{chat_id}:{task_id}:retry"
        now = time.time()
        with _card_owner_guard:
            _cleanup_task_action_cooldown_locked(now)
            last = _task_action_cooldown.get(key, 0)
            if now - last < _TASK_ACTION_COOLDOWN_SECONDS:
                return False, "操作太频繁，请稍后再试。"

    task = get_task(task_id)
    if not task or task.chat_id != chat_id:
        return False, "任务不存在，或不在当前会话。"
    if not task.requester_id or not requester_id or task.requester_id != requester_id:
        return False, "仅任务发起者可操作该任务。"

    if action == "detail":
        text = f"任务 {task.task_id} 状态：{STATUS_LABEL_MAP.get(task.status, task.status)}"
        if task.result_preview:
            text += f"\n结果预览：{task.result_preview}"
        if task.error:
            text += f"\n错误：{task.error}"
        send_text(chat_id, text)
        return True, "详情已发送。"

    if action == "cancel":
        if task.status in {"success", "error", "canceled"}:
            return False, "任务已结束，无法取消。"
        if request_cancel(task.task_id):
            return True, f"已请求取消任务 {task.task_id}。"
        return False, "取消失败。"

    if action == "retry":
        if not task.text:
            return False, "缺少原任务内容，无法重试。"
        ok, info = _start_claude_task(
            chat_id,
            requester_id or task.requester_id,
            task.text,
            task.continue_session,
        )
        if not ok:
            return False, info
        # 重试成功后写入防抖记录
        key = f"{chat_id}:{task_id}:retry"
        with _card_owner_guard:
            _task_action_cooldown[key] = time.time()
        return True, f"已重试，任务 {info}。"

    return False, "未知任务操作。"


def _handle_git_action(chat_id: str, requester_id: str, action: str) -> tuple:
    if not chat_id:
        return False, "未找到会话。"
    if action in {"test", "tests"}:
        _request_test_permission(chat_id, requester_id)
        return True, "已发送权限确认。"
    if action == "commit_msg":
        ok, info = _start_claude_task(
            chat_id,
            requester_id,
            "请查看当前 git diff，生成一条简洁的提交信息和 3 点变更摘要。",
            continue_session=False,
        )
        if not ok:
            return False, info
        return True, f"已提交任务 {info}。"
    text = build_git_reply(action, get_current_dir(chat_id))
    send_text(chat_id, text)
    return True, "Git 结果已发送。"


def _handle_path_draft_action(chat_id: str, requester_id: str, action: str, rel_path: str) -> tuple:
    """兼容旧路径草稿卡。新流程选中路径后会直接预读取。"""
    if action == "clear":
        send_text(chat_id, "当前没有 @ 路径草稿；现在选中路径后会直接预读取。")
        return True, "已忽略旧草稿。"
    if action == "drill_down" and rel_path:
        base_dir = get_current_dir(chat_id)
        ok, err = open_file_picker(chat_id, base_dir, rel_path=rel_path, offset=0, mode="read")
        if not ok:
            return False, err
        return True, "已打开子路径。"
    if rel_path:
        base_dir = get_current_dir(chat_id)
        return _preload_path_and_start_task(chat_id, requester_id, base_dir, rel_path)
    return False, "旧路径草稿已失效，请重新发送 @ 选择路径。"


def _request_delete_auth_card(chat_id: str, requester_id: str) -> None:
    """发送删除授权确认卡片。"""
    if not chat_id:
        return
    request_id = str(uuid.uuid4())
    _remember_card_owner(chat_id, requester_id)
    sent = send_delete_auth_card(chat_id, request_id)
    if not sent:
        send_text(chat_id, "删除授权卡片发送失败，请稍后重试。")


def _handle_delete_auth_action(
    chat_id: str,
    requester_id: str,
    request_id: str,
    action: str,
) -> tuple:
    """处理删除授权卡片动作。"""
    if not chat_id:
        return False, "未找到会话。"

    if action == "cancel":
        return True, "已取消删除授权。"

    if action != "confirm":
        return False, "未知操作。"

    clear_session_permission_mode(chat_id)
    send_text(chat_id, "已删除本会话授权，写入操作需要重新授权。")
    return True, "已删除授权。"


def _handle_deferred_permission_action(
    chat_id: str,
    requester_id: str,
    request_id: str,
    action: str,
) -> tuple:
    """处理 Claude 文本授权请求对应的三选一卡片。"""
    if not chat_id or not request_id:
        return False, "缺少权限请求信息。"

    item = _pop_deferred_permission(request_id, chat_id, requester_id)
    if not item:
        return False, "该权限请求已过期或已处理。"

    if action == "deny":
        send_text(chat_id, "已拒绝本次执行。")
        return True, "已拒绝。"
    if action not in {"allow_once", "allow_session"}:
        return False, "未知权限操作。"

    if action == "allow_session":
        set_session_permission_mode(chat_id, "bypassPermissions")

    prompt_text = item.get("prompt_text", "")
    if prompt_text:
        prompt = prompt_text
        continue_session = bool(item.get("continue_session", False))
        apply_mobile_short = bool(item.get("apply_mobile_short", True))
    else:
        prompt = (
            "用户已通过飞书权限确认卡片授权。\n"
            "请继续执行刚才请求授权的操作，只执行刚才已经确认的最小改动。\n"
            "必须实际使用工具完成并读取文件验证。\n"
            "如果验证失败，必须明确说明失败，不要声称已完成。"
        )
        continue_session = True
        apply_mobile_short = True

    ok, info = _start_claude_task(
        chat_id,
        requester_id,
        prompt,
        continue_session=continue_session,
        apply_mobile_short=apply_mobile_short,
        permission_mode_override="bypassPermissions",
        notify_start=False,
    )
    if not ok:
        return False, info
    return True, "已授权，正在处理任务。"


def _is_git_test_command(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized.startswith("/git test")


def _request_test_permission(chat_id: str, requester_id: str) -> None:
    """运行测试属于命令执行，先在飞书里确认，再交给 Claude Code。"""
    prompt_text = _build_authorized_test_prompt(chat_id)
    if _session_allows_bypass(chat_id):
        _start_claude_task(
            chat_id,
            requester_id,
            prompt_text,
            continue_session=False,
            permission_mode_override="bypassPermissions",
        )
        return
    _send_deferred_permission(
        chat_id,
        requester_id,
        "运行当前项目测试命令",
        prompt_text=prompt_text,
        continue_session=False,
        apply_mobile_short=True,
    )


def _build_authorized_test_prompt(chat_id: str) -> str:
    current_dir = get_current_dir(chat_id)
    return (
        "用户已通过飞书权限确认卡片授权你运行当前项目的测试命令。\n"
        f"当前工作目录：{current_dir}\n"
        "请自动识别并运行合适的测试或检查命令，只执行测试/检查类命令。\n"
        "不要创建、修改或删除文件。\n"
        "回复时必须包含实际执行的命令和结果摘要；如果没有真实执行成功，必须明确说明原因。"
    )


def _build_path_preload_prompt(rel_path: str, is_dir: bool) -> str:
    kind = "目录" if is_dir else "文件"
    return (
        f"@{rel_path} 请预读取这个{kind}，用于接下来继续问答。\n"
        "要求：\n"
        "1. 只做阅读和理解，不要修改文件，不要执行有副作用的命令。\n"
        "2. 如果是目录，请先查看目录结构，再按需阅读关键文件，记住结构、职责和后续可能需要追问的上下文。\n"
        "3. 不要输出分析过程、文件摘要、命令清单或建议。\n"
        f"4. 完成后只回复一句：已读完 {rel_path}，可以继续提问。"
    )


def _preload_done_reply(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        marker = "完成后只回复一句："
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return ""


def _reply_requests_permission(reply: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    patterns = [
        "授权吗",
        "是否授权",
        "是否允许",
        "允许执行吗",
        "允许写入吗",
        "需要您确认",
        "请点击上方",
        "确认授权后",
    ]
    return any(pattern in text for pattern in patterns)


def _reply_requests_destructive(reply: str) -> bool:
    """检测 Claude 回复是否在请求删除/破坏性操作的授权。"""
    text = (reply or "").strip().lower()
    destructive_patterns = [
        "删除",
        "remove",
        "delete",
        "drop",
        "销毁",
        "清空",
    ]
    permission_patterns = [
        "授权",
        "允许",
        "是否允许",
        "需要确认",
    ]
    return any(p in text for p in destructive_patterns) and any(p in text for p in permission_patterns)


def _should_request_write_permission(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s or s.startswith("/"):
        return False

    # 明显的疑问/阅读意图，不触发权限卡
    interrogative_markers = [
        "可行吗", "能不能", "能否", "先告诉我", "告诉我可行",
        "查看", "解释", "分析", "怎么", "如何", "方案", "建议", "思路",
        "什么意思", "是什么", "怎么用", "有什么区别",
    ]
    if any(marker in s for marker in interrogative_markers):
        return False
    # 问号结尾大概率是提问
    if text.rstrip().endswith("?") or text.rstrip().endswith("？"):
        return False

    # 明确的写入/修改意图（复合词降低误触）
    write_markers = [
        "创建", "新建", "添加", "增加", "实现", "修复",
        "改一下", "改成", "帮我写", "写个", "写一个", "写入",
        "修改", "调整", "补充", "删除", "移除", "重命名", "保存",
        "安装", "你创建吧",
        "帮我执行", "请执行", "执行一下", "执行这个",
        "帮我运行", "请运行", "运行一下", "跑一下", "跑测试",
        "执行命令", "运行命令", "输命令",
        "帮我测试", "请测试", "测试一下",
    ]
    if any(marker in s for marker in write_markers):
        return True

    # 英文写入意图
    en_markers = ["create", "write", "modify", "delete", "remove", "rename", "execute", "run", "command", "bash", "shell"]
    if any(marker in s for marker in en_markers):
        return True

    return False


def _session_allows_bypass(chat_id: str) -> bool:
    return get_session_permission_mode(chat_id) == "bypassPermissions"


def _is_plain_authorize_text(text: str) -> bool:
    """快速授权短文本（仅在已有待处理权限卡或已授权会话的上下文使用）。"""
    normalized = (text or "").strip().lower()
    return normalized in {"授权", "允许", "同意", "approve", "allow", "yes", "y"}


def _is_explicit_authorize_text(text: str) -> bool:
    """显式授权文本（用于无上下文时直接开启 bypass，防止单字误触）。"""
    normalized = (text or "").strip()
    return normalized in {
        "开启授权", "完全授权", "永久授权", "授予完整权限",
        "启用完全授权", "开启完全授权", "永久允许", "后续都允许", "永远允许",
    }


def _matches_delete_auth_intent(text: str) -> bool:
    """检测是否要删除/撤销授权的自然语言意图。"""
    s = (text or "").strip().lower()
    delete_auth_markers = [
        "删除授权",
        "清除授权",
        "取消授权",
        "撤销授权",
        "收回授权",
        "清除权限",
        "删除权限",
        "取消权限",
        "吊销授权",
        "移除授权",
        "重置授权",
        "清除会话权限",
        "delete auth",
        "revoke auth",
        "remove auth",
        "clear auth",
    ]
    return any(marker in s for marker in delete_auth_markers)


def _send_deferred_permission(
    chat_id: str,
    requester_id: str,
    reply: str,
    prompt_text: str = "",
    continue_session: bool = True,
    apply_mobile_short: bool = True,
    extra_hint: str = "",
) -> None:
    if not chat_id:
        return
    request_id = str(uuid.uuid4())
    with _card_owner_guard:
        _cleanup_deferred_permissions_locked()
        _deferred_permissions[request_id] = {
            "chat_id": chat_id,
            "requester_id": requester_id,
            "created_at": time.time(),
            "prompt_text": prompt_text,
            "continue_session": continue_session,
            "apply_mobile_short": apply_mobile_short,
        }
    _remember_card_owner(chat_id, requester_id)
    try:
        sent = send_deferred_permission_card(chat_id, request_id, reply, extra_hint)
    except Exception as e:
        logger.error(f"[permission] 发送延迟权限卡失败: {e}")
        sent = False
    if not sent:
        with _card_owner_guard:
            _deferred_permissions.pop(request_id, None)
        send_text(chat_id, "权限确认卡片发送失败，请稍后重试。")


def _has_deferred_permission(chat_id: str, requester_id: str) -> bool:
    with _card_owner_guard:
        _cleanup_deferred_permissions_locked()
        for item in _deferred_permissions.values():
            item_chat_id = item.get("chat_id", "")
            item_requester_id = item.get("requester_id", "")
            if item_chat_id != chat_id:
                continue
            if not item_requester_id or not requester_id or item_requester_id != requester_id:
                continue
            return True
    return False


def _pop_deferred_permission(request_id: str, chat_id: str, requester_id: str):
    with _card_owner_guard:
        _cleanup_deferred_permissions_locked()
        item = _deferred_permissions.get(request_id)
        if not item:
            return None
        item_chat_id = item.get("chat_id", "")
        item_requester_id = item.get("requester_id", "")
        if item_chat_id != chat_id:
            return None
        if not item_requester_id or not requester_id or item_requester_id != requester_id:
            return None
        return _deferred_permissions.pop(request_id)


def _remember_card_owner(chat_id: str, owner_id: str) -> None:
    if not chat_id or not owner_id:
        return
    with _card_owner_guard:
        _cleanup_card_owner_locked()
        # 仅在尚无 owner 或调用者是当前 owner 时更新，防止用户 B 覆盖用户 A 的卡片所有权
        existing = _card_owner_by_chat.get(chat_id)
        if existing is None or existing[0] == owner_id:
            _card_owner_by_chat[chat_id] = (owner_id, time.time())


def _remember_preloaded_context(chat_id: str, owner_id: str, rel_path: str, is_dir: bool) -> None:
    if not chat_id or not owner_id:
        return
    with _card_owner_guard:
        _cleanup_preloaded_context_locked()
        _preloaded_context_by_chat[chat_id] = (owner_id, rel_path, is_dir, time.time())


def _has_preloaded_context(chat_id: str, owner_id: str) -> bool:
    if not chat_id:
        return False
    with _card_owner_guard:
        _cleanup_preloaded_context_locked()
        item = _preloaded_context_by_chat.get(chat_id)
        if not item:
            return False
        context_owner, _rel_path, _is_dir, _ = item
        return not context_owner or not owner_id or context_owner == owner_id


def _apply_preloaded_context_to_text(chat_id: str, owner_id: str, text: str) -> str:
    with _card_owner_guard:
        _cleanup_preloaded_context_locked()
        item = _preloaded_context_by_chat.get(chat_id)
        if not item:
            return text
        context_owner, rel_path, is_dir, _ = item
        if context_owner and owner_id and context_owner != owner_id:
            return text
    kind = "目录" if is_dir else "文件"
    return (
        f"当前用户通过 @ 选择并预读取的{kind}是：{rel_path}。\n"
        f"如果用户说“当前文件夹/当前目录/这里/这个文件夹”，默认指这个路径。\n"
        "如果本轮需要创建、修改或删除文件，必须实际使用 Claude Code 工具完成，并在回复前读取/检查文件验证。\n"
        "如果没有实际验证成功，不能声称已经完成。\n\n"
        f"用户问题：{text}"
    )


def _is_authorized_card_operator(chat_id: str, operator_id: str) -> bool:
    if not chat_id:
        return False
    if not operator_id:
        return False  # 缺失操作者身份时拒绝，不允许匿名绕过
    with _card_owner_guard:
        _cleanup_card_owner_locked()
        item = _card_owner_by_chat.get(chat_id)
        if not item:
            return True
        owner_id, _ = item
        return owner_id == operator_id


def _cleanup_task_action_cooldown_locked(now: float) -> None:
    expired = [
        k for k, ts in _task_action_cooldown.items()
        if now - ts > _TASK_ACTION_COOLDOWN_TTL
    ]
    for k in expired:
        del _task_action_cooldown[k]


def _cleanup_card_owner_locked() -> None:
    now = time.time()
    expired = [cid for cid, (_, ts) in _card_owner_by_chat.items() if now - ts > _CARD_OWNER_TTL_SECONDS]
    for cid in expired:
        del _card_owner_by_chat[cid]


def _cleanup_preloaded_context_locked() -> None:
    now = time.time()
    expired = [
        cid
        for cid, (_, _, _, ts) in _preloaded_context_by_chat.items()
        if now - ts > _PRELOADED_CONTEXT_TTL_SECONDS
    ]
    for cid in expired:
        del _preloaded_context_by_chat[cid]


def _cleanup_deferred_permissions_locked() -> None:
    now = time.time()
    expired = [
        request_id
        for request_id, item in _deferred_permissions.items()
        if now - item.get("created_at", 0) > _DEFERRED_PERMISSION_TTL_SECONDS
    ]
    for request_id in expired:
        del _deferred_permissions[request_id]
