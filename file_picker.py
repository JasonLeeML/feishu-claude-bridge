"""飞书卡片文件浏览器（@ 触发）。"""
from pathlib import Path
from typing import List, Optional, Tuple

from client import card_button, send_interactive_card
from utils import is_path_allowed

PAGE_SIZE = 9
MAX_LABEL_LEN = 28
MAX_SIZE_LABEL_LEN = 22  # 文件名在含大小标签时的截断长度


def open_file_picker(
    chat_id: str,
    base_dir: str,
    rel_path: str = ".",
    offset: int = 0,
    mode: str = "read",
) -> Tuple[bool, str]:
    """发送指定目录的文件浏览卡片。"""
    card, err = build_file_picker_card(chat_id, base_dir, rel_path=rel_path, offset=offset, mode=mode)
    if err:
        return False, err
    ok = send_interactive_card(chat_id, card)
    if not ok:
        return False, "发送文件选择卡片失败，请稍后重试。"
    return True, ""


def build_file_picker_card(
    chat_id: str,
    base_dir: str,
    rel_path: str = ".",
    offset: int = 0,
    mode: str = "read",
) -> Tuple[Optional[dict], str]:
    """构建文件浏览卡片。返回 (card_dict, error_msg)，失败时 card 为 None。"""
    base = Path(base_dir).resolve()
    target, rel, err = _resolve_dir(base, rel_path)
    if err:
        return None, err

    if mode == "cd":
        entries = _list_entries(target, dir_only=True)
    else:
        entries = _list_entries(target, dir_only=False)
    total = len(entries)
    offset = max(0, min(offset, max(total - 1, 0)))
    page_entries = entries[offset: offset + PAGE_SIZE]

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"当前目录：`{rel}`\n"
                    + _mode_hint(mode)
                ),
            },
        }
    ]

    nav_buttons = []
    if rel != ".":
        parent_rel = str(Path(rel).parent)
        nav_buttons.append(
            card_button(
                "⬆️ 上一级",
                {"kind": "file_picker", "mode": mode, "action": "open", "path": parent_rel, "offset": 0},
            )
        )
    nav_buttons.append(
        card_button("刷新", {"kind": "file_picker", "mode": mode, "action": "open", "path": rel, "offset": offset})
    )
    elements.append({"tag": "action", "actions": nav_buttons})
    if mode == "cd":
        elements.append(
            {
                "tag": "action",
                "actions": [
                    card_button(
                        "✅ 选择当前目录",
                        {"kind": "file_picker", "mode": mode, "action": "choose_dir", "path": rel},
                    )
                ],
            }
        )
    else:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    card_button(
                        "使用当前目录",
                        {"kind": "file_picker", "mode": mode, "action": "use_path", "path": rel},
                    )
                ],
            }
        )

    if not page_entries:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "此目录为空。"}})
    else:
        actions = []
        for name, is_dir, size_label in page_entries:
            child_rel = str((Path(rel) / name).as_posix()) if rel != "." else name
            if is_dir:
                label = f"📁 {_clip(name)}"
            else:
                display_name = _clip(name, MAX_SIZE_LABEL_LEN)
                label = f"📄 {display_name}  {size_label}" if size_label else f"📄 {_clip(name)}"
            actions.append(
                card_button(
                    label,
                    {
                        "kind": "file_picker",
                        "mode": mode,
                        "action": "open" if is_dir else "use_path",
                        "path": child_rel,
                        "offset": 0,
                    },
                )
            )
            if len(actions) == 3:
                elements.append({"tag": "action", "actions": actions})
                actions = []
        if actions:
            elements.append({"tag": "action", "actions": actions})

    page_actions = []
    if offset > 0:
        page_actions.append(
            card_button(
                "上一页",
                {
                    "kind": "file_picker",
                    "mode": mode,
                    "action": "open",
                    "path": rel,
                    "offset": max(0, offset - PAGE_SIZE),
                },
            )
        )
    if offset + PAGE_SIZE < total:
        page_actions.append(
            card_button(
                "下一页",
                {
                    "kind": "file_picker",
                    "mode": mode,
                    "action": "open",
                    "path": rel,
                    "offset": offset + PAGE_SIZE,
                },
            )
        )
    if page_actions:
        elements.append({"tag": "action", "actions": page_actions})

    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"总条目：{total}，显示：{offset + 1 if total else 0}-{min(offset + PAGE_SIZE, total)}"}
            ],
        }
    )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {
                "tag": "plain_text",
                "content": "选择要阅读的文件" if mode == "read" else "选择要切换的目录",
            },
        },
        "elements": elements,
    }
    return card, ""


def resolve_selected_path(base_dir: str, rel_path: str) -> Tuple[bool, str, bool]:
    """校验并返回被选中文件/目录的相对路径与是否目录。"""
    base = Path(base_dir).resolve()
    target = (base / rel_path).resolve() if rel_path not in ("", ".") else base
    if not is_path_allowed(str(target)):
        return False, "该路径不在允许范围内。", False
    if not target.exists():
        return False, "所选路径不存在。", False
    if not target.is_file() and not target.is_dir():
        return False, "所选路径不是文件或目录。", False
    try:
        rel = target.relative_to(base).as_posix() if target != base else "."
    except Exception:
        return False, "路径不在当前会话目录内。", False
    return True, rel, target.is_dir()


def resolve_selected_dir(base_dir: str, rel_path: str) -> Tuple[bool, str]:
    """校验并返回被选中的目录绝对路径。"""
    base = Path(base_dir).resolve()
    target = (base / rel_path).resolve() if rel_path not in ("", ".") else base
    if not is_path_allowed(str(target)):
        return False, "该目录不在允许范围内。"
    if not target.exists() or not target.is_dir():
        return False, "所选目录不存在，或不是目录。"
    return True, str(target)


def _resolve_dir(base: Path, rel_path: str) -> Tuple[Path, str, str]:
    rel = (rel_path or ".").strip()
    if rel in ("", "/"):
        rel = "."
    target = (base / rel).resolve() if rel != "." else base

    if not is_path_allowed(str(target)):
        return target, rel, "该目录不在允许范围内。"
    if not target.exists():
        return target, rel, "目录不存在。"
    if not target.is_dir():
        return target, rel, "目标不是目录。"
    try:
        rel_out = target.relative_to(base).as_posix() if target != base else "."
    except Exception:
        return target, rel, "目录不在当前会话目录内。"
    return target, rel_out, ""


def _list_entries(directory: Path, dir_only: bool = False) -> List[Tuple[str, bool, str]]:
    """列出目录条目，返回 (名称, 是否目录, 大小标签)。"""
    dirs = []
    files = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return [("(无权限访问此目录)", False, "")]
    except OSError:
        return [("(无法读取此目录)", False, "")]
    for item in entries:
        name = item.name
        if name.startswith("."):
            continue
        try:
            is_dir = item.is_dir()
        except OSError:
            continue  # 符号链接断开等
        if is_dir:
            dirs.append((name, True, ""))
        elif item.is_file() and not dir_only:
            try:
                size_label = _format_size(item.stat().st_size)
            except OSError:
                size_label = "?"  # 断开的符号链接或无权限文件
            files.append((name, False, size_label))
    dirs.sort(key=lambda x: x[0].lower())
    files.sort(key=lambda x: x[0].lower())
    return dirs + files


def _format_size(size_bytes: int) -> str:
    """将字节数转为可读大小标签。"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}K"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}M"


def _mode_hint(mode: str) -> str:
    if mode == "cd":
        return "点击文件夹继续展开；点击“选择当前目录”完成切换。"
    return "点击文件夹继续展开；点击文件或“使用当前目录”让 Claude 先预读取。"


def _clip(text: str, max_len: int = MAX_LABEL_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
