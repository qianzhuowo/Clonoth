from __future__ import annotations

"""Generate NovelAI images from Scene Planner YAML.

This is the formatting/generation half of the LWB-like pipeline:
planner node -> YAML(scene/chars/costume/uc/center/size_label) -> this tool -> NovelAI images.
"""

SPEC = {
    "name": "nai_generate_from_plan",
    "description": "读取绘图分析节点输出的 YAML 计划，格式化为 NovelAI V4/V4.5 prompt 与角色级 caption，并逐张调用 NovelAI 生图。",
    "async_mode": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "plan_yaml": {"type": "string", "description": "绘图分析节点输出的 YAML 文本，包含 images 列表。推荐且标准字段。"},
            "yaml": {"type": "string", "description": "兼容字段：等同 plan_yaml"},
            "plan": {"description": "兼容字段：可以是 YAML 字符串，也可以是已解析的计划对象"},
            "images": {"type": "array", "description": "兼容字段：模型误把 images 单独传入时会自动包装为 YAML 计划"},
        },
        "required": ["plan_yaml"],
    },
}

TIMEOUT_SEC = 900.0


if __name__ == "__main__":
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from urllib import request as urllib_request

    from format_plan import build_tasks_from_plan

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    def emit_intermediate(text, attachments=None):
        supervisor_url = os.environ.get("CLONOTH_SUPERVISOR_URL", "").rstrip("/")
        session_id = os.environ.get("CLONOTH_PARENT_SESSION_ID") or os.environ.get("CLONOTH_SESSION_ID") or ""
        if not supervisor_url or not session_id:
            return
        payload = {
            "type": "intermediate_reply",
            "payload": {
                "node_id": os.environ.get("CLONOTH_NODE_ID", ""),
                "task_id": os.environ.get("CLONOTH_TASK_ID", ""),
                "text": str(text or ""),
                "attachments": attachments or [],
            },
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib_request.Request(
                f"{supervisor_url}/v1/sessions/{session_id}/events",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib_request.urlopen(req, timeout=3).read()
        except Exception:
            pass

    args = json.loads(sys.stdin.read() or "{}")
    raw_plan = args.get("plan_yaml") or args.get("yaml") or args.get("plan")
    if not raw_plan and isinstance(args.get("images"), list):
        raw_plan = {"mindful_prelude": {"visual_plan": {"reasoning": "auto-wrapped from images field"}}, "images": args.get("images")}
    if isinstance(raw_plan, dict):
        try:
            import yaml
            plan_yaml = yaml.safe_dump(raw_plan, allow_unicode=True, sort_keys=False)
        except Exception:
            plan_yaml = json.dumps(raw_plan, ensure_ascii=False)
    else:
        plan_yaml = str(raw_plan or "").strip()
    if not plan_yaml:
        fail("plan_yaml is required; accepted aliases: yaml, plan, images")

    try:
        tasks = build_tasks_from_plan(plan_yaml)
    except Exception as exc:
        fail(f"failed to parse plan_yaml: {exc}")
    if not tasks:
        fail("no image task parsed from plan_yaml")

    tool_path = Path(__file__).with_name("nai_generate.py")
    attachments = []
    results = []
    failures = []

    for task in tasks:
        payload = {
            "prompt": task["prompt"],
            "negative_prompt": task["negative_prompt"],
            "character_prompts": task["character_prompts"],
            "width": task["width"],
            "height": task["height"],
            "size_label": task["size_label"],
            "params": task["params"],
            "preset_id": task.get("preset_id", ""),
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(tool_path)],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=190,
            )
            parsed = json.loads((proc.stdout or "").strip() or "{}")
            if proc.returncode != 0 or parsed.get("ok") is False:
                failures.append({"index": task["index"], "error": parsed.get("error") or proc.stderr or f"exit {proc.returncode}"})
                continue
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            att = data.get("attachments") or parsed.get("attachments") or []
            if isinstance(att, list):
                attachments.extend(att)
            # 逐张自动发图不能依赖工具子进程直接 POST
            # 让 QQ/OneBot、Web 等平台都从同一条附件路由发送图片。
            results.append({
                "index": task["index"],
                "anchor": task.get("anchor", ""),
                "size_label": task["size_label"],
                "width": task["width"],
                "height": task["height"],
                "preset_id": task.get("preset_id", ""),
                "preset_name": task.get("preset_name", ""),
                "path": data.get("path"),
                "seed": data.get("seed"),
            })
        except Exception as exc:
            failures.append({"index": task["index"], "error": str(exc)})

    if not results:
        fail(f"all image generations failed: {failures}")

    lines = [f"已生成 {len(results)} 张图。"]
    for item in results:
        lines.append(f"- #{item['index']} {item['size_label']} {item.get('path') or ''}")
    if failures:
        lines.append(f"失败 {len(failures)} 张：" + "; ".join(f"#{f['index']} {f['error']}" for f in failures))

    output({
        "ok": True,
        "data": {
            "result": "\n".join(lines),
            "images": results,
            "failures": failures,
            "attachments": attachments,
        },
        "attachments": attachments,
    })
