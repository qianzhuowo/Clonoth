# Clonoth OneBot 11 适配器

NoneBot2 插件，通过 OneBot 11 协议（NapCat / go-cqhttp 等实现）将 QQ 消息接入 Clonoth。

## 目录结构

```
platform/onebot/
├── __init__.py       # 插件主体：消息处理、附件收集、EventRouter 回调
├── config.py         # 环境变量配置
└── emoji_handler.py  # QQ 自定义表情处理（表情包名称索引）
```

## 前置依赖

- Python 3.11+
- NoneBot2
- nonebot-adapter-onebot（OneBot V11 适配器）
- httpx（附件下载）
- OneBot 11 实现端（如 NapCat、go-cqhttp、Lagrange 等）
- clonoth_sdk（项目根目录下的 SDK 包）

```bash
pip install nonebot2 nonebot-adapter-onebot httpx
```

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `CLONOTH_BASE_URL` | 否 | `http://127.0.0.1:8765` | Supervisor API 地址 |
| `CLONOTH_WORKSPACE` | 否 | `/www/wwwroot/Clonoth` | 工作区路径，用于 SDK 导入和附件存储 |
| `CLONOTH_ENTRY_NODE` | 否 | `qq.orchestrator` | QQ 入口节点 ID。默认使用综合入口，支持联网搜索、调度、重启、取消任务，并可委派项目/命令相关任务；如需搜索-only 安全窄入口，可显式设置为 `qq.web_search`。 |
| `CLONOTH_BQBS_PATH` | 否 | 空 | QQ 自定义表情名称索引文件路径 |
| `CLONOTH_ADMIN_QQ_USERS` | 是 | `[占位符],[占位符]` | Clonoth 审批管理员 QQ 号，逗号分隔。只有这些用户能通过私聊命令批准/拒绝审批 |
| `CLONOTH_ALLOWED_GROUPS` | 是 | `[占位符]` | 允许接入的 QQ 群号，逗号分隔。默认占位符不会匹配任何真实群，避免空配置开放所有群 |
| `CLONOTH_ALLOWED_PRIVATE_USERS` | 否 | `[私聊只允许已经通过好友请求的人]` | 允许私聊使用 Clonoth 的 QQ 用户，逗号分隔。默认仅允许好友私聊；管理员始终允许 |

## 部署步骤

### 1. 部署 OneBot 11 实现端

以 NapCat 为例：

1. 安装 NapCat 并登录 QQ 账号。
2. 配置反向 WebSocket 连接地址，指向 NoneBot2 监听端口（默认 `ws://127.0.0.1:8080/onebot/v11/ws`）。
3. 确认连接建立后 NoneBot2 能收到消息事件。

### 2. 配置 NoneBot2 项目

在 NoneBot2 项目中加载本插件。将 `platform/onebot/` 目录作为 NoneBot2 插件加载：

```python
# bot.py 或 pyproject.toml
nonebot.load_plugin("platform.onebot")
```

或将目录放入 NoneBot2 插件目录，使其自动发现。

### 3. 配置环境变量

在 NoneBot2 的 `.env` 中添加：

```bash
CLONOTH_BASE_URL=http://127.0.0.1:8765
CLONOTH_WORKSPACE=/path/to/clonoth
CLONOTH_ENTRY_NODE=qq.orchestrator
CLONOTH_ADMIN_QQ_USERS=[占位符],[占位符]
CLONOTH_ALLOWED_PRIVATE_USERS=[私聊只允许已经通过好友请求的人]
CLONOTH_ALLOWED_GROUPS=[占位符]
```

### 4. 确保 Supervisor 已启动

本插件通过 Clonoth SDK 与 Supervisor 通信。启动前确认 Supervisor 已在 `CLONOTH_BASE_URL` 指定的地址上运行。

### 5. 启动 NoneBot2

```bash
nb run
```

## 功能说明

### 消息处理

- 群聊中 @Bot 触发回复，同时携带最近群聊历史作为上下文；只有 `CLONOTH_ALLOWED_GROUPS` 中的群会接入。
- 私聊消息直接触发回复，无需 @；默认只允许好友私聊，或通过 `CLONOTH_ALLOWED_PRIVATE_USERS` 指定用户。
- 管理员 QQ 号通过 `CLONOTH_ADMIN_QQ_USERS` 配置；管理员始终允许私聊，用于处理审批命令。
- 支持引用消息解析。

### 附件处理

图片附件从 QQ 临时 URL 下载并保存到 `data/attachments/` 目录，与 Discord 适配器使用相同的路径格式。MIME 类型根据 URL 和响应头自动推断。

### QQ 表情

支持 QQ 自定义表情（face segment）的文本化。如果配置了 `CLONOTH_BQBS_PATH`，可将表情 ID 映射为可读名称。

### 审批流程

QQ 适配器不会再自动放行 `approval_requested`。当 Clonoth 触发需要审批的内部或外部操作时：

1. Bot 会把审批摘要私聊发送给 `CLONOTH_ADMIN_QQ_USERS` 中的管理员。
2. 管理员通过私聊回复命令处理审批：
   - 同意：`审批 同意 <approval_id>`
   - 拒绝：`审批 拒绝 <approval_id>`
3. `<approval_id>` 可以填写完整 ID，也可以填写唯一前缀。
4. 如果未配置 `CLONOTH_ADMIN_QQ_USERS`，审批请求会被默认拒绝，避免无人确认时误放行。

### 任务状态反馈

默认入口为 `qq.orchestrator`，适合希望 QQ 同时承担联网搜索、调度、重启和取消任务的部署；`qq.web_search` 会继续保留为极简搜索入口，适合只想开放联网搜索能力的部署。

Bot 处理消息时，通过 QQ 表情 Reaction 反馈当前阶段；当检测到联网搜索工具进度时，还会向会话发送低频文本提示，例如“已收到联网搜索请求，正在检索网页资料……”，避免用户误以为 Bot 卡住：

| 阶段 | 表情 ID | 含义 |
|---|---|---|
| received | 76 | 已收到消息 |
| submitted | 281 | 已提交到引擎 |
| thinking | 178 | 模型思考中 |
| tool | 97 | 工具调用中 |
| writing | 326 | 生成回复中 |

## Docker 注意事项

- NapCat 容器重启后会丢失 QQ 登录 session，需要用手机重新扫码登录。**不要随意重启 NapCat 容器**。
- `CLONOTH_WORKSPACE` 指向的路径需要在容器内可访问（挂载为卷或与 Supervisor 共享卷）。
- NoneBot2 与 NapCat 之间的 WebSocket 连接需在容器网络中可达。

## 与 Discord 适配器的区别

| 特性 | Discord | OneBot |
|---|---|---|
| 协议 | Discord Gateway (WebSocket) | OneBot 11 (反向 WebSocket) |
| 框架 | discord.py | NoneBot2 |
| 触发方式 | 所有消息 / @Bot | 群聊 @Bot / 私聊 |
| 审批按钮 | Discord UI Button | QQ 管理员私聊命令审批（不自动放行） |
| Bridge Server | 有（discord_manage 工具） | 无 |
| 附件上传 | Discord CDN 下载 → 本地保存 | QQ 临时 URL 下载 → 本地保存 |
