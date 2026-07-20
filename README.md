# Feishu Claude Bridge

> 一个面向移动端远程开发场景的 AI 编码代理桥接系统。
> 通过飞书机器人 WebSocket 长连接连接本机 Claude Code CLI，让你可以在手机端发起需求、选择项目上下文、确认高危权限、执行编码任务，并接收最终结果。

## 项目简介

**Feishu Claude Bridge** 将飞书作为移动端交互入口，将本机 Claude Code 作为真正的代码执行环境，构建了一套适合远程编码的轻量桥接系统。

项目的核心思路是：

- **飞书负责交互**：消息入口、交互式卡片、权限确认、结果展示。
- **Claude Code 负责执行**：代码阅读、文件修改、命令执行、测试运行。
- **Bridge 负责调度**：会话隔离、上下文绑定、权限控制、任务管理、路径安全校验。

因此，它不是一个新的在线 IDE，也不是一个云端代码执行平台，而是把飞书手机版包装成一个可控、低噪音、可远程操作的 Claude Code 控制入口。

## 解决的问题

在移动端远程使用 Claude Code 时，常见痛点包括：

- 项目上下文难以准确选择；
- 写文件、删文件、运行命令等高危操作缺少确认机制；
- 长任务执行状态不清晰；
- 飞书卡片和消息容易造成噪音；
- 本地中转层不应长期保存敏感会话数据。

Feishu Claude Bridge 针对这些问题提供了：

- `@` 文件 / 目录选择与预读取；
- 会话级上下文绑定；
- 写入与命令执行前的权限三选一卡片；
- 任务状态管理、取消与重试机制；
- **长任务进度通知**：超过 35 秒自动推送等待时长，后续每 45 秒更新；
- **启动配置验证**：自动检测凭证有效性、路径安全性、超时合理性；
- 默认无持久化的中转站模式；
- 面向手机端优化的短回复与低噪音交互。

## 核心功能

### 手机远程编码

在飞书中直接向机器人发送需求，Bridge 会调用本机 `claude`，并将 Claude Code 的执行结果返回到飞书。

适合以下场景：

- 临时查看项目代码；
- 远程修改小功能；
- 运行测试或查看 Git 状态；
- 在手机上继续处理 Claude Code 任务。

### `@` 文件 / 目录选择

发送 `@` 后，Bridge 会在飞书中打开文件选择卡片。

你可以：

- 点击目录继续展开；
- 点击文件让 Claude 预读取该文件；
- 点击“使用当前目录”让 Claude 预读取当前目录。

预读取完成后，Claude 会只回复一句：

```text
已读完 xxx，可以继续提问。
```

之后，你可以直接使用“当前文件夹”“这里”“这个目录”等自然语言表达，Bridge 会默认指向刚才选择的路径。

### 权限三选一

当用户请求涉及以下高危操作时，Bridge 会先发送权限确认卡片：

- 创建、修改、删除文件；
- 安装依赖；
- 执行命令；
- 运行测试；
- 其他可能改变本地项目状态的操作。

权限卡片提供三个选项：

| 选项           | 含义                           |
| -------------- | ------------------------------ |
| 允许一次       | 仅允许本次请求继续执行         |
| 本会话总是允许 | 当前会话后续不再重复弹出权限卡 |
| 拒绝           | 取消本次执行                   |

### 会话级授权

选择“本会话总是允许”后，当前 chat 会话会记住授权状态。

后续同一会话中的写入、命令执行、测试运行等操作会直接交给 Claude Code，不再重复确认。

如需撤销授权，可以发送：

```text
/delete-auth
```

或：

```text
/revoke
```

也可以直接说：

```text
删除授权
撤销授权
清除权限
```

### 长任务进度通知

对于超过 35 秒的任务，Bridge 会主动推送进度消息（"仍在处理中… 已等待 X 分 X 秒"），之后每 45 秒更新一次。短任务不会产生额外噪音。

### 启动自检

Bridge 启动时自动验证：

- 飞书凭证是否有效（支持环境变量注入）；
- `allowed_roots` 是否过于宽泛；
- `allowed_user_ids` 白名单是否配置；
- Claude CLI 超时设置是否合理。

这些检查帮助你在上线前发现配置风险。

### 中转站模式

项目默认不在本地长期保存敏感数据：

```yaml
storage:
  persist_sessions: false
  persist_tasks: false
  persist_pending_messages: false
```

也就是说，Bridge 默认只作为轻量中转层使用：

- 不持久化会话；
- 不持久化任务；
- 不持久化待补发消息；
- 进程重启后不恢复历史状态。

如果确实需要恢复能力，可以按需将对应配置改为 `true`。

### 低噪音移动端体验

为了适配手机端使用，项目默认关闭：

- 回答后的快捷操作卡片（可通过 `mobile.quick_actions_after_reply` 开启）；
- 自动任务状态卡片（可通过 `mobile.auto_status_cards` 开启）。

这样 Claude Code 回复完成后，你可以像普通聊天一样继续提问，不会被过多卡片打断。

此外，长任务（超过 35 秒）会自动推送进度提示，短任务则保持静默，兼顾信息透明和低噪音。

## 工作流程

整体执行流程如下：

1. 在目标项目目录中启动 `feishu`。
2. 启动脚本将当前目录写入 `FEISHU_CLAUDE_PROJECT_DIR`。
3. Bridge 通过飞书 WebSocket 长连接接收用户消息。
4. Bridge 判断消息类型：
   - 内建命令；
   - 路径选择；
   - 权限确认；
   - 普通 Claude Code 请求。
5. 普通问题直接调用 Claude Code。
6. 涉及写入、执行命令或运行测试的请求，先发送权限卡片。
7. Claude Code 在目标项目目录中完成任务。
8. Bridge 将最终结果返回飞书。

## 快速开始

### 1. 准备环境

确保本机可以直接运行 Claude Code CLI：

```bash
claude --version
```

建议创建独立的 conda 环境：

```bash
conda create -n feishu-claude python=3.9
conda activate feishu-claude
pip install -r requirements.txt
```

### 2. 配置飞书应用

复制示例配置并编辑本地 `config.yaml`：

```bash
cp config.example.yaml config.yaml
```

```yaml
feishu:
  app_id: "FEISHU_APP_ID"
  app_secret: "FEISHU_APP_SECRET"
  max_message_length: 3500
  send_retry_times: 3
  send_retry_interval_seconds: 0.8

claude:
  fallback_project_dir: "~"
  timeout_seconds: 300
  max_turns: 5
  output_format: "text"
  max_concurrent_runs: 4
  permission_mode: "default"

mobile:
  short_reply_default: true
  short_reply_lines: 6
  auto_status_cards: false
  quick_actions_after_reply: false

storage:
  persist_sessions: false
  persist_tasks: false
  persist_pending_messages: false

security:
  allowed_user_ids: []

paths:
  allowed_roots:
    - "~"
```

也可以通过环境变量覆盖飞书凭证：

```bash
export FEISHU_APP_ID="your_app_id"
export FEISHU_APP_SECRET="your_app_secret"
```

### 3. 启动 Bridge

在你想远程控制的项目目录中执行：

```bash
feishu
```

或者在 Bridge 项目目录中执行：

```bash
make run
```

如果还没有配置全局 `feishu` 命令，可以直接运行：

```bash
bash ./scripts/feishu
```

## 飞书后台配置要求

飞书应用需要开启以下能力：

- 机器人能力；
- 接收消息事件；
- 交互式卡片；
- 事件订阅：`card.action.trigger`。

如果卡片按钮点击后没有反应，优先检查：

- 是否订阅了 `card.action.trigger`；
- 是否开启交互式卡片能力；
- 机器人是否具有接收消息事件权限；
- Bridge 进程是否仍在运行。

## 常用命令

| 命令                  | 作用                                          |
| --------------------- | --------------------------------------------- |
| `/help`             | 查看帮助                                      |
| `/pwd`              | 查看当前工作目录                              |
| `/cd`               | 打开目录选择卡片                              |
| `/cd 路径`          | 切换当前会话的工作目录                        |
| `/reset`            | 重置当前会话，并清除会话授权                  |
| `/clear`            | 清除当前会话，并清除会话授权                  |
| `/sessions`         | 查看当前会话与总会话数                        |
| `/status`           | 打开最近任务状态卡片                          |
| `/status task_id`   | 查看指定任务状态                              |
| `/cancel task_id`   | 取消任务                                      |
| `/last`             | 查看最近任务                                  |
| `/resume`           | 让 Claude Code 继续最近一次任务               |
| `/delete-auth`      | 删除本会话授权，需卡片确认                    |
| `/revoke`           | 同`/delete-auth`                            |
| `/git`              | 打开 Git 快捷面板                             |
| `/git status`       | 查看 Git 状态                                 |
| `/git diff`         | 查看 Git diff 统计                            |
| `/git log`          | 查看当前分支和最近提交                        |
| `/git files`        | 查看变更文件                                  |
| `/git test`         | 请求权限后，由 Claude Code 运行测试           |
| `/test`             | 请求权限后，由 Claude Code 自动识别并运行测试 |
| `/find 关键词`      | 按文件名搜索                                  |
| `/grep 关键词`      | 按文件内容搜索                                |
| `/open 路径[:行号]` | 预览文件片段                                  |
| `/health`           | 诊断 Bridge 和 Claude CLI 运行状态            |
| `/continue 问题`    | 继续 Claude Code 最近会话                     |
| `@`                 | 打开文件 / 目录选择器                         |
| `@文件或目录 问题`  | 让 Claude Code 重点阅读指定路径并回答         |

## `@` 上下文选择示例

发送：

```text
@
```

在卡片中选择 `scripts` 目录后，Claude 会先预读取该目录。

随后可以继续发送：

```text
在当前文件夹里创建一个 txt 文件，内容是 hello
```

Bridge 会先发送权限卡片。授权后，Claude Code 会在刚才选择的 `scripts` 目录中执行，并在回复前验证结果。

## 权限机制说明

Bridge 的权限控制分为两层。

### 1. 前置权限卡

当用户消息明显包含以下意图时，Bridge 会先发送权限卡片，再启动 Claude Code：

- 创建文件；
- 修改文件；
- 删除文件；
- 安装依赖；
- 执行命令；
- 运行测试。

### 2. Claude Code 权限兜底

如果 Claude Code 执行过程中返回权限错误，Bridge 会再次发送三选一卡片。

用户授权后，Bridge 会使用：

```bash
--permission-mode bypassPermissions
```

继续执行本次请求。

## 配置说明

### `feishu`

| 配置项                          | 说明                                       |
| ------------------------------- | ------------------------------------------ |
| `app_id` / `app_secret`     | 飞书应用凭证，建议生产环境通过环境变量注入 |
| `max_message_length`          | 飞书单条文本消息的分片长度                 |
| `send_retry_times`            | 消息发送失败后的重试次数                   |
| `send_retry_interval_seconds` | 消息发送失败后的重试间隔                   |

### `claude`

| 配置项                   | 说明                                              |
| ------------------------ | ------------------------------------------------- |
| `fallback_project_dir` | 没有会话目录时使用的默认目录                      |
| `timeout_seconds`      | Claude Code 单次执行超时时间                      |
| `max_turns`            | 传给 Claude Code 的最大轮数                       |
| `output_format`        | Claude Code 输出格式，通常保持`text`            |
| `max_concurrent_runs`  | 全局最多并发 Claude Code 任务数                   |
| `permission_mode`      | 默认建议保持`default`，授权后由 Bridge 临时切换 |

### `mobile`

| 配置项                        | 说明                               |
| ----------------------------- | ---------------------------------- |
| `short_reply_default`       | 默认让 Claude 输出手机友好的短结论 |
| `short_reply_lines`         | 短回复的默认行数                   |
| `auto_status_cards`         | 是否自动发送任务状态卡片           |
| `quick_actions_after_reply` | 是否在回答后发送快捷操作卡片       |

### `storage`

| 配置项                       | 说明                 |
| ---------------------------- | -------------------- |
| `persist_sessions`         | 是否持久化会话       |
| `persist_tasks`            | 是否持久化任务       |
| `persist_pending_messages` | 是否持久化待补发消息 |

### `security`

| 配置项               | 说明                                                                     |
| -------------------- | ------------------------------------------------------------------------ |
| `allowed_user_ids` | 允许访问机器人的飞书用户 ID 白名单。为空时默认允许所有能触达机器人的用户 |

正式使用时，建议填写自己的飞书用户 ID，避免其他成员误操作本机项目。

### `paths`

| 配置项            | 说明                              |
| ----------------- | --------------------------------- |
| `allowed_roots` | Bridge 可访问和切换的根目录白名单 |

建议将 `allowed_roots` 收敛到具体项目目录，避免授予过大的路径范围。

## 项目结构

```text
feishu-claude-bridge/
├── main.py              # WebSocket 入口、信号处理、优雅退出、配置自检
├── config.py            # YAML 配置加载、默认值、启动前 validate_config()
├── session.py           # chat_id 隔离的 Claude 会话与工作目录管理
├── client.py            # 飞书消息发送、Markdown→post 富文本（链接/表格/代码块）
├── claude_runner.py     # Claude Code CLI 子进程调用、超时控制、权限重试
├── commands.py          # /cd /git /find /grep /open /health 等内建命令
├── handlers.py          # 消息路由、任务调度、权限卡片、@ 上下文管理
├── task_manager.py      # 任务生命周期、取消、重试、可选持久化
├── file_picker.py       # @ 触发的文件/目录选择卡片
├── permission_flow.py   # 阻塞式同步权限三选一与会话授权
├── utils.py             # 路径校验（带缓存）、文本提取、用户白名单
├── requirements.txt     # Python 依赖
├── config.yaml          # 本地配置（不提交）
├── config.example.yaml  # 配置模板
├── Makefile
└── scripts/feishu       # 启动脚本，自动激活 conda 环境并绑定项目目录
```

## 常见问题

### 找不到 `claude` 命令

确认启动 Bridge 的环境中可以执行：

```bash
claude --version
```

如果使用 conda 环境，请确认启动脚本已经正确激活环境。

### 卡片点击提示过期或无效

常见原因包括：

- 飞书后台没有订阅 `card.action.trigger`；
- 没有开启交互式卡片能力；
- 同一张权限卡已经被点击过；
- Bridge 进程重启后，内存中的权限请求已丢失。

可以发送 `/health` 确认 Bridge 运行状态。

### 选择目录后，“当前文件夹”指向不对

先确认 `@` 选择完成后，Claude 是否回复：

```text
已读完 xxx，可以继续提问。
```

只有预读取完成后，后续“当前文件夹”“这里”“这个目录”才会绑定到刚才选择的路径。

### 不想本地生成 `tasks.json` / `sessions.json`

保持以下配置为 `false`：

```yaml
storage:
  persist_sessions: false
  persist_tasks: false
  persist_pending_messages: false
```

### Claude 回复支持哪些 Markdown 格式？

Bridge 会将 Claude Code 的输出转为飞书 **post 富文本**消息，支持的格式：

| Markdown          | 飞书效果             |
| ----------------- | -------------------- |
| `**粗体**`        | 粗体                 |
| `*斜体*`          | 下划线               |
| `` `行内代码` ``  | 等宽字体             |
| ` ```代码块``` `  | 等宽字体（自动去语言标签） |
| `[文字](url)`     | 可点击链接           |
| `![图片](url)`    | 图片占位标记         |
| `# 标题`          | 粗体标题             |
| `> 引用`          | 引用块前缀           |
| `- 列表`          | 圆点列表             |
| `\| 表格 \|`      | 等宽文本表格         |

超长消息自动降级为纯文本发送。

### 启动目录不对

`scripts/feishu` 会把执行启动命令时所在目录作为远程项目目录。

因此，请在目标项目目录中运行：

```bash
feishu
```

## 使用建议

- 日常问答直接发送普通消息。
- 需要指定上下文时，先发送 `@` 选择文件或目录。
- 真正要修改文件时，直接说“在当前文件夹创建 / 修改 / 删除……”即可。
- 长时间远程编码时，可以选择一次“本会话总是允许”。
- 远程操作结束后，建议使用 `/delete-auth` 撤销授权。
- 正式使用时，建议配置 `allowed_user_ids` 和更严格的 `allowed_roots`。

## 适用场景

Feishu Claude Bridge 适合：

- 想在手机端远程控制 Claude Code 的个人开发者；
- 经常需要临时查看或修改本机项目的人；
- 希望通过飞书机器人管理本地 AI 编码任务的用户；
- 对权限确认、路径白名单和默认无持久化有安全要求的远程编码场景。

## 免责声明

Bridge 会在用户授权后调用本机 Claude Code 执行文件修改、命令运行等操作。请在正式使用前合理配置：

- 飞书用户白名单；
- 可访问路径白名单；
- Claude Code 权限策略；
- 本地项目备份与 Git 版本控制。

建议在重要项目中先通过 Git 保持干净工作区，再进行远程编码操作。
