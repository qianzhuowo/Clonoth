from __future__ import annotations

"""Clonoth external tool: QQ 聊天消息转发 / 私发 / 提醒。

用于 QQ Bot 的自然语言转发能力，让 AI 能解析并挑选上文消息完成发送任务：
  - “帮我把上面聊到的关于 xxx 的消息私发给我”
  - “把上面的会议内容和文件合并发送到群 xxx”
  - “转发这图片给 xx”
  - “帮我提醒一下 xx 明天记得带笔记本”
  - “把仓库里的 xx 文件发出来 / 发给我”（op=file）
  - “把刚才生成的那张图发给 xx”（op=recent 查看，再用 use_recent 发送）

工具在 Engine 子进程中运行，通过本地 HTTP Bridge 调用 QQ Bot 进程完成实际
发送。真实 QQ 群号/QQ 号只留在 Bot 进程内，本工具与模型上下文都只接触匿名下标、
关键词与目标别名。

调用流程建议：
  1. 先用 op="list" 查看当前群最近消息（带 index 下标、preview 预览）。
  2. 再用 op="forward"/"send" 并给出 message_indices（挑选的下标）或 query
     （关键词），以及 target_type/target_ref（发送目标）。
  3. 纯提醒/通知用 op="remind" + text，不需要挑选历史。
  4. 发送仓库文件用 op="file" + file_paths（工作区内相对路径，可多个）+ target_type。
"""

SPEC = {
    "name": "qq_forward",
    "description": (
        "QQ 聊天消息转发/私发/提醒工具。用于把当前群里的历史消息（可多选多条）转发或发送到"
        "指定 QQ 私聊/群聊，或发送一条提醒通知。适用于用户说“把上面聊到的关于 xxx 的消息私发"
        "给我”“把上面会议内容和文件合并发送到群 xxx”“转发这图片给 xx”“提醒 xx 明天带笔记本”"
        "等请求。\n\n"
        "先用 op=list 查看当前群最近消息（返回带 index 下标、preview 预览、has_image/has_file"
        "标记的列表），再用 op=forward 或 op=send 转发/发送，用 message_indices 挑选下标或 query"
        "关键词筛选消息；纯提醒用 op=remind 加 text。\n\n"
        "目标 target_type 取值：self（私发给当前用户/发给我）、current（当前群/本群）、"
        "private（指定私聊，target_ref 填对方显示名或 qq:号）、group（指定群，target_ref 填群显示名"
        "或 群:号）。真实 QQ 号由 Bot 进程内部解析，不需要也不要臆造真实号码。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": ["list", "forward", "send", "remind", "file", "recent"],
                "description": (
                    "操作类型。list=列出当前群最近消息供挑选；forward=合并转发挑选的消息（卡片形式）；"
                    "send=把挑选的消息拼成文本+附件直接发送；remind=发送一条提醒/通知文本；file=把 Clonoth 工作区（仓库）内的文件发送给目标（file_paths）；recent=列出本会话最近由 Bot 发出/生成的图片（如生图插件产出的图片）供挑选。"
                ),
            },
            "target_type": {
                "type": "string",
                "enum": ["self", "current", "private", "group"],
                "description": (
                    "发送目标类型。self=私发给当前用户；current=发送到当前群；private=指定私聊；group=指定群。"
                    "list 操作不需要该字段。"
                ),
            },
            "target_ref": {
                "type": "string",
                "description": (
                    "当 target_type 为 private/group 时的目标引用：对方/群的显示名或别名，"
                    "也可用 qq:号 / 群:号 显式指定。self/current 时留空即可。"
                ),
            },
            "message_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要挑选转发/发送的消息下标列表（来自 op=list 返回的 index，从 1 开始）。支持多选多条。",
            },
            "query": {
                "type": "string",
                "description": "按关键词筛选要转发/发送的消息（例如“会议”“xxx 项目”）。可与 message_indices 二选一或配合使用。",
            },
            "text": {
                "type": "string",
                "description": "附加文本：forward/send 时作为开头补充说明；remind 时为提醒正文（必填）。",
            },
            "include_images": {
                "type": "boolean",
                "default": True,
                "description": "是否连同挑选消息里的图片一起发送，默认 true。",
            },
            "include_files": {
                "type": "boolean",
                "default": True,
                "description": "是否连同挑选消息里的文件一起发送，默认 true。",
            },
            "limit": {
                "type": "integer",
                "default": 30,
                "description": "op=list 时返回的最近消息条数，默认 30。",
            },
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "op=file 时要发送的文件路径列表（可多个）。路径相对于 Clonoth 工作区根目录"
                    "（例如 data/report.pdf、config/nodes/qq.orchestrator.yaml）；只允许工作区内的文件。"
                ),
            },
            "file_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "op=file 时可选的显示文件名列表，与 file_paths 一一对应；缺省时使用原文件名。",
            },
            "use_recent": {
                "type": "boolean",
                "default": False,
                "description": (
                    "forward/send/file 时为 true 表示发送“最近生成/发出的图片”（如生图插件刚产出的图）。"
                    "用户说“把刚才那张图/刚生成的图发给 xx”时使用；不给 recent_indices 时默认取最新一张。"
                ),
            },
            "recent_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "从 op=recent 返回的列表里挑选要发送的图片下标（从 1 开始，可多选）；给出时自动启用 use_recent。",
            },
        },
        "required": ["op"],
    },
}

TIMEOUT_SEC = 30.0


if __name__ == "__main__":
    import json
    import os
    import sys
    import urllib.error
    import urllib.request

    def _read_input():
        raw = (sys.stdin.read() or "{}").lstrip("\ufeff")
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error, hint=""):
        message = str(error)
        if hint:
            message = f"{message}\n修复建议：{hint}"
        print(json.dumps(
            {"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}},
            ensure_ascii=False,
        ))
        sys.exit(1)

    def _env_first(*names, default=""):
        for name in names:
            value = os.environ.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return default

    args = _read_input()

    op = str(args.get("op") or "").strip().lower()
    if op not in {"list", "forward", "send", "remind", "file", "recent"}:
        fail(
            f"未知或缺少 op：{op or '(空)'}。",
            "op 必须是 list / forward / send / remind / file / recent 之一。建议先用 op=list 或 op=recent 查看可选内容。",
        )

    host = _env_first("ONEBOT_FORWARD_BRIDGE_HOST", default="127.0.0.1")
    port = _env_first("ONEBOT_FORWARD_BRIDGE_PORT", default="8769")
    token = _env_first("ONEBOT_FORWARD_BRIDGE_TOKEN", default="")
    # session_id 由 Engine 注入，Bridge 用它把请求映射回真实 QQ 群/用户，工具本身看不到真实号码。
    # 说明：入口任务实际运行在 branch session（CLONOTH_SESSION_ID=branch_xxx），而 QQ Bot
    # 侧的 _session_targets 仅按用户可见的 parent session 登记。因此这里优先使用
    # CLONOTH_PARENT_SESSION_ID，让 Bridge 能稳定映射回来源群/用户；缺省时再回退到
    # CLONOTH_SESSION_ID，并把两者都透传给 Bridge 供其做二次回退。
    parent_session_id = _env_first("CLONOTH_PARENT_SESSION_ID", default="")
    runtime_session_id = _env_first("CLONOTH_SESSION_ID", default="")
    session_id = parent_session_id or runtime_session_id
    if not session_id:
        fail(
            "缺少会话上下文（CLONOTH_SESSION_ID）。",
            "该工具只能在 QQ 会话内使用，无法在无会话上下文时定位来源群/用户。",
        )

    payload = {
        "op": op,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "runtime_session_id": runtime_session_id,
    }
    if op == "list":
        payload["query"] = str(args.get("query") or "").strip()
        try:
            payload["limit"] = int(args.get("limit") or 30)
        except Exception:
            payload["limit"] = 30
    elif op == "recent":
        payload["only_images"] = bool(args.get("only_images", True))
        try:
            payload["limit"] = int(args.get("limit") or 20)
        except Exception:
            payload["limit"] = 20
    else:
        payload["action"] = op
        payload["target_type"] = str(args.get("target_type") or "").strip().lower()
        payload["target_ref"] = str(args.get("target_ref") or "").strip()
        payload["query"] = str(args.get("query") or "").strip()
        payload["text"] = str(args.get("text") or "").strip()
        payload["include_images"] = bool(args.get("include_images", True))
        payload["include_files"] = bool(args.get("include_files", True))
        indices = args.get("message_indices")
        clean_indices = []
        if isinstance(indices, list):
            for item in indices:
                try:
                    clean_indices.append(int(item))
                except Exception:
                    continue
        payload["message_indices"] = clean_indices

        # “最近生成/发出的图片”选择（如生图插件产出的图）。
        use_recent = bool(args.get("use_recent", False))
        recent_indices = []
        raw_recent = args.get("recent_indices")
        if isinstance(raw_recent, list):
            for item in raw_recent:
                try:
                    recent_indices.append(int(item))
                except Exception:
                    continue
        if recent_indices:
            use_recent = True
        payload["use_recent"] = use_recent
        payload["recent_indices"] = recent_indices

        if op == "file":
            raw_paths = args.get("file_paths")
            if isinstance(raw_paths, str):
                file_paths = [raw_paths.strip()] if raw_paths.strip() else []
            elif isinstance(raw_paths, list):
                file_paths = [str(p).strip() for p in raw_paths if str(p).strip()]
            else:
                file_paths = []
            raw_names = args.get("file_names")
            if isinstance(raw_names, str):
                file_names = [raw_names]
            elif isinstance(raw_names, list):
                file_names = [str(n) for n in raw_names]
            else:
                file_names = []
            payload["file_paths"] = file_paths
            payload["file_names"] = file_names
            if not file_paths and not use_recent:
                fail(
                    "op=file 缺少 file_paths。",
                    "请在 file_paths 里给出要发送的工作区文件路径（相对于仓库根目录，可多个），或用 use_recent 发送最近生成的图片。",
                )

        if op != "remind" and not payload["target_type"]:
            fail(
                "缺少 target_type。",
                "请指定 target_type：self（私发给我）、current（当前群）、private（指定私聊）、group（指定群）。",
            )
        if op == "remind" and not payload["text"]:
            fail("op=remind 缺少提醒正文 text。", "请在 text 里写清楚提醒内容。")
        if op == "remind" and not payload["target_type"]:
            fail(
                "op=remind 缺少 target_type。",
                "提醒也需要目标：self / current / private / group。",
            )

    url = f"http://{host}:{port}/qq_forward"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Forward-Token"] = token

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            fail(err_body.get("error", f"HTTP {e.code}"))
        except SystemExit:
            raise
        except Exception:
            fail(f"转发 Bridge 返回 HTTP {e.code}")
    except urllib.error.URLError as e:
        fail(
            f"无法连接 QQ 转发 Bridge（{url}）：{e}",
            "请确认 QQ Bot 进程已启动且 ONEBOT_ENABLE_FORWARD_BRIDGE 未关闭。",
        )
    except Exception as e:
        fail(f"调用 QQ 转发 Bridge 失败：{e}")

    if not isinstance(body, dict) or not body.get("ok"):
        fail(str((body or {}).get("error") or "转发失败。"))

    if op == "list":
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        if not messages:
            result_text = "当前会话没有可挑选的历史消息（可能不是群聊，或暂无缓存消息）。"
        else:
            lines = ["当前群最近消息（用 index 下标挑选要转发/发送的消息）："]
            for item in messages:
                if not isinstance(item, dict):
                    continue
                marks = []
                if item.get("has_image"):
                    marks.append("图")
                if item.get("has_file"):
                    marks.append("件")
                mark_text = f" [{'/'.join(marks)}]" if marks else ""
                lines.append(f"#{item.get('index')}{mark_text} {item.get('preview') or ''}")
            result_text = "\n".join(lines)
        output({
            "ok": True,
            "data": {"result": result_text, "messages": messages},
        })

    if op == "recent":
        recent = body.get("recent") if isinstance(body.get("recent"), list) else []
        if not recent:
            result_text = "本会话没有最近由 Bot 发出/生成的图片可选（可能还未生图，或图片已过期）。"
        else:
            lines = ["本会话最近生成/发送的图片（用 recent_indices 下标挑选后配合 use_recent 发送）："]
            for item in recent:
                if not isinstance(item, dict):
                    continue
                lines.append(f"#{item.get('index')} [{item.get('type') or 'file'}] {item.get('name') or ''}")
            result_text = "\n".join(lines)
        output({
            "ok": True,
            "data": {"result": result_text, "recent": recent},
        })

    result_text = str(body.get("result") or "已完成。")
    output({"ok": True, "data": {"result": result_text}})
