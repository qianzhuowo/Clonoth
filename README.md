<div align="center">
  <img src="public/logo.jpg" alt="Clonoth Logo" width="200" />
  <h1>Clonoth</h1>
  <p>声明式、可审计的多 Agent 引擎</p>
</div>

## 0. 架构

Clonoth 采用自包含节点架构，由以下部分组成：

- **Supervisor**：控制面。负责 HTTP API、事件日志、审批流程、安全策略、进程管理。
- **Engine**：执行引擎。加载自包含节点定义，调度 AI 节点和工具节点，支持节点间委派（dispatch/return）。
- **Toolbox**：工具层。包含 14 个内置工具和 `tools/` 下的脚本工具。
- **Shell CLI**：终端适配层。负责用户交互、流式输出、审批确认。

所有角色——入口节点、执行节点、审核节点、规划节点——都是 AI 节点的不同配置。每个节点自身包含提示词、模型、工具权限和委派目标（delegate_targets），不依赖外部 workflow 文件。

## 1. 快速开始

### 1.1 安装依赖

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\activate
# Linux/Mac
# source .venv/bin/activate

pip install -r requirements.txt
```

### 1.2 配置模型

编辑 `data/config.yaml`（可参考 `config.example.yaml`）：

```yaml
version: 1
provider: openai
openai:
  base_url: "${OPENAI_BASE_URL}"
  api_key: "${OPENAI_API_KEY}"
  model: "${OPENAI_MODEL}"
```

### 1.3 启动

**单入口启动（推荐）**

```bash
python main.py
```

Supervisor 会自动拉起 Engine 和 Shell CLI。

**分开启动**

```bash
# 启动控制面
python -m supervisor.main --no-workers

# 启动执行引擎
python -m engine --supervisor http://127.0.0.1:8765

# 启动终端 CLI
python -m shell.cli --supervisor http://127.0.0.1:8765
```

## 2. 节点系统

### 2.1 节点定义

`config/nodes/*.yaml` 定义节点。每个节点声明自己的类型、提示词、模型路由和工具权限。

当前默认节点：

| 节点 | 用途 |
|---|---|
| `bootstrap.shell_orchestrator` | 入口节点。判断直接回复还是移交下游 |
| `bootstrap.executor` | 执行节点。多步推理、调用工具 |
| `bootstrap.cmd_reviewer` | 命令审核节点。对 shell 命令做语义审核 |

每个节点的 `delegate_targets` 字段定义了它可以委派任务的下游节点。入口节点在 `config/runtime.yaml` 的 `shell.entry_node_id` 中指定。

### 2.2 节点内联提示词与模型

`config/nodes/*.yaml` 直接保存节点的 `prompt` 和 `model`。共享提示词片段可以通过 `{{include:文件名}}` 从 `config/nodes/` 同目录引入。
`kind`、`version`、`output_mode` 等默认字段可以省略。所有节点统一使用 `finish`（完成）、`ask`（提问）、`dispatch_node`（委派）三个伪工具。

## 3. 工具系统

### 3.1 内置工具

14 个内置工具：`list_dir`、`read_file`、`write_file`、`execute_command`、`search_in_files`、`create_or_update_skill`、`list_skills`、`delete_skill`、`create_or_update_mcp_client`、`list_mcp_clients`、`delete_mcp_client`、`create_or_update_tool`、`reload_tools`、`request_restart`。

### 3.2 脚本工具

`tools/*.py` 下的脚本工具以独立子进程运行。通过 stdin/stdout 的 JSON 协议通信。AI 可以通过 `create_or_update_tool` 创建新工具。

### 3.3 MCP 工具

MCP 客户端定义在 `data/mcp_clients.yaml`。Engine 启动时自动加载。

## 4. 安全策略

安全策略定义在 `data/policy.yaml`（参考 `policy.example.yaml`）。

### 4.1 双层审核

命令执行采用双层机制：

1. `cmd_reviewer` 节点做 AI 语义审核
2. Supervisor Policy 做硬规则和人类审批

AI 审核不能替代人类审批。

### 4.2 文件保护

| 路径 | 策略 |
|---|---|
| `engine/**`、`supervisor/**`、`toolbox/**`、`providers/**`、`shell/**` | 需要审批 |
| `config/nodes/**` | 需要审批 |
| `tools/**` | 需要审批 |
| `clonoth_runtime.py`、`main.py` | 需要审批 |
| `data/policy.yaml` | 硬拒绝 |
| `data/events.jsonl` | 硬拒绝 |
| `.env` | 硬拒绝 |
| `config/runtime.yaml` | 自动允许 |

### 4.3 命令硬拒绝

以下命令无论如何不会通过：`rm -rf /`、`rm -rf ~`、`rm -rf *`、`format`、`mkfs`、`fdisk`、`dd if=/dev/zero`、`shutdown`、`reboot`。

## 5. 自修改

AI 可以在人类审批下修改节点定义、工具和运行参数。修改后通过 `request_restart` 重启 Engine 使改动生效。

唯一不可修改的是安全策略（`data/policy.yaml`）和事件日志（`data/events.jsonl`）。

## 6. 目录结构

```text
Clonoth/
├── supervisor/     # 控制面
├── engine/         # 执行引擎
├── shell/          # CLI 适配层
├── toolbox/        # 工具层（内置工具 + 脚本工具运行器 + MCP）
├── providers/      # 模型适配层
├── config/         # nodes / runtime
├── tools/          # 脚本工具
├── skills/         # Skill 文件
├── data/           # 事件日志、配置、运行时数据
└── docs/           # 文档
```

## 7. 流式输出

`config/runtime.yaml` 中 `engine.streaming: true` 开启后，入口节点的 LLM 响应会逐块推送到 CLI。CLI 同时显示思维链（灰色）和文本内容（逐字输出）。
