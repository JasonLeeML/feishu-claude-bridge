"""会话管理模块"""
import json
import logging
import threading
import uuid

import config

logger = logging.getLogger(__name__)

CONVERSATION_SESSIONS_FILE = config.BASE_DIR / "sessions.json"
_sessions_lock = threading.RLock()


def load_sessions() -> dict:
    """加载持久化的会话"""
    if not config.PERSIST_SESSIONS:
        return {}
    if CONVERSATION_SESSIONS_FILE.exists():
        try:
            return json.loads(CONVERSATION_SESSIONS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.warning(f"[session] sessions.json 格式错误 ({e})，将重新创建。")
        except Exception as e:
            logger.warning(f"[session] 加载 sessions.json 失败 ({e})，将重新创建。")
    return {}


def _save_sessions_locked():
    """在持锁状态下保存会话到磁盘（原子写）。"""
    if not config.PERSIST_SESSIONS:
        return
    try:
        tmp_file = CONVERSATION_SESSIONS_FILE.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(conversation_sessions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_file.replace(CONVERSATION_SESSIONS_FILE)
    except Exception as e:
        logger.error(f"保存会话失败: {e}")


conversation_sessions = load_sessions()  # chat_id → {"session_id": uuid, "current_dir": path}


def save_sessions():
    """保存会话到磁盘。"""
    with _sessions_lock:
        _save_sessions_locked()


def get_or_create_session(chat_id: str) -> dict:
    """获取或创建会话"""
    with _sessions_lock:
        if chat_id not in conversation_sessions:
            conversation_sessions[chat_id] = {
                "session_id": str(uuid.uuid4()),
                "current_dir": config.CURRENT_PROJECT_DIR
            }
            _save_sessions_locked()
        return dict(conversation_sessions[chat_id])


def clear_session(chat_id: str):
    """清除指定 chat_id 的会话"""
    with _sessions_lock:
        if chat_id in conversation_sessions:
            del conversation_sessions[chat_id]
            _save_sessions_locked()


def set_session_dir(chat_id: str, path: str):
    """设置指定会话的工作目录"""
    with _sessions_lock:
        if chat_id in conversation_sessions:
            conversation_sessions[chat_id]["current_dir"] = path
            _save_sessions_locked()


def rotate_session_id(chat_id: str) -> str:
    """为指定 chat_id 生成新的 session_id 并持久化。调用方保证会话已存在。"""
    with _sessions_lock:
        if chat_id not in conversation_sessions:
            logger.warning(f"[session] rotate_session_id 在会话不存在时被调用（chat_id={chat_id[:8]}...），已自动创建。")
            conversation_sessions[chat_id] = {
                "session_id": str(uuid.uuid4()),
                "current_dir": config.CURRENT_PROJECT_DIR
            }
        new_id = str(uuid.uuid4())
        conversation_sessions[chat_id]["session_id"] = new_id
        _save_sessions_locked()
        return new_id


def get_session(chat_id: str) -> dict:
    """获取会话信息，不存在返回空字典。"""
    with _sessions_lock:
        sess = conversation_sessions.get(chat_id)
        return dict(sess) if sess else {}


def list_sessions() -> dict:
    """获取全部会话快照。"""
    with _sessions_lock:
        return {cid: dict(sess) for cid, sess in conversation_sessions.items()}
