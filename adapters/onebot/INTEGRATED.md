# 项目内 OneBot 集成说明

本项目已经完成 QQ / OneBot 的项目内集成。当前集成包含两层组件：

1. `adapters/onebot/app.py`：项目内 NoneBot2 接入层。它负责接收 OneBot V11 事件，并把 QQ 消息送入 Clonoth。
2. `adapters/onebot/napcat_manager.py`：项目内 NapCat 启动器。它负责启动项目目录中的 NapCat，并写入 OneBot 反向 WebSocket 配置。

## 当前验证状态

最近一次验证结果：

```text
Supervisor: 正常
OneBot 适配层: 正常
NapCat: 正常
NapCat 登录 QQ: 正常
OneBot 反向 WebSocket: 已连接
```

OneBot 适配层监听地址：

```text
ws://127.0.0.1:18080/onebot/v11/ws
```

NoneBot 日志中已经确认连接：

```text
OneBot V11 | Bot <BOT_QQ> connected
```

NapCat 日志中已经确认反向 WebSocket 已启动：

```text
WebSocket反向服务: ws://127.0.0.1:18080/onebot/v11/ws, : 已启动
```

端口连接中可以看到已建立连接：

```text
127.0.0.1:18080 <-> 127.0.0.1:<NapCat 临时端口>
```

## 当前本地目录

NapCat Windows 一键包与运行目录位于：

```text
data/onebot/napcat/onekey/
```

当前实际运行目录位于：

```text
data/onebot/napcat/onekey/NapCat.44498.Shell/
```

下载缓存位于：

```text
data/onebot/downloads/
```

项目侧 OneBot 配置记录位于：

```text
data/onebot/config/napcat-onebot.json
```

运行日志位于：

```text
data/logs/onebot-adapter-*.log
data/logs/napcat-*.log
data/logs/napcat.out.log
data/logs/napcat.err.log
```

## Supervisor 自动启动

`config/runtime.yaml` 中启用了项目内 QQ 接入：

```yaml
supervisor:
  process_manager:
    spawn_onebot_adapter: true
    spawn_napcat: true
    onebot_host: "127.0.0.1"
    onebot_port: 18080
    onebot_entry_node: "qq_assistant"
    napcat_quick_login: "<BOT_QQ>"
```

Supervisor 启动后会自动拉起：

- Engine workers
- OneBot adapter
- NapCat manager
- Shell/TUI

正常情况下，不需要手动启动 `adapters/onebot/app.py` 或 `adapters/onebot/napcat_manager.py`。

## 连接关系

实际运行链路如下：

```text
QQ / NapCat
  -> OneBot V11 反向 WebSocket
  -> ws://127.0.0.1:18080/onebot/v11/ws
  -> adapters/onebot/app.py
  -> adapters/onebot/__init__.py
  -> clonoth_sdk
  -> Supervisor
  -> Engine
  -> EventRouter 回调
  -> QQ
```

## NapCat 配置写入规则

NapCat 会读取账号专用 OneBot 配置文件。项目管理脚本已经处理这一点。

通用配置会写入：

```text
data/onebot/napcat/onekey/config/onebot11.json
data/onebot/napcat/onekey/bootmain/config/onebot11.json
data/onebot/napcat/onekey/NapCat.44498.Shell/config/onebot11.json
```

账号专用配置会写入：

```text
data/onebot/napcat/onekey/NapCat.44498.Shell/versions/9.9.26-44498/resources/app/napcat/config/onebot11_<BOT_QQ>.json
```

这个账号专用配置中必须包含：

```json
{
  "network": {
    "websocketClients": [
      {
        "name": "ClonothReverseWebSocket",
        "enable": true,
        "url": "ws://127.0.0.1:18080/onebot/v11/ws",
        "messagePostFormat": "array"
      }
    ]
  }
}
```

如果 NapCat 能登录但 OneBot 没有连接，优先检查账号专用配置文件。

## QQ 登录

QQ 密码不会写入项目。

首次运行时，NapCat 可能需要扫码登录。扫码完成后，NapCat 会保存本地登录状态。后续运行使用 `napcat_quick_login` 中的 QQ 号作为快速登录参数。

该值只是 QQ 号，不是密码。

## 手动命令

检查 Supervisor：

```bash
python -c "import httpx; print(httpx.get('http://127.0.0.1:8765/v1/health', timeout=5).text)"
```

检查 NapCat：

```bash
python adapters/onebot/napcat_manager.py status
```

启动项目内 OneBot 接入层：

```bash
python adapters/onebot/app.py
```

管理 NapCat：

```bash
python adapters/onebot/napcat_manager.py start --reverse-host 127.0.0.1 --reverse-port 18080 --reverse-suffix /onebot/v11/ws --quick-login <BOT_QQ>
python adapters/onebot/napcat_manager.py stop
python adapters/onebot/napcat_manager.py status
```

检查端口连接：

```powershell
Get-NetTCPConnection -LocalPort 18080
```

正常状态应同时看到：

1. 一个监听状态。
2. 一个与 `127.0.0.1` 建立的连接。

## 使用方式

私聊机器人 QQ 时，直接发送消息即可。

群聊中需要 `@机器人` 才会触发回复。

当前入口节点为：

```text
qq_assistant
```

## QQ 语音输入与语音回复

QQ 语音输入和语音回复已经接入现有 Mimo ASR/TTS 工作流。

当前规则：

1. 用户发送 QQ 语音消息时，OneBot 适配器先下载语音文件，再调用 Supervisor 的 `/v1/audio/transcribe` 转写为文本。
2. 转写完成后，本地语音文件会被删除。会话上下文只保留转写文本和输入方式标记。
3. 用户用语音发送消息时，本轮默认允许语音回复。适配器会提示模型用 `<tts>...</tts>` 标出适合发成 QQ 语音的内容。
4. 用户用文字发送消息时，默认仍以文字回复。模型判断确实适合语音回复时，可以主动输出 `<tts>...</tts>` 段。
5. OneBot 适配器收到最终回复后，会提取 TTS 段，调用 Supervisor 的 `/v1/audio/synthesize` 合成音频。
6. 合成后的音频优先用 QQ `record` 消息发送。发送失败时退回为普通文件上传。
7. TTS 音频发送完成后会被删除。长期不保留接收和生成的音频文件。

相关配置位于 `config/runtime.yaml`：

```yaml
onebot_voice_input: true
onebot_voice_reply: true
onebot_voice_reply_on_voice_input: true
onebot_voice_reply_auto_on_text: true
onebot_voice_reply_private: true
onebot_voice_reply_group: true
onebot_delete_audio_after_use: true
onebot_tts_format: "wav"
onebot_tts_voice: ""
onebot_tts_instruction: ""
```

用户不需要输入 `[TTS:auto]` 或其他特殊标记。语音回复标记由模型按适配器注入的提示生成。

## 常见问题

### NapCat 运行但没有连接 OneBot

检查账号专用配置：

```text
data/onebot/napcat/onekey/NapCat.44498.Shell/versions/9.9.26-44498/resources/app/napcat/config/onebot11_<BOT_QQ>.json
```

确认里面存在 `websocketClients`，并且地址是：

```text
ws://127.0.0.1:18080/onebot/v11/ws
```

然后重启 NapCat。

### 端口 18080 被占用

检查占用进程：

```powershell
Get-NetTCPConnection -LocalPort 18080
```

如果存在手动启动残留的 `adapters/onebot/app.py`，应清理旧进程，只保留 Supervisor 管理的进程。

### 首次登录需要扫码

这是正常情况。扫码完成后，后续可以使用快速登录。二维码路径通常位于：

```text
data/onebot/napcat/onekey/NapCat.44498.Shell/versions/9.9.26-44498/resources/app/napcat/cache/qrcode.png
```
