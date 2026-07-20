"""飞书客户端模块"""
import json
import logging
import re
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from config import (
    APP_ID,
    APP_SECRET,
    BASE_DIR,
    FEISHU_MAX_LENGTH,
    FEISHU_SEND_RETRY_INTERVAL_SECONDS,
    FEISHU_SEND_RETRY_TIMES,
    PERSIST_PENDING_MESSAGES,
)
from utils import STATUS_LABEL_MAP

logger = logging.getLogger(__name__)

# 普通 OpenAPI client：用于发送消息
client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.WARNING)
    .build()
)
_pending_file = BASE_DIR / "pending_messages.json"
_pending_lock = threading.Lock()
_last_flush_time: float = time.time()
_FLUSH_INTERVAL_SECONDS = 30

_KNOWN_LANG_TAGS = frozenset({
    "python", "py", "javascript", "js", "typescript", "ts", "java", "go", "rust", "rs",
    "c", "cpp", "c++", "csharp", "cs", "ruby", "rb", "php", "swift", "kotlin", "kt",
    "scala", "r", "sql", "bash", "sh", "shell", "zsh", "powershell", "ps1",
    "html", "css", "scss", "sass", "less", "xml", "json", "yaml", "yml", "toml",
    "markdown", "md", "dockerfile", "makefile", "cmake", "ini", "cfg", "conf",
    "diff", "patch", "graphql", "proto", "text", "plaintext",
    "vim", "vimscript", "lua", "perl", "haskell", "hs", "elm", "elixir", "exs",
    "clojure", "clj", "dart", "matlab", "octave", "fortran", "cobol",
})


def _is_language_tag(line: str) -> bool:
    """判断代码块首行是否为已知编程语言标记。"""
    return line.strip().lower() in _KNOWN_LANG_TAGS


def _sanitize_text(text: str) -> str:
    """移除飞书可能拒绝的控制字符。"""
    if not text:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _chunk_text(text: str, max_len: int) -> list:
    """将文本按最大长度切分，尽量在换行处分段。"""
    if max_len < 1:
        max_len = 1
    if len(text) <= max_len:
        return [text]

    chunks = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        split_at = rest.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(rest[:split_at].rstrip("\n"))
        rest = rest[split_at:].lstrip("\n")
    return [c if c else " " for c in chunks]


def _send_text_once(chat_id: str, text: str) -> bool:
    """发送单条文本消息。"""
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.im.v1.message.create(request)
    if not response.success():
        logger.warning(f"[send_text] 发送失败 chat_id={chat_id}, code={response.code}, msg={response.msg}")
    return response.success()


def _send_post_once(chat_id: str, content: list) -> bool:
    """发送单条 post（富文本）消息。content 是飞书 post 段落数组。"""
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("post")
            .content(json.dumps({"zh_cn": {"title": "", "content": content}}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.im.v1.message.create(request)
    if not response.success():
        logger.warning(f"[send_post] 发送失败 chat_id={chat_id}, code={response.code}, msg={response.msg}")
    return response.success()


def _markdown_to_post_content(text: str) -> list:
    """
    将 Markdown 文本转换为飞书 post 消息 content。
    支持：```code block```、`inline code`、**bold**、*italic*、> quote、- list
    """
    text = _preprocess_markdown_lines(text)
    segments = []
    # 先按代码块分割
    parts = text.split("```")
    in_code = False
    for part in parts:
        if in_code:
            # 代码块：整个作为一行等宽文本
            lines = part.strip("\n").splitlines()
            if len(lines) > 1 and _is_language_tag(lines[0].strip()):
                lines = lines[1:]
            for line in lines:
                if line:
                    segments.append({"tag": "text", "text": line, "text_style": {"font_style": "code"}})
                segments.append({"tag": "text", "text": "\n"})
        else:
            # 普通文本，继续处理行内格式
            segments.extend(_process_inline_segment(part))
        in_code = not in_code

    # 合并连续的纯文本 segment
    merged = []
    for seg in segments:
        if (
            seg.get("tag") == "text"
            and merged
            and merged[-1].get("tag") == "text"
            and not seg.get("text_style")
            and not merged[-1].get("text_style")
        ):
            merged[-1]["text"] += seg["text"]
        else:
            merged.append(seg)
    return _segments_to_post_content(merged)


def _segments_to_post_content(segments: list) -> list:
    """将带换行的 segment 列表转换成飞书 post 需要的二维段落数组。"""
    content = [[]]
    for seg in segments:
        text = str(seg.get("text", ""))
        if not text:
            continue

        parts = text.split("\n")
        for idx, part in enumerate(parts):
            if part:
                item = dict(seg)
                item["text"] = part
                content[-1].append(item)
            if idx < len(parts) - 1:
                content.append([])

    normalized = [line for line in content if line]
    return normalized or [[{"tag": "text", "text": " "}]]


def _process_inline_segment(text: str) -> list:
    """处理一段普通文本（无代码块）的行内格式。"""
    result = []
    # 行内代码 `...`
    parts = text.split("`")
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append({"tag": "text", "text": part, "text_style": {"font_style": "code"}})
        else:
            result.extend(_process_bold_italic(part))
    return result


# 匹配 Markdown 链接 [text](url) 和图片 ![alt](url)
_LINK_RE = re.compile(r'(?<!\!)\[(.+?)\]\((\S+?)\)')
_IMAGE_RE = re.compile(r'!\[(.+?)\]\((\S+?)\)')


def _process_links_and_images(text: str) -> list:
    """将 Markdown [text](url) 和 ![alt](url) 转为飞书 post 的 <a>/<img> 标签。"""
    result = []
    # 先处理图片（避免 ![...](url) 被链接规则误匹配）
    pos = 0
    for m in _IMAGE_RE.finditer(text):
        if m.start() > pos:
            result.append({"tag": "text", "text": text[pos:m.start()]})
        alt = m.group(1) or "图片"
        url = m.group(2)
        result.append({"tag": "text", "text": f"🖼 {alt}", "text_style": {"underline": True}})
        pos = m.end()
    rest = text[pos:]
    # 再处理链接
    pos = 0
    for m in _LINK_RE.finditer(rest):
        if m.start() > pos:
            result.append({"tag": "text", "text": rest[pos:m.start()]})
        label = m.group(1)
        url = m.group(2)
        # 飞书 post 支持 <a> 标签
        result.append({"tag": "a", "text": label, "href": url})
        pos = m.end()
    if pos < len(rest):
        result.append({"tag": "text", "text": rest[pos:]})
    return result if result else [{"tag": "text", "text": text}]


def _process_bold_italic(text: str) -> list:
    """处理粗体 **text** 和斜体 *text*，并在最后处理链接/图片。"""
    result = []
    # 粗体 **text**（先处理，避免与斜体混淆）
    while True:
        m = re.match(r'^(.*?)\*\*(.+?)\*\*(.*)$', text)
        if not m:
            break
        pre, bold_text, rest = m.groups()
        if pre:
            result.extend(_process_italic(pre))
        result.append({"tag": "text", "text": bold_text, "text_style": {"bold": True}})
        text = rest
    if text:
        result.extend(_process_italic(text))
    # 在每个文本段上应用链接/图片转换（含带样式文本，如 **[链接](url)**）
    out = []
    for seg in result:
        if seg.get("tag") == "text":
            converted = _process_links_and_images(seg["text"])
            if len(converted) == 1 and converted[0].get("tag") == "text" and converted[0]["text"] == seg["text"]:
                # 无链接/图片，保留原 segment（含其样式）
                out.append(seg)
            else:
                # 有链接/图片，展开为多个 segment（丢弃原样式，飞书 post 不支持嵌套样式）
                out.extend(converted)
        else:
            out.append(seg)
    return out


def _process_italic(text: str) -> list:
    """处理斜体 *text*（非 **）。"""
    result = []
    while True:
        m = re.match(r'^(.*?)(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)(.*)$', text)
        if not m:
            break
        pre, italic_text, rest = m.groups()
        if pre:
            result.append({"tag": "text", "text": pre})
        result.append({"tag": "text", "text": italic_text, "text_style": {"underline": True}})
        text = rest
    if text:
        result.append({"tag": "text", "text": text})
    return result


# ── 表格处理 ────────────────────────────────────────────
_TABLE_SEP_RE = re.compile(r'^\s*\|?[\s:-]+\|[\s|:-]+\s*$')


def _is_table_separator(line: str) -> bool:
    """判断是否为 Markdown 表格分隔行（如 |---|---|）。"""
    return bool(_TABLE_SEP_RE.match(line))


def _parse_table_cells(line: str) -> list:
    """解析单行表格单元格。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _format_table(cells: list, col_widths: list) -> str:
    """将单元格列表格式化为等宽对齐的文本表格。"""
    parts = []
    for i, cell in enumerate(cells):
        w = col_widths[i] if i < len(col_widths) else max(len(cell), 3)
        parts.append(cell.ljust(w))
    return "│ " + " │ ".join(parts) + " │"


def _preprocess_markdown_lines(text: str) -> str:
    """按行转换 Markdown 标题、引用、列表、表格，兼容后续行内格式解析。"""
    lines = text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检测表格：当前行含 | 且下一行是分隔符
        if "|" in line and i + 2 < len(lines) and _is_table_separator(lines[i + 1]):
            # 收集表头 + 分隔符 + 数据行
            header_cells = _parse_table_cells(line)
            data_rows = []
            j = i + 2
            while j < len(lines) and "|" in lines[j]:
                data_rows.append(_parse_table_cells(lines[j]))
                j += 1
            # 计算列宽
            all_cells = [header_cells] + data_rows
            col_count = max(len(row) for row in all_cells)
            col_widths = [0] * col_count
            for row in all_cells:
                for ci, cell in enumerate(row):
                    col_widths[ci] = max(col_widths[ci], len(cell))
            col_widths = [max(w, 3) for w in col_widths]
            # 输出
            result.append(_format_table(header_cells, col_widths))
            result.append(_format_table(["─" * w for w in col_widths], col_widths))
            for row in data_rows:
                result.append(_format_table(row, col_widths))
            i = j
            continue

        # 标题：# / ## / ###
        m = re.match(r'^(#{1,3})\s+(.+)$', line)
        if m:
            content = m.group(2).replace('*', '\\*')
            result.append(f"**{content}**")
            i += 1
            continue
        # 引用块 > text
        m = re.match(r'^>\s*(.+)$', line)
        if m:
            result.append(f"┃ {m.group(1)}")
            i += 1
            continue
        # 无序列表项
        m = re.match(r'^(\s*)[-*]\s+(.+)$', line)
        if m:
            result.append(f"• {m.group(2)}")
            i += 1
            continue
        # 有序列表项：保持原样
        m = re.match(r'^(\s*\d+[.)]\s*)(.+)$', line)
        if m:
            result.append(f"{m.group(1)}{m.group(2)}")
            i += 1
            continue
        result.append(line)
        i += 1
    return "\n".join(result)


def send_interactive_card(chat_id: str, card: dict) -> bool:
    """发送交互卡片消息。"""
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.im.v1.message.create(request)
    if not response.success():
        logger.warning(f"[send_card] 发送失败 chat_id={chat_id}, code={response.code}, msg={response.msg}")
    return response.success()


def _build_permission_actions(request_id: str, kind: str = "") -> list:
    """构造权限三选一按钮组。kind 为空时用于同步权限卡，非空时用于延迟权限卡。"""
    value_base = {"kind": kind, "request_id": request_id} if kind else {"request_id": request_id}
    return [
        {
            "tag": "action",
            "actions": [
                card_button("允许一次", {**value_base, "action": "allow_once"}, button_type="primary"),
                card_button("完全授权（本会话）", {**value_base, "action": "allow_session"}),
                card_button("拒绝", {**value_base, "action": "deny"}, button_type="danger"),
            ],
        },
    ]


def _format_permission_preview(text: str, max_len: int = 220) -> str:
    """截断并清理权限卡预览文本。"""
    preview = _sanitize_text(text or "").strip()
    if len(preview) > max_len:
        preview = preview[:max_len] + "..."
    return preview or "(无预览)"


def send_permission_card(chat_id: str, request_id: str, prompt_preview: str) -> bool:
    """发送同步权限确认三选一卡片（阻塞等待用户点击）。"""
    preview = _format_permission_preview(prompt_preview)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "Claude 权限确认"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"Claude 执行当前请求需要额外权限，请选择：\n> {preview}",
                },
            },
            *_build_permission_actions(request_id),
        ],
    }
    return send_interactive_card(chat_id, card)


def send_deferred_permission_card(chat_id: str, request_id: str, prompt_preview: str, extra_hint: str = "") -> bool:
    """发送非阻塞权限确认卡。点击后由 handlers 继续上一轮任务。"""
    preview = _format_permission_preview(prompt_preview)
    hint_line = f"{extra_hint}\n" if extra_hint else ""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "Claude 权限确认"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"{hint_line}Claude 请求继续执行需要额外权限，请选择：\n> {preview}",
                },
            },
            *_build_permission_actions(request_id, kind="deferred_permission"),
        ],
    }
    return send_interactive_card(chat_id, card)


def send_quick_actions_card(chat_id: str, task_id: str) -> bool:
    """发送移动端快捷操作按钮。"""
    def _qa_value(action: str) -> dict:
        return {"kind": "quick_action", "action": action, "task_id": task_id}
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"任务 {task_id} 快捷操作"},
        },
        "elements": [
            {
                "tag": "action",
                "actions": [
                    card_button("继续追问", _qa_value("continue"), button_type="primary"),
                    card_button("运行测试", _qa_value("tests")),
                    card_button("修复报错", _qa_value("fix_error")),
                ],
            },
            {
                "tag": "action",
                "actions": [
                    card_button("解释改动", _qa_value("explain_changes")),
                    card_button("文件列表", _qa_value("files")),
                    card_button("提交信息", _qa_value("commit_msg")),
                ],
            },
            {
                "tag": "action",
                "actions": [
                    card_button("总结3点", _qa_value("summary")),
                    card_button("只看命令", _qa_value("commands")),
                ],
            },
        ],
    }
    return send_interactive_card(chat_id, card)


def send_git_panel_card(chat_id: str) -> bool:
    """发送 Git 快捷面板。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "purple",
            "title": {"tag": "plain_text", "content": "Git 快捷面板"},
        },
        "elements": [
            {
                "tag": "action",
                "actions": [
                    card_button("状态", {"kind": "git_action", "action": "status"}),
                    card_button("Diff", {"kind": "git_action", "action": "diff"}),
                    card_button("分支/日志", {"kind": "git_action", "action": "log"}),
                ],
            },
            {
                "tag": "action",
                "actions": [
                    card_button("运行测试", {"kind": "git_action", "action": "test"}),
                    card_button("提交信息", {"kind": "git_action", "action": "commit_msg"}),
                    card_button("变更文件", {"kind": "git_action", "action": "files"}),
                ],
            },
        ],
    }
    return send_interactive_card(chat_id, card)


def send_task_status_card(chat_id: str, tasks: list) -> bool:
    """发送最近任务状态卡片（含取消/重试）。"""
    elements = []
    if not tasks:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "当前没有任务记录。"}})
    else:
        for t in tasks:
            status_text = STATUS_LABEL_MAP.get(t.status, t.status)
            preview = _sanitize_text((t.result_preview or t.error or t.text or "").strip())
            if len(preview) > 80:
                preview = preview[:80] + "..."
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{t.task_id}** · {status_text}\n{preview or '(无预览)'}",
                    },
                }
            )
            actions = [
                card_button("详情", {"kind": "task_action", "action": "detail", "task_id": t.task_id}),
            ]
            if t.status in {"queued", "running"}:
                actions.append(
                    card_button("取消", {"kind": "task_action", "action": "cancel", "task_id": t.task_id}, button_type="danger"),
                )
            if t.status in {"success", "error", "canceled"}:
                actions.append(
                    card_button("重试", {"kind": "task_action", "action": "retry", "task_id": t.task_id}),
                )
            elements.append({"tag": "action", "actions": actions})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "cyan",
            "title": {"tag": "plain_text", "content": "最近任务"},
        },
        "elements": elements,
    }
    return send_interactive_card(chat_id, card)


def card_button(label: str, value: dict, button_type: str = None) -> dict:
    """构造飞书卡片按钮 element，供 client / file_picker 等模块复用。"""
    button = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "value": value,
    }
    if button_type:
        button["type"] = button_type
    return button


def send_delete_auth_card(chat_id: str, request_id: str) -> bool:
    """发送删除授权确认卡片。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "删除授权确认"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "确定要删除本会话的授权吗？\n删除后，写入操作需要重新授权。",
                },
            },
            {
                "tag": "action",
                "actions": [
                    card_button("确认删除", {"kind": "delete_auth", "action": "confirm", "request_id": request_id}, button_type="primary"),
                    card_button("取消", {"kind": "delete_auth", "action": "cancel", "request_id": request_id}),
                ],
            },
        ],
    }
    return send_interactive_card(chat_id, card)


def send_thinking_indicator(chat_id: str, task_id: str = "") -> None:
    """发送"正在思考"提示，用户感知机器人已收到消息。"""
    tail = f"（{task_id}）" if task_id else ""
    send_text(chat_id, f"正在处理...{tail}")


def send_progress_update(chat_id: str, elapsed_seconds: int, task_id: str = "") -> None:
    """发送长时间任务的进度提示，每隔一定时间调用一次。
    只有 elapsed_seconds > 30 时才发送，避免对短任务造成噪音。"""
    if elapsed_seconds < 30:
        return
    minutes = elapsed_seconds // 60
    secs = elapsed_seconds % 60
    if minutes > 0:
        time_str = f"{minutes}分{secs}秒"
    else:
        time_str = f"{secs}秒"
    tail = f"（{task_id}）" if task_id else ""
    send_text(chat_id, f"仍在处理中... 已等待 {time_str}{tail}")


def _load_pending_messages() -> list:
    if not PERSIST_PENDING_MESSAGES:
        return []
    if not _pending_file.exists():
        return []
    try:
        data = json.loads(_pending_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_pending_messages(messages: list) -> None:
    if not PERSIST_PENDING_MESSAGES:
        return
    try:
        _pending_file.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"[send_text] 保存待发消息失败: {e}")


def _enqueue_pending_message(chat_id: str, text: str) -> None:
    if not PERSIST_PENDING_MESSAGES:
        logger.warning("[send_text] 发送失败且未启用本地待发持久化，消息不会落盘。")
        return
    with _pending_lock:
        pending = _load_pending_messages()
        pending.append({"chat_id": chat_id, "text": text, "ts": int(time.time())})
        _save_pending_messages(pending)


def _flush_pending_messages(max_flush: int = 10) -> None:
    if not PERSIST_PENDING_MESSAGES:
        return
    with _pending_lock:
        pending = _load_pending_messages()
        if not pending:
            return
        # 清空队列，避免和新入队消息混淆
        _save_pending_messages([])

    # 锁外执行网络 I/O，避免阻塞其他发送线程
    remain = []
    flushed = 0
    for item in pending:
        if flushed >= max_flush:
            remain.append(item)
            continue
        cid = item.get("chat_id", "")
        txt = item.get("text", "")
        if cid and txt and _send_text_once(cid, txt):
            flushed += 1
        else:
            remain.append(item)

    # 合并：未发送的 + 发送期间新入队的
    with _pending_lock:
        newer = _load_pending_messages()
        _save_pending_messages(remain + newer)


def send_text(chat_id: str, text: str) -> bool:
    """给指定 chat_id 发送文本消息"""
    text = _sanitize_text(text)
    if not text:
        text = "没有可发送的内容。"

    # 定时批量 flush，避免每次都扫描磁盘
    global _last_flush_time
    with _pending_lock:
        now = time.time()
        should_flush = now - _last_flush_time >= _FLUSH_INTERVAL_SECONDS
        if should_flush:
            _last_flush_time = now
    if should_flush:
        _flush_pending_messages()

    chunks = _chunk_text(text, FEISHU_MAX_LENGTH)
    ok = True
    for chunk in chunks:
        sent = False
        for attempt in range(FEISHU_SEND_RETRY_TIMES):
            if _send_text_once(chat_id, chunk):
                sent = True
                break
            if attempt < FEISHU_SEND_RETRY_TIMES - 1:
                time.sleep(FEISHU_SEND_RETRY_INTERVAL_SECONDS * (attempt + 1))
        if not sent:
            _enqueue_pending_message(chat_id, chunk)
            ok = False
    return ok


def send_rich_text(chat_id: str, text: str) -> bool:
    """
    发送带 Markdown 格式的富文本消息（post 类型）。
    自动降级为纯文本（send_text）如果 post 发送失败。
    """
    if not text or not text.strip():
        return send_text(chat_id, "没有可发送的内容。")

    # 将 Markdown 转换为 post content
    content = _markdown_to_post_content(text)

    # 按 max_length 分段（post 每个字符都算 Unicode 长度）
    total = len(json.dumps({"zh_cn": {"title": "", "content": content}}, ensure_ascii=False))
    if total <= FEISHU_MAX_LENGTH:
        ok = _send_post_once(chat_id, content)
        if ok:
            return True
        # 降级：fall through to send_text

    # 降级到纯文本
    return send_text(chat_id, text)
