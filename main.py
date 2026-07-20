"""飞书 Claude Bridge - 主入口"""
import logging
import os
import signal
import sys
import threading
import time

try:
    import lark_oapi as lark
except ImportError:
    sys.exit(
        "缺少依赖 lark_oapi，请先安装：\n"
        "  pip install -r requirements.txt\n"
        "或：\n"
        "  pip install 'lark-oapi>=1.6.5,<2.0.0'"
    )

from config import APP_ID, APP_SECRET, validate_config
from handlers import handle_card_action, handle_message
from session import save_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
_shutdown_started = threading.Event()
_shutdown_done = threading.Event()
_ws_client = None  # 模块级引用，供信号处理时关闭连接


def _force_kill():
    """兜底：若 5 秒内 _shutdown() 未完成，强制终止进程。"""
    time.sleep(5)
    if not _shutdown_done.is_set():
        logger.warning("优雅退出超时，强制终止。")
        os._exit(1)


# 预创建 timer 线程，避免在信号处理器中创建线程（不安全）
_force_kill_timer = threading.Thread(target=_force_kill, daemon=True)


def _on_signal(signum, frame):
    if _shutdown_started.is_set():
        return  # 已经在退出流程中
    sig_name = signal.Signals(signum).name
    logger.info(f"收到信号 {sig_name}，开始优雅退出...")
    _shutdown_started.set()
    # 尝试关闭 WebSocket 连接，让 ws_client.start() 尽快返回
    if _ws_client is not None:
        try:
            _ws_client.stop()
        except Exception:
            pass
    # 启动预创建的强制终止 timer（仅 start()，不在信号处理器中创建新线程）
    if not _force_kill_timer.is_alive():
        try:
            _force_kill_timer.start()
        except RuntimeError:
            pass  # 已启动过，忽略


def main():
    global _ws_client
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # 配置验证
    config_warnings = validate_config()
    if config_warnings:
        for w in config_warnings:
            logger.warning(w)
    else:
        logger.info("✅ 配置检查通过。")

    logger.info("启动飞书长连接客户端...")
    logger.info("提示：若卡片点击报错，请确认已订阅事件 `card.action.trigger` 且开启交互式卡片。")

    _ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=(
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(handle_message)
            .register_p2_card_action_trigger(handle_card_action)
            .build()
        ),
        log_level=lark.LogLevel.WARNING,
    )

    _MAX_RECONNECT_DELAY = 300  # 最大重连间隔 5 分钟
    _reconnect_delay = 1
    _reconnect_attempt = 0
    while not _shutdown_started.is_set():
        try:
            _ws_client.start()
            # 正常退出（如收到信号）后重置重连延迟
            _reconnect_delay = 1
            _reconnect_attempt = 0
        except Exception as e:
            _reconnect_attempt += 1
            logger.error(f"WebSocket 连接异常 (第{_reconnect_attempt}次)：{e}")
        if _shutdown_started.is_set():
            break
        logger.info(f"将在 {_reconnect_delay}s 后重连（第{_reconnect_attempt}次断开）...")
        time.sleep(_reconnect_delay)
        _reconnect_delay = min(_reconnect_delay * 2, _MAX_RECONNECT_DELAY)
    _shutdown()


def _shutdown():
    if _shutdown_done.is_set():
        return  # 已执行过退出流程
    _shutdown_started.set()

    logger.info("正在保存会话状态...")
    try:
        save_sessions()
    except Exception as e:
        logger.error(f"保存会话失败: {e}")

    from task_manager import cancel_all_running
    task_ids = cancel_all_running()
    for task_id in task_ids:
        logger.info(f"已取消未结束任务 {task_id}")

    logger.info("退出完成。")
    _shutdown_done.set()
    os._exit(0)


if __name__ == "__main__":
    main()
