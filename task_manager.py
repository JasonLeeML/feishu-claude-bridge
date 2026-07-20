"""任务管理：为移动端提供 task_id、状态查询与取消（含持久化与清理）。"""
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import config

logger = logging.getLogger(__name__)

TASKS_FILE = config.BASE_DIR / "tasks.json"
TASK_RETENTION_SECONDS = 24 * 3600
TASK_PER_CHAT_MAX = 100
TASK_GLOBAL_MAX = 2000


@dataclass
class TaskInfo:
    task_id: str
    chat_id: str
    requester_id: Optional[str]
    text: str
    continue_session: bool
    status: str = "queued"  # queued/running/success/error/canceled
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    result_preview: str = ""
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)


_tasks: Dict[str, TaskInfo] = {}
_latest_task_by_chat: Dict[str, str] = {}
_lock = threading.Lock()


def create_task(chat_id: str, requester_id: Optional[str], text: str, continue_session: bool) -> TaskInfo:
    with _lock:
        _cleanup_locked()
        while True:
            task_id = str(uuid.uuid4())[:8]
            if task_id not in _tasks:
                break

        task = TaskInfo(
            task_id=task_id,
            chat_id=chat_id,
            requester_id=requester_id,
            text=text,
            continue_session=continue_session,
        )
        _tasks[task.task_id] = task
        _latest_task_by_chat[chat_id] = task.task_id
        _save_locked()
        return _clone_task(task)


def mark_running(task_id: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.status = "running"
        task.started_at = time.time()
        _cleanup_locked()
        _save_locked()


def mark_success(task_id: str, result_preview: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.status = "success"
        task.finished_at = time.time()
        task.result_preview = (result_preview or "")[:180]
        _cleanup_locked()
        _save_locked()


def mark_error(task_id: str, error: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.status = "error"
        task.finished_at = time.time()
        task.error = (error or "")[:300]
        _cleanup_locked()
        _save_locked()


def mark_canceled(task_id: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.status = "canceled"
        task.finished_at = time.time()
        _cleanup_locked()
        _save_locked()


def get_task(task_id: str) -> Optional[TaskInfo]:
    with _lock:
        task = _tasks.get(task_id)
        return _clone_task(task) if task else None


def get_latest_task(chat_id: str) -> Optional[TaskInfo]:
    with _lock:
        tid = _latest_task_by_chat.get(chat_id)
        task = _tasks.get(tid) if tid else None
        return _clone_task(task) if task else None


def request_cancel(task_id: str) -> bool:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return False
        task.cancel_event.set()
        if task.status == "queued":
            task.status = "canceled"
            task.finished_at = time.time()
        _cleanup_locked()
        _save_locked()
    return True


def list_tasks_by_chat(chat_id: str, limit: int = 6):
    """按创建时间倒序返回会话最近任务。"""
    with _lock:
        _cleanup_locked()
        matched = [t for t in _tasks.values() if t.chat_id == chat_id]
        matched.sort(key=lambda x: x.created_at, reverse=True)
        picked = matched[: max(1, limit)]
        return [_clone_task(t) for t in picked]


def _clone_task(task: TaskInfo) -> TaskInfo:
    return TaskInfo(
        task_id=task.task_id,
        chat_id=task.chat_id,
        requester_id=task.requester_id,
        text=task.text,
        continue_session=task.continue_session,
        status=task.status,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        result_preview=task.result_preview,
        error=task.error,
        cancel_event=task.cancel_event,  # share same Event reference so .is_set() tracks original
    )


def _cleanup_locked() -> None:
    now = time.time()

    # 1) 移除过期已结束任务
    expired = [
        tid
        for tid, t in _tasks.items()
        if t.status in {"success", "error", "canceled"} and t.finished_at and now - t.finished_at > TASK_RETENTION_SECONDS
    ]
    for tid in expired:
        del _tasks[tid]

    # 2) 每个会话上限
    per_chat: Dict[str, list] = {}
    for t in _tasks.values():
        per_chat.setdefault(t.chat_id, []).append(t)
    for chat_id, arr in per_chat.items():
        arr.sort(key=lambda x: x.created_at, reverse=True)
        for t in arr[TASK_PER_CHAT_MAX:]:
            _tasks.pop(t.task_id, None)

    # 3) 全局上限
    all_tasks = sorted(_tasks.values(), key=lambda x: x.created_at, reverse=True)
    for t in all_tasks[TASK_GLOBAL_MAX:]:
        _tasks.pop(t.task_id, None)

    _rebuild_latest_locked()


def _rebuild_latest_locked() -> None:
    _latest_task_by_chat.clear()
    all_tasks = sorted(_tasks.values(), key=lambda x: x.created_at)
    for t in all_tasks:
        _latest_task_by_chat[t.chat_id] = t.task_id


def _save_locked() -> None:
    if not config.PERSIST_TASKS:
        return
    payload = []
    for t in _tasks.values():
        payload.append(
            {
                "task_id": t.task_id,
                "chat_id": t.chat_id,
                "requester_id": t.requester_id,
                "text": t.text,
                "continue_session": t.continue_session,
                "status": t.status,
                "created_at": t.created_at,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
                "result_preview": t.result_preview,
                "error": t.error,
            }
        )
    try:
        tmp = TASKS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(TASKS_FILE)
    except Exception as e:
        logger.error(f"[task_manager] 保存任务失败: {e}")


def _load_tasks() -> None:
    if not config.PERSIST_TASKS:
        return
    if not TASKS_FILE.exists():
        return
    try:
        raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            logger.warning(f"[task_manager] tasks.json 格式错误，期望 list，得到 {type(raw).__name__}。")
            return
        now = time.time()
        for item in raw:
            if not isinstance(item, dict):
                continue
            task = TaskInfo(
                task_id=str(item.get("task_id", ""))[:8] or str(uuid.uuid4())[:8],
                chat_id=str(item.get("chat_id", "")),
                requester_id=item.get("requester_id"),
                text=str(item.get("text", "")),
                continue_session=bool(item.get("continue_session", False)),
                status=str(item.get("status", "error")),
                created_at=float(item.get("created_at", now)),
                started_at=float(item.get("started_at", 0.0)),
                finished_at=float(item.get("finished_at", 0.0)),
                result_preview=str(item.get("result_preview", "")),
                error=str(item.get("error", "")),
                cancel_event=threading.Event(),
            )
            if not task.chat_id:
                continue
            # 重启后，将未结束任务标记为中断失败，避免长期 hanging。
            if task.status in {"queued", "running"}:
                task.status = "error"
                task.finished_at = now
                task.error = "服务重启导致任务中断。"
            _tasks[task.task_id] = task
        _cleanup_locked()
    except json.JSONDecodeError as e:
        logger.warning(f"[task_manager] tasks.json 格式错误 ({e})，将重新创建。")
        return
    except Exception as e:
        logger.warning(f"[task_manager] 加载 tasks.json 失败 ({e})，将重新创建。")
        return


def get_task_stats() -> dict:
    """返回任务统计信息（总数、活跃数）。"""
    with _lock:
        active = sum(1 for t in _tasks.values() if t.status in {"queued", "running"})
        return {"total": len(_tasks), "active": active}


def cancel_all_running() -> list:
    """取消所有运行中/排队的任务，返回已取消的 task_id 列表（供优雅退出使用）。"""
    with _lock:
        task_ids = []
        for task in _tasks.values():
            if task.status in {"queued", "running"}:
                task.cancel_event.set()
                task_ids.append(task.task_id)
        for task_id in task_ids:
            task = _tasks.get(task_id)
            if task:
                task.status = "canceled"
                task.finished_at = time.time()
        _cleanup_locked()
        _save_locked()
        return task_ids


with _lock:
    _load_tasks()
