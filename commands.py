"""Bridge 命令处理模块"""
import config
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from permission_flow import clear_session_permission_mode
from session import clear_session, get_or_create_session, get_session, list_sessions, set_session_dir
from task_manager import get_latest_task, get_task, get_task_stats, request_cancel
from utils import is_path_allowed, STATUS_LABEL_MAP

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".claude",
    ".codex",
    ".agents",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".pytest_cache",
}
MAX_SEARCH_FILES = 5000
MAX_TEXT_BYTES = 512 * 1024


def set_current_project_dir(path: str, chat_id: str = None) -> str:
    """切换当前工作目录，显示旧目录 -> 新目录。"""
    new_path = str(Path(path).expanduser().resolve())

    if not os.path.isdir(new_path):
        return f"目录不存在：{Path(new_path).name}"

    if not is_path_allowed(new_path):
        return "不允许切换到该目录。使用 /cd 切换到已允许的目录，或联系管理员添加路径。"

    old_path = None
    if chat_id:
        sess = get_session(chat_id)
        old_path = sess.get("current_dir") if sess else None
        get_or_create_session(chat_id)
        set_session_dir(chat_id, new_path)

    if old_path and old_path != new_path:
        return f"已切换工作目录：\n`{old_path}` → `{new_path}`"
    return f"已切换工作目录：{new_path}"


def handle_bridge_command(text: str, chat_id: str = None) -> Optional[str]:
    """处理 bridge 自己的命令；返回 None 表示不是 bridge 命令"""
    text = text.strip()

    # 获取当前会话的目录
    if chat_id:
        sess = get_session(chat_id)
        if sess:
            current_dir = sess["current_dir"]
        else:
            current_dir = config.CURRENT_PROJECT_DIR
    else:
        current_dir = config.CURRENT_PROJECT_DIR

    if text == "/help":
        return (
            "📁 **导航与目录**\n"
            "`/pwd` 查看当前目录\n"
            "`/cd` 打开目录选择卡片\n"
            "`/cd 路径` 切换工作目录\n"
            "`@` 打开文件选择器（选择后自动预读取）\n"
            "`@文件 问题` 让 Claude 重点阅读指定文件\n\n"
            "💬 **会话管理**\n"
            "`/sessions` 查看会话信息\n"
            "`/reset` 重置当前会话 + 授权\n"
            "`/clear` 清除当前会话 + 授权\n"
            "`/continue 问题` 继续 Claude Code 最近会话\n"
            "`/resume` 继续最近一次任务\n\n"
            "📋 **任务**\n"
            "`/status` 打开最近任务卡片\n"
            "`/status ID` 查看指定任务\n"
            "`/cancel ID` 取消任务（/cancel 取消最近）\n"
            "`/last` 查看最近任务详情\n\n"
            "🔀 **Git**\n"
            "`/git` 打开 Git 快捷面板\n"
            "`/git status|diff|log|files` 直接查看 Git 信息\n"
            "`/git test` 或 `/test` 授权后跑测试\n\n"
            "🔍 **搜索**\n"
            "`/find 关键词` 按文件名搜索\n"
            "`/grep 关键词` 按文件内容搜索\n"
            "`/open 路径[:行号]` 预览文件片段\n\n"
            "🔒 **权限**\n"
            "`/delete-auth` 或 `/revoke` 删除会话授权\n\n"
            "🩺 **诊断**\n"
            "`/health` 检查 Bridge 和 Claude CLI 状态\n\n"
            "发送消息 → 普通 Claude Code 问答（当前目录）"
        )

    if text == "/health":
        return _build_health_reply()

    if text == "/pwd":
        return f"当前工作目录：{current_dir}"

    if text == "/reset":
        clear_session(chat_id) if chat_id else None
        if chat_id:
            clear_session_permission_mode(chat_id)
        return "已重置当前会话：Claude 对话历史已清除，工作目录恢复为默认值，会话授权已撤销。"

    if text == "/clear":
        clear_session(chat_id)
        if chat_id:
            clear_session_permission_mode(chat_id)
        return "已清除当前会话，重新开始对话吧！"

    if text == "/sessions":
        sessions = list_sessions()
        if not sessions:
            return "暂无会话记录"
        if chat_id:
            current = sessions.get(chat_id)
            if not current:
                return f"当前会话不存在。总会话数：{len(sessions)}"
            return (
                f"当前会话：{chat_id[:8]}...\n"
                f"工作目录：{current['current_dir']}\n"
                f"总会话数：{len(sessions)}"
            )
        return f"总会话数：{len(sessions)}"

    if text.startswith("/status"):
        arg = text[len("/status"):].strip()
        task = get_task(arg) if arg else (get_latest_task(chat_id) if chat_id else None)
        if not task:
            return "未找到任务。用法：/status 或 /status task_id"
        if task.chat_id != chat_id:
            return "只能查看当前会话的任务状态。"
        status_text = STATUS_LABEL_MAP.get(task.status, task.status)
        tail = ""
        if task.status == "success" and task.result_preview:
            tail = f"\n结果预览：{task.result_preview}"
        if task.status == "error" and task.error:
            tail = f"\n错误：{task.error}"
        return f"任务 {task.task_id} 状态：{status_text}{tail}"

    if text == "/last":
        task = get_latest_task(chat_id) if chat_id else None
        if not task:
            return "未找到最近任务。"
        return _format_task_detail(task)

    if text.startswith("/cancel"):
        arg = text[len("/cancel"):].strip()
        task = get_task(arg) if arg else (get_latest_task(chat_id) if chat_id else None)
        if not task:
            return "未找到任务。用法：/cancel 或 /cancel task_id"
        if task.chat_id != chat_id:
            return "只能取消当前会话的任务。"
        if task.status in {"success", "error", "canceled"}:
            return f"任务 {task.task_id} 已结束，当前状态：{task.status}"
        if request_cancel(task.task_id):
            return f"已请求取消任务 {task.task_id}。"
        return "取消失败，请稍后重试。"

    if text.startswith("/cd "):
        path = text[len("/cd "):].strip()
        if not path:
            return "用法：/cd ~/projects/xxx"
        if not os.path.isabs(path):
            path = os.path.join(current_dir, path)
        return set_current_project_dir(path, chat_id)

    if text.startswith("/git "):
        action = text[len("/git "):].strip() or "status"
        return build_git_reply(action, current_dir)

    if text.startswith("/find "):
        keyword = text[len("/find "):].strip()
        return find_files_reply(current_dir, keyword)

    if text.startswith("/grep "):
        keyword = text[len("/grep "):].strip()
        return grep_reply(current_dir, keyword)

    if text.startswith("/open "):
        target = text[len("/open "):].strip()
        return open_preview_reply(current_dir, target)

    return None


def build_git_reply(action: str, current_dir: str) -> str:
    """生成 Git 快捷动作的文本结果。"""
    action = (action or "status").strip().lower()
    if action in {"status", "st"}:
        return _run_command(["git", "status", "--short", "--branch"], current_dir, timeout=10)
    if action in {"diff", "d"}:
        return _run_command(["git", "diff", "--stat"], current_dir, timeout=10)
    if action in {"files", "file"}:
        return _run_command(["git", "diff", "--name-status"], current_dir, timeout=10)
    if action in {"log", "branch"}:
        branch = _run_command(["git", "branch", "--show-current"], current_dir, timeout=10)
        log = _run_command(["git", "log", "--oneline", "-5"], current_dir, timeout=10)
        return f"当前分支：{branch.strip() or '(未知)'}\n最近提交：\n{log.strip() or '(无)'}"
    if action in {"test", "tests"}:
        return "运行测试需要先确认权限。请发送 /test，或点击 Git 面板里的“运行测试”。"
    return "未知 Git 动作。可用：status、diff、log、files、test"


def find_files_reply(current_dir: str, keyword: str) -> str:
    if not keyword:
        return "用法：/find 关键词"
    keyword_lower = keyword.lower()
    matches = []
    for path in _iter_project_files(current_dir):
        if keyword_lower in path.name.lower():
            matches.append(path.relative_to(current_dir).as_posix())
        if len(matches) >= 40:
            break
    if not matches:
        return f"未找到文件名包含 `{keyword}` 的文件。"
    return "找到这些文件：\n" + "\n".join(matches)


def grep_reply(current_dir: str, keyword: str) -> str:
    if not keyword:
        return "用法：/grep 关键词"
    matches = []
    keyword_lower = keyword.lower()
    for path in _iter_project_files(current_dir):
        if len(matches) >= 40:
            break
        try:
            if path.stat().st_size > MAX_TEXT_BYTES:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        rel = path.relative_to(current_dir).as_posix()
        for lineno, line in enumerate(lines, 1):
            if keyword_lower in line.lower():
                matches.append(f"{rel}:{lineno}: {line.strip()[:160]}")
                if len(matches) >= 40:
                    break
    if not matches:
        return f"未找到内容包含 `{keyword}` 的文本。"
    return "匹配结果：\n" + "\n".join(matches)


def open_preview_reply(current_dir: str, target_expr: str) -> str:
    if not target_expr:
        return "用法：/open 路径[:行号]"

    path_expr, line_no = _split_path_line(target_expr)
    ok, target_or_error = _resolve_project_path(current_dir, path_expr)
    if not ok:
        return target_or_error
    target = target_or_error
    if not target.exists() or not target.is_file():
        return "目标不存在，或不是文件。"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return f"读取文件失败：{e}"

    if not lines:
        return f"{target.relative_to(current_dir).as_posix()} 是空文件。"
    center = max(1, min(line_no or 1, len(lines)))
    start = max(1, center - 20)
    end = min(len(lines), center + 40)
    rel = target.relative_to(current_dir).as_posix()
    body = "\n".join(f"{idx:>4} | {lines[idx - 1]}" for idx in range(start, end + 1))
    return f"{rel}:{center}\n```text\n{body}\n```"


def _format_task_detail(task) -> str:
    text = f"最近任务 {task.task_id}：{STATUS_LABEL_MAP.get(task.status, task.status)}"
    if task.result_preview:
        text += f"\n结果预览：{task.result_preview}"
    if task.error:
        text += f"\n错误：{task.error}"
    if task.text:
        text += f"\n原始请求：{task.text[:160]}"
    return text


def _iter_project_files(current_dir: str):
    base = Path(current_dir).resolve()
    if not is_path_allowed(str(base)):
        return
    seen = 0
    stack = [base]
    while stack and seen < MAX_SEARCH_FILES:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception:
            continue
        for item in entries:
            if item.name in SKIP_DIRS or (item.name.startswith(".") and item.name not in {".env.example"}):
                continue
            if item.is_dir():
                stack.append(item)
            elif item.is_file():
                # 符号链接/挂载点可能指向 allowed_roots 之外，需校验解析后的真实路径
                if item.is_symlink() and not is_path_allowed(str(item.resolve())):
                    continue
                seen += 1
                yield item
                if seen >= MAX_SEARCH_FILES:
                    break


def _split_path_line(expr: str) -> tuple:
    m = re.match(r"^(.*?):(\d+)$", expr)
    if not m:
        return expr, None
    return m.group(1), int(m.group(2))


def _resolve_project_path(current_dir: str, path_expr: str):  # -> (bool, Path | str)
    base = Path(current_dir).resolve()
    target = Path(path_expr).expanduser()
    if not target.is_absolute():
        target = base / target
    target = target.resolve()
    if not is_path_allowed(str(target)):
        return False, "路径不在允许范围内。使用 /cd 切换到已允许的目录，或联系管理员。"
    try:
        target.relative_to(base)
    except Exception:
        return False, "路径不在当前工作目录内。使用 /cd 切换到包含该路径的目录。"
    return True, target


def _build_health_reply() -> str:
    """构建 Bridge 健康检查和诊断信息。"""
    lines = ["🩺 **Bridge 健康检查**\n"]

    lines.append(f"• Python: {sys.version.split()[0]}")
    lines.append(f"• 系统: {platform.system()} {platform.release()}")
    lines.append(f"• 工作目录: {config.CURRENT_PROJECT_DIR}")

    claude_ok, claude_ver = _check_claude_cli()
    if claude_ok:
        lines.append(f"• Claude CLI: ✅ {claude_ver}")
    else:
        lines.append(f"• Claude CLI: ❌ {claude_ver}")

    stats = get_task_stats()
    lines.append(f"• 活跃任务: {stats['active']} / {stats['total']}")
    lines.append(f"• 并发上限: {config.CLAUDE_MAX_CONCURRENT_RUNS}")

    sessions = list_sessions()
    lines.append(f"• 活跃会话: {len(sessions)}")

    git_ok, git_ver = _check_git()
    if git_ok:
        lines.append(f"• Git: ✅ {git_ver}")
    else:
        lines.append(f"• Git: ❌ {git_ver}")

    return "\n".join(lines)


def _check_claude_cli() -> tuple:
    """检查 claude CLI 是否可用。"""
    claude_path = shutil.which("claude")
    if not claude_path:
        return False, "未找到 claude 命令"
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        ver = (result.stdout or result.stderr or "").strip().split("\n")[0][:60]
        return True, ver or "(版本未知)"
    except Exception as e:
        return False, f"版本检查失败: {e}"


def _check_git() -> tuple:
    """检查 git 是否可用。"""
    git_path = shutil.which("git")
    if not git_path:
        return False, "未找到 git 命令"
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return True, (result.stdout or "").strip().replace("git version ", "")[:40]
    except Exception:
        return False, "版本检查失败"


def _run_command(args: list, current_dir: str, timeout: int = 30) -> str:
    if not is_path_allowed(current_dir):
        return "当前目录不在允许范围内，无法执行命令。使用 /cd 切换到已允许的目录。"
    try:
        result = subprocess.run(
            args,
            cwd=current_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return f"找不到命令：{args[0]}"
    except subprocess.TimeoutExpired:
        return f"命令超时：{' '.join(args)}"
    except Exception:
        return "执行命令失败，请稍后重试。"

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    body = output or error or "(无输出)"
    if result.returncode != 0:
        body = f"命令异常结束（代码 {result.returncode}）：\n{body}"
    return body[:6000]
