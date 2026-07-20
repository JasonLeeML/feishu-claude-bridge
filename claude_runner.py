"""Claude CLI 调用模块"""
import json
import logging
import os
import signal
import subprocess
import threading
import time
from threading import Event

from permission_flow import ask_permission, get_session_permission_mode, set_session_permission_mode
from session import get_or_create_session, rotate_session_id

from config import (
    CLAUDE_MAX_TURNS,
    CLAUDE_OUTPUT_FORMAT,
    CLAUDE_PERMISSION_MODE,
    CLAUDE_TIMEOUT,
    CLAUDE_VERBOSE,
)

logger = logging.getLogger(__name__)

SESSION_IN_USE_HINT = "is already in use"


def run_claude_with_requester(
    prompt: str,
    chat_id: str,
    continue_session: bool = False,
    requester_id: str = None,
    cancel_event: Event = None,
    permission_mode_override: str = None,
) -> str:
    """调用 Claude Code，可携带发起者用于权限确认。"""
    session_info = get_or_create_session(chat_id)
    project_dir = session_info["current_dir"]
    timeout_seconds = CLAUDE_TIMEOUT
    max_turns = str(CLAUDE_MAX_TURNS)
    output_format = (CLAUDE_OUTPUT_FORMAT or "text").strip().lower()
    if output_format not in {"text", "json", "stream-json"}:
        output_format = "text"
    session_permission_mode = get_session_permission_mode(chat_id)
    base_permission_mode = permission_mode_override or session_permission_mode or CLAUDE_PERMISSION_MODE

    first_session_id = session_info["session_id"]
    first_output, first_error = _run_claude_once(
        prompt=prompt,
        session_id=first_session_id,
        project_dir=project_dir,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
        output_format=output_format,
        continue_session=continue_session,
        permission_mode=base_permission_mode,
        cancel_event=cancel_event,
    )
    if not first_error:
        return first_output

    if _is_permission_error(first_error):
        choice = ask_permission(chat_id, requester_id, prompt)
        if choice == "allow_once":
            approve_mode = "bypassPermissions"
        elif choice == "allow_session":
            approve_mode = "bypassPermissions"
            set_session_permission_mode(chat_id, "bypassPermissions")
        elif choice == "timeout":
            return "权限确认超时（2 分钟未选择），已取消本次执行。可重新发送相同内容重试。"
        elif choice == "send_failed":
            return "权限确认卡片发送失败，已取消本次执行。请稍后重试，或检查飞书应用卡片权限配置。"
        else:
            return "已拒绝本次执行。"

        # 权限通过后，换新 session 重试，避免同一 session 在权限错误后状态异常
        new_session_id = rotate_session_id(chat_id)
        second_output, second_error = _run_claude_once(
            prompt=prompt,
            session_id=new_session_id,
            project_dir=project_dir,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            output_format=output_format,
            continue_session=False,
            permission_mode=approve_mode,
            cancel_event=cancel_event,
        )
        if second_error:
            return "权限确认后执行仍失败：\n" + second_error
        return second_output

    # 兜底：若 session_id 被占用，自动轮换 session 并重试一次。
    if SESSION_IN_USE_HINT not in first_error:
        return (
            f"Claude Code 执行遇到问题：\n{first_error}\n\n"
            "建议：发送 /health 检查状态，或稍后重试。"
        )
    if continue_session:
        return (
            "当前会话仍在被占用，无法继续历史上下文。\n"
            "请稍后重试，或发送 /clear 清除会话后开启新轮次。"
        )

    new_session_id = rotate_session_id(chat_id)
    retry_output, retry_error = _run_claude_once(
        prompt=prompt,
        session_id=new_session_id,
        project_dir=project_dir,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
        output_format=output_format,
        continue_session=False,
        permission_mode=base_permission_mode,
        cancel_event=cancel_event,
    )
    if retry_error:
        return (
            "Claude Code 执行失败（已自动重试一次）：\n"
            f"首次错误：{first_error}\n"
            f"重试错误：{retry_error}"
        )
    return retry_output


def _run_claude_once(
    prompt: str,
    session_id: str,
    project_dir: str,
    timeout_seconds: int,
    max_turns: str,
    output_format: str,
    continue_session: bool,
    permission_mode: str,
    cancel_event: Event = None,
) -> tuple[str, str]:
    """执行一次 Claude CLI。返回 (output, error)，二者必有其一。"""
    if continue_session:
        cmd = ["claude", "--resume", session_id, "-p", prompt]
    else:
        cmd = ["claude", "--session-id", session_id, "-p", prompt]
    cmd += ["--max-turns", max_turns, "--output-format", output_format]

    if permission_mode and permission_mode != "default":
        cmd += ["--permission-mode", permission_mode]

    if CLAUDE_VERBOSE:
        cmd.append("--verbose")

    try:
        process = subprocess.Popen(
            cmd,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return "", (
            "找不到 claude 命令。请确认已安装 Claude Code：\n"
            "• 运行 `claude --version` 检查是否已安装\n"
            "• 如未安装，请参考 https://docs.anthropic.com/en/docs/claude-code/overview\n"
            "• 如在 conda 环境中，请确认已激活正确的环境"
        )
    except Exception as e:
        return "", f"调用 Claude Code 出错：{e}\n请检查 claude 命令是否可正常执行（运行 claude --version 测试）。"

    # 用单独线程收集输出，主线程短间隔等待完成/取消/超时。
    # 不能直接用 cancel_event.wait(timeout=remaining)，否则正常完成也会等到超时才返回。
    done_event = threading.Event()
    result_holder = [{"stdout": "", "stderr": ""}]

    def _wait_for_process():
        try:
            result_holder[0]["stdout"], result_holder[0]["stderr"] = process.communicate()
        except Exception:
            pass
        done_event.set()

    wait_thread = threading.Thread(target=_wait_for_process, daemon=True)
    wait_thread.start()

    deadline = time.monotonic() + timeout_seconds
    while True:
        if done_event.is_set():
            break

        if cancel_event and cancel_event.is_set():
            _stop_process(process, done_event)
            return "", "任务已取消。"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process, done_event)
            return "", (
                f"Claude Code 执行超时（{timeout_seconds}s）。\n"
                "建议：简化问题、减少 max_turns，或在 config.yaml 中增大 claude.timeout_seconds。"
            )

        if done_event.wait(timeout=min(0.2, remaining)):
            break

    stdout_data = result_holder[0]["stdout"] or ""
    stderr_data = result_holder[0]["stderr"] or ""

    if process.returncode != 0:
        stdout_preview = stdout_data.strip()
        stderr_preview = stderr_data.strip()
        details = stderr_preview or stdout_preview or "未知错误"
        return "", "Claude Code 执行失败：\n" + details[:1200]

    if output_format == "text":
        full_output = stdout_data.strip()
        return full_output or "Claude 没有返回内容。", ""

    if output_format == "json":
        text = stdout_data.strip()
        if not text:
            return "Claude 没有返回内容。", ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return (
                    str(data.get("result") or data.get("content") or data.get("text") or text)
                ), ""
            return str(data), ""
        except Exception:
            return text, ""

    # 解析 stream-json 输出
    full_output = ""
    diagnostics = []
    for line in stdout_data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            msg_type = data.get("type")
            if msg_type == "text":
                full_output += data.get("content", "")
            elif msg_type == "assistant":
                message = data.get("message", {})
                content = message.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            full_output += item.get("text", "")
            elif msg_type in ("result", "done", "complete"):
                result = data.get("result") or data.get("content", "")
                if result:
                    full_output = result
            elif msg_type == "system":
                subtype = data.get("subtype", "")
                if subtype in ("api_retry", "error", "warning"):
                    diagnostics.append(line)
        except json.JSONDecodeError:
            full_output += line

    if not full_output:
        if diagnostics:
            return "Claude 未产出正文，诊断信息：\n" + "\n".join(diagnostics[-3:]), ""
        if stderr_data:
            return f"Claude 没有返回内容。技术详情：{stderr_data[:200]}", ""
        return "Claude 没有返回内容。", ""

    return full_output, ""


def _stop_process(process: subprocess.Popen, done_event: Event) -> None:
    """终止子进程，并给输出收集线程一点时间收尾。"""
    try:
        if process.poll() is None:
            if os.name == "nt":
                process.kill()
            elif process.pid is not None:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    done_event.wait(timeout=2)


def _is_permission_error(error_text: str) -> bool:
    s = (error_text or "").lower()
    keys = [
        "permission",
        "requires approval",
        "approval",
        "denied",
        "not allowed",
    ]
    return any(k in s for k in keys)
