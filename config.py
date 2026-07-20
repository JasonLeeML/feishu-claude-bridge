"""配置加载模块"""
import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
HOME_DIR = str(Path.home())


def load_config():
    if not CONFIG_FILE.exists():
        raise RuntimeError("缺少 config.yaml，请在项目根目录创建并填入飞书应用配置。")
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(f"config.yaml 格式错误，请检查 YAML 语法。\n详情：{e}") from e
        return data or {}


config = load_config()

feishu_cfg = config.get("feishu", {})
APP_ID = os.environ.get("FEISHU_APP_ID") or feishu_cfg.get("app_id", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET") or feishu_cfg.get("app_secret", "")
if not APP_ID or not APP_SECRET:
    raise RuntimeError("请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET，或在 config.yaml 中配置 feishu.app_id / feishu.app_secret。")
claude_cfg = config.get("claude", {})
FEISHU_MAX_LENGTH = max(1, feishu_cfg.get("max_message_length", 3500))
FEISHU_SEND_RETRY_TIMES = feishu_cfg.get("send_retry_times", 3)
FEISHU_SEND_RETRY_INTERVAL_SECONDS = feishu_cfg.get("send_retry_interval_seconds", 0.8)

CLAUDE_TIMEOUT = claude_cfg.get("timeout_seconds", 300)
CLAUDE_MAX_TURNS = claude_cfg.get("max_turns", 5)
CLAUDE_OUTPUT_FORMAT = claude_cfg.get("output_format", "text")
CLAUDE_VERBOSE = claude_cfg.get("verbose", False)
CLAUDE_PERMISSION_MODE = claude_cfg.get("permission_mode", "default")
CLAUDE_MAX_CONCURRENT_RUNS = claude_cfg.get("max_concurrent_runs", 4)
CLAUDE_FALLBACK_DIR = claude_cfg.get("fallback_project_dir", HOME_DIR)
MOBILE_SHORT_REPLY_DEFAULT = config.get("mobile", {}).get("short_reply_default", True)
MOBILE_SHORT_REPLY_LINES = config.get("mobile", {}).get("short_reply_lines", 6)
MOBILE_AUTO_STATUS_CARDS = config.get("mobile", {}).get("auto_status_cards", False)
MOBILE_QUICK_ACTIONS_AFTER_REPLY = config.get("mobile", {}).get("quick_actions_after_reply", False)

storage_cfg = config.get("storage", {})
PERSIST_SESSIONS = storage_cfg.get("persist_sessions", False)
PERSIST_TASKS = storage_cfg.get("persist_tasks", False)
PERSIST_PENDING_MESSAGES = storage_cfg.get("persist_pending_messages", False)

START_PROJECT_DIR = (
    os.environ.get("FEISHU_CLAUDE_PROJECT_DIR")
    or CLAUDE_FALLBACK_DIR
)

CURRENT_PROJECT_DIR = str(Path(START_PROJECT_DIR).expanduser().resolve())


def validate_config() -> list:
    """验证配置合理性，返回警告信息列表。"""
    warnings = []

    # 安全：allowed_roots 范围检查
    allowed_roots = config.get("paths", {}).get("allowed_roots", [])
    home_dir = str(Path.home())
    broad_paths = {"/", home_dir, str(Path.home().parent)}
    for root in allowed_roots:
        resolved = str(Path(root).expanduser().resolve())
        if resolved in broad_paths:
            warnings.append(f"⚠ allowed_roots 包含较宽泛路径 '{root}'，建议收敛到具体项目目录以提高安全性。")
            break

    # 安全：allowed_user_ids 为空
    allowed_users = config.get("security", {}).get("allowed_user_ids", [])
    if not allowed_users:
        warnings.append("⚠ allowed_user_ids 为空，所有可触达用户均可调用此机器人。正式使用建议配置白名单。")

    # 飞书凭证（使用已解析的模块级常量，确保环境变量覆盖生效）
    if not APP_ID or APP_ID == "FEISHU_APP_ID":
        warnings.append("⚠ 未配置有效的 FEISHU_APP_ID，请设置环境变量或在 config.yaml 中配置 feishu.app_id。")
    if not APP_SECRET or APP_SECRET == "FEISHU_APP_SECRET":
        warnings.append("⚠ 未配置有效的 FEISHU_APP_SECRET，请设置环境变量或在 config.yaml 中配置 feishu.app_secret。")

    # Claude CLI 超时
    timeout = config.get("claude", {}).get("timeout_seconds", 300)
    if timeout < 30:
        warnings.append(f"⚠ claude.timeout_seconds={timeout} 过短（<30s），可能导致正常任务超时。")
    if timeout > 1800:
        warnings.append(f"⚠ claude.timeout_seconds={timeout} 过长（>30min），长时间无响应可能影响体验。")

    # 输出格式
    output_format = config.get("claude", {}).get("output_format", "text")
    if output_format not in ("text", "json", "stream-json"):
        warnings.append(f"⚠ claude.output_format='{output_format}' 无效，将使用默认值 'text'。")

    # 移动端配置一致性
    mobile = config.get("mobile", {})
    if mobile.get("short_reply_lines", 6) < 1:
        warnings.append("⚠ mobile.short_reply_lines 应 >= 1。")

    return warnings
