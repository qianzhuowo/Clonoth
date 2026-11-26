"""submit_scene_plan 合同：错误码、校验、纠错文案与 JSON 外壳修复。

对应 LWB scene-plan-contract.js + scene-plan-arguments.js。本项目差异：
- 没有插图点（insert_after 退役），images[] 只要求 scene / characters；
- 角色库没有多形态，name 按正式名 / 别名归一，form 原样保留为可选字符串。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence

SUBMIT_SCENE_PLAN_TOOL_NAME = "submit_scene_plan"
SCENE_TITLE_MAX_LENGTH = 14
REQUIRED_IMAGE_FIELDS: tuple[str, ...] = ("scene", "characters")


def to_scene_character_prompt_tag(value: Any) -> str:
    text = str(value or "").strip()
    return "no humans" if text.lower() == "no_humans" else text


class ErrorCategory:
    INPUT = "input"
    AGENT_CONFIG = "agent-config"
    PROMPT = "prompt"
    TOOL_PROTOCOL = "tool-protocol"
    SCHEMA = "schema"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    PROVIDER = "provider"
    UNKNOWN = "unknown"


AGENT_CONFIG_ERROR_CODES = frozenset({"AGENT_PRESET_INVALID", "MODEL_MISSING", "PROVIDER_MISSING"})
PROMPT_ERROR_CODES = frozenset({"PROMPT_EXPANSION_FAILED", "PREPARED_INPUT_INVALID"})
INPUT_ERROR_CODES = frozenset({"EMPTY_MESSAGE", "IMAGE_LIMIT_EXCEEDED"})
TOOL_PROTOCOL_ERROR_CODES = frozenset({
    "TAGGED_TOOL_CALL_INVALID",
    "TOOL_CONTRACT_INVALID",
    "TOOL_CALL_MISSING",
    "TOOL_CALL_MULTIPLE",
    "TOOL_CALL_NAME_INVALID",
})
SCHEMA_ERROR_CODES = frozenset({
    "TOOL_ARGUMENTS_INVALID_JSON",
    "TOOL_ARGUMENTS_SCHEMA_INVALID",
    "NO_IMAGE_TASKS",
})
CORRECTABLE_ERROR_CODES = frozenset({
    "TOOL_CALL_MISSING",
    "TOOL_CALL_MULTIPLE",
    "TOOL_CALL_NAME_INVALID",
    "TOOL_ARGUMENTS_INVALID_JSON",
    "TOOL_ARGUMENTS_SCHEMA_INVALID",
    "NO_IMAGE_TASKS",
})


class ScenePlannerError(Exception):
    def __init__(self, message: str, code: str = "SCENE_PLANNER_ERROR",
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details: dict[str, Any] | None = dict(details) if details is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def get_error_category(error: BaseException | None) -> str:
    code = str(getattr(error, "code", "") or "")
    if code in INPUT_ERROR_CODES:
        return ErrorCategory.INPUT
    if code in AGENT_CONFIG_ERROR_CODES:
        return ErrorCategory.AGENT_CONFIG
    if code in PROMPT_ERROR_CODES:
        return ErrorCategory.PROMPT
    if code in TOOL_PROTOCOL_ERROR_CODES:
        return ErrorCategory.TOOL_PROTOCOL
    if code in SCHEMA_ERROR_CODES:
        return ErrorCategory.SCHEMA
    if code == "REQUEST_TIMEOUT":
        return ErrorCategory.TIMEOUT
    if code == "REQUEST_ABORTED":
        return ErrorCategory.ABORTED
    if code == "PROVIDER_REQUEST_FAILED":
        return ErrorCategory.PROVIDER
    return ErrorCategory.UNKNOWN


def is_correction_error(error: BaseException | None) -> bool:
    return str(getattr(error, "code", "") or "") in CORRECTABLE_ERROR_CODES


def get_correction_instruction(code: str) -> str:
    if code == "TOOL_CALL_MISSING":
        return f"你没有调用 Tool。请只调用一次 {SUBMIT_SCENE_PLAN_TOOL_NAME}，并提交完整计划。"
    if code == "TOOL_CALL_MULTIPLE":
        return f"你调用了多个 Tool。请合并为完整计划，并只调用一次 {SUBMIT_SCENE_PLAN_TOOL_NAME}。"
    if code == "TOOL_CALL_NAME_INVALID":
        return f"你调用了错误的 Tool。请只调用 {SUBMIT_SCENE_PLAN_TOOL_NAME}。"
    return f"{SUBMIT_SCENE_PLAN_TOOL_NAME} 参数未通过校验。请按错误位置修正后重新提交完整计划。"


_MAX_HINT_FIELDS = 5
_MAX_HINT_FIELD_CHARS = 40


def build_tool_protocol_retry_correction(error: BaseException | None) -> str:
    """外层重试时附给模型的一条 user 纠错；仅 TOOL_PROTOCOL 类错误才非空。"""
    code = str(getattr(error, "code", "") or "")
    if code not in TOOL_PROTOCOL_ERROR_CODES:
        return ""
    message = str(getattr(error, "message", None) or error or "未知错误")
    details = getattr(error, "details", None) or {}
    if code == "TAGGED_TOOL_CALL_INVALID":
        lines = [f"上一轮工具调用的 JSON 无效。解析器报错：{message}"]
        stray = details.get("strayFields") if isinstance(details, Mapping) else None
        if isinstance(stray, (list, tuple)) and stray:
            fields = ", ".join(str(f)[:_MAX_HINT_FIELD_CHARS] for f in list(stray)[:_MAX_HINT_FIELDS])
            lines.append(f"{fields} 被写到了 arguments 外面（多半是 arguments 提前用 }} 闭合了）。")
        lines.append(
            "请完整重新输出一个 <tool_call>，格式为 "
            f'{{"name":"{SUBMIT_SCENE_PLAN_TOOL_NAME}","arguments":{{…}}}}；'
            "逐层核对括号配对，所有参数字段（如 images）都必须放在 arguments 里面，不要多写或少写 } ]。"
        )
        return "\n".join(lines)
    return f"上一轮没有交出有效的工具调用：{message}\n{get_correction_instruction(code)}"


def _normalize_correction_details(details: Any) -> dict[str, Any]:
    if not isinstance(details, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in ("path", "rule", "received", "expected"):
        if key in details:
            out[key] = details[key]
    return out


def create_correction_result(error: BaseException) -> dict[str, Any]:
    """校验失败时作为工具返回交给模型的 JSON。"""
    code = str(getattr(error, "code", "") or "SCENE_PLANNER_ERROR")
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": str(getattr(error, "message", None) or error),
            "details": _normalize_correction_details(getattr(error, "details", None)),
        },
        "instruction": get_correction_instruction(code),
    }


def get_correction_signature(error: BaseException) -> str:
    """相同签名连续出现两次即视为模型无法自纠，提前终止。"""
    details = _normalize_correction_details(getattr(error, "details", None))
    return json.dumps(
        [str(getattr(error, "code", "") or ""), details.get("path", ""), details.get("rule", "")],
        ensure_ascii=False,
    )


# ---- JSON 外壳修复（scene-plan-arguments.js） ----

_IMAGES_PREFIX = re.compile(r'^[ \t\r\n]*\{[ \t\r\n]*"images"[ \t\r\n]*:[ \t\r\n]*(?=\[)')
_WS = " \t\r\n"


def _find_complete_array_end(source: str, start: int) -> int:
    stack: list[tuple[str, set[str] | None]] = []
    string_start = -1
    index = start
    length = len(source)
    while index < length:
        char = source[index]
        if string_start >= 0:
            if char == "\\":
                index += 1
            elif char == '"':
                frame = stack[-1] if stack else None
                nxt = index + 1
                while nxt < length and source[nxt] in _WS:
                    nxt += 1
                if frame is not None and frame[1] is not None and nxt < length and source[nxt] == ":":
                    key = json.loads(source[string_start:index + 1])
                    if key in frame[1]:
                        return -1
                    frame[1].add(key)
                string_start = -1
        elif char == '"':
            string_start = index
        elif char in "[{":
            stack.append(("]" if char == "[" else "}", set() if char == "{" else None))
        elif char in "]}":
            if not stack or stack.pop()[0] != char:
                return -1
            if not stack:
                return index + 1
        index += 1
    return -1


def repair_scene_plan_arguments(source: str) -> dict[str, Any] | None:
    """只修补完整 images 数组之后的外壳，绝不改数组内容。"""
    prefix = _IMAGES_PREFIX.match(source)
    if not prefix:
        return None
    try:
        array_end = _find_complete_array_end(source, prefix.end())
        if array_end < 0:
            return None
        tail = source[array_end:]
        if tail.strip(_WS) == "":
            repair = {"kind": "missing_root_closer", "offset": len(source), "removed": "", "inserted": "}"}
        else:
            root_close = re.match(r"^[ \t\r\n]*\}", tail)
            if not root_close:
                return None
            offset = array_end + root_close.end()
            suffix = source[offset:]
            if not re.fullmatch(r"[ \t\r\n}\]]+", suffix) or not re.search(r"[}\]]", suffix):
                return None
            repair = {"kind": "trailing_closers", "offset": offset, "removed": suffix, "inserted": ""}
        repaired = source[: repair["offset"]] + repair["inserted"]
        parameters = json.loads(repaired)
        if not isinstance(parameters, dict):
            return None
        return {"parameters": parameters, "argumentRepair": repair}
    except (ValueError, TypeError):
        return None


# ---- 角色库查找 ----

def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def find_known_character(name: Any, present_characters: Sequence[Mapping[str, Any]] | None) -> dict[str, Any] | None:
    """按正式名 / 别名（忽略大小写与首尾空白）找已录入角色。"""
    key = _norm_name(name)
    if not key:
        return None
    for character in present_characters or []:
        if not isinstance(character, Mapping):
            continue
        if _norm_name(character.get("name")) == key:
            return dict(character)
        aliases = character.get("aliases") or []
        if isinstance(aliases, (list, tuple)) and any(_norm_name(alias) == key for alias in aliases):
            return dict(character)
    return None


# ---- 校验 ----

def _normalize_limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 and not isinstance(value, bool) else 0


def fail_schema(path: str, message: str, value: Any, expected: Any = None) -> None:
    raise ScenePlannerError(
        f"场景计划参数无效：{path} {message}",
        "TOOL_ARGUMENTS_SCHEMA_INVALID",
        {"path": path, "rule": message, "received": value, "expected": message if expected is None else expected},
    )


def _assert_object(value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        fail_schema(path, "必须是 object", value)


def _assert_required_fields(value: Any, fields: Iterable[str], path: str) -> None:
    _assert_object(value, path)
    for key in fields:
        if key not in value:
            fail_schema(f"{path}.{key}", "是必填字段", None)


def _assert_exact_fields(value: Any, fields: Sequence[str], path: str) -> None:
    _assert_required_fields(value, fields, path)
    allowed = set(fields)
    for key in value.keys():
        if key not in allowed:
            fail_schema(f"{path}.{key}", "是不允许的字段", value[key])


def _require_string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        fail_schema(path, "必须是 string", value)
    normalized = value.strip()
    if not allow_empty and not normalized:
        fail_schema(path, "不能为空", value)
    return normalized


def _optional_string(value: Mapping[str, Any], key: str, path: str) -> str:
    if key not in value or value[key] is None:
        return ""
    return _require_string(value[key], f"{path}.{key}", allow_empty=True)


GRID_COL = {"A": 0.1, "B": 0.3, "C": 0.5, "D": 0.7, "E": 0.9}
GRID_ROW = {"1": 0.1, "2": 0.3, "3": 0.5, "4": 0.7, "5": 0.9}
_GRID_RE = re.compile(r"^([A-E])([1-5])$")


def normalize_center(value: Any, path: str, center_mode: str) -> dict[str, float]:
    if value is None:
        return {"x": 0.5, "y": 0.5}
    if center_mode == "normalized":
        _assert_exact_fields(value, ("x", "y"), path)

        def coordinate(raw: Any, raw_path: str) -> float:
            number: float
            if isinstance(raw, bool):
                number = float("nan")
            elif isinstance(raw, (int, float)):
                number = float(raw)
            elif isinstance(raw, str) and raw.strip():
                try:
                    number = float(raw)
                except ValueError:
                    number = float("nan")
            else:
                number = float("nan")
            if number != number or number < 0 or number > 1:
                fail_schema(raw_path, "必须是 0～1 的数字", raw)
            return number

        return {"x": coordinate(value.get("x"), f"{path}.x"), "y": coordinate(value.get("y"), f"{path}.y")}
    grid = _require_string(value, path).upper()
    match = _GRID_RE.match(grid)
    if not match:
        fail_schema(path, "必须是 A1～E5 坐标", value)
    return {"x": GRID_COL[match.group(1)], "y": GRID_ROW[match.group(2)]}


def normalize_character(value: Any, path: str, present_characters: Sequence[Mapping[str, Any]] | None,
                        center_mode: str) -> dict[str, Any]:
    _assert_required_fields(value, ("name",), path)
    returned_name = _require_string(value["name"], f"{path}.name")
    known = find_known_character(returned_name, present_characters)
    canonical = str(known.get("name") or "").strip() if known else ""
    type_ = _optional_string(value, "type", path)
    appear = _optional_string(value, "appear", path)
    if not canonical and not appear:
        fail_schema(f"{path}.appear", "未知角色必须填写外貌", value.get("appear"))
    center = normalize_center(value.get("center"), f"{path}.center", center_mode)
    return {
        "name": canonical or returned_name,
        "form": _optional_string(value, "form", path),
        "danbooru": _optional_string(value, "danbooru", path),
        "type": type_,
        "appear": "" if canonical else appear,
        "costume": _optional_string(value, "costume", path),
        "action": _optional_string(value, "action", path),
        "interact": _optional_string(value, "interact", path),
        "uc": _optional_string(value, "uc", path),
        "center": center,
    }


def normalize_orientation(value: Any) -> str:
    return "landscape" if str(value or "").strip().lower() == "landscape" else "portrait"


def normalize_images(images: Any, *, max_images: Any = 0, max_plan_images: Any = 0,
                     max_characters_per_image: Any = 0, center_mode: str = "grid",
                     present_characters: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not isinstance(images, list):
        fail_schema("images", "必须是 array", images)
    if not images:
        raise ScenePlannerError("场景计划没有图片任务。", "NO_IMAGE_TASKS", {
            "path": "images", "rule": "必须至少提交一个图片任务", "received": 0, "expected": "非空 images 数组",
        })
    exact = _normalize_limit(max_images)
    plan_cap = _normalize_limit(max_plan_images)
    char_cap = _normalize_limit(max_characters_per_image)
    mode = "normalized" if center_mode == "normalized" else "grid"
    if exact and len(images) != exact:
        fail_schema("images", f"本次必须恰好包含 {exact} 项", len(images))
    if not exact and plan_cap and len(images) > plan_cap:
        fail_schema("images", f"本次最多包含 {plan_cap} 项", len(images))
    tasks: list[dict[str, Any]] = []
    for image_index, image in enumerate(images):
        path = f"images[{image_index}]"
        _assert_required_fields(image, REQUIRED_IMAGE_FIELDS, path)
        characters = image["characters"]
        if not isinstance(characters, list):
            fail_schema(f"{path}.characters", "必须是 array", characters)
        if char_cap and len(characters) > char_cap:
            fail_schema(f"{path}.characters", f"最多包含 {char_cap} 人", len(characters))
        chars = [
            normalize_character(character, f"{path}.characters[{i}]", present_characters, mode)
            for i, character in enumerate(characters)
        ]
        post_prompt = _optional_string(image, "post_prompt", path)
        title = _optional_string(image, "title", path)[:SCENE_TITLE_MAX_LENGTH]
        task: dict[str, Any] = {
            "index": image_index + 1,
            "scene": _require_string(image["scene"], f"{path}.scene"),
            "orientation": normalize_orientation(image.get("orientation")),
            "chars": chars,
        }
        if title:
            task["title"] = title
        if post_prompt:
            task["post_prompt"] = post_prompt
        tasks.append(task)
    return tasks


def parse_arguments(raw_arguments: Any, *, allow_repair: bool = True) -> dict[str, Any]:
    if isinstance(raw_arguments, Mapping):
        return {"parameters": dict(raw_arguments)}
    if not isinstance(raw_arguments, str):
        raise ScenePlannerError(f"{SUBMIT_SCENE_PLAN_TOOL_NAME} 参数不是 JSON object。", "TOOL_ARGUMENTS_INVALID_JSON", {
            "path": "toolCalls[0].arguments", "rule": "必须是合法 JSON object",
            "received": type(raw_arguments).__name__, "expected": "JSON object 字符串",
        })
    try:
        parsed = json.loads(raw_arguments)
        if not isinstance(parsed, dict):
            raise TypeError("root must be an object")
        return {"parameters": parsed}
    except (ValueError, TypeError) as error:
        recovered = repair_scene_plan_arguments(raw_arguments) if allow_repair else None
        if recovered:
            return recovered
        raise ScenePlannerError(
            f"{SUBMIT_SCENE_PLAN_TOOL_NAME} 参数 JSON 损坏或截断：{error}",
            "TOOL_ARGUMENTS_INVALID_JSON",
            {"path": "toolCalls[0].arguments", "rule": "必须是合法且完整的 JSON object",
             "received": raw_arguments[:160], "expected": "完整 JSON object"},
        ) from error


def parse_submitted_scene_plan(tool_calls: Sequence[Mapping[str, Any]] | None, *,
                               present_characters: Sequence[Mapping[str, Any]] | None = None,
                               max_images: Any = 0, max_plan_images: Any = 0,
                               max_characters_per_image: Any = 0, center_mode: str = "grid",
                               allow_repair: bool = True, context: str = "") -> dict[str, Any]:
    """tool_calls: [{name, arguments}]；返回 {tasks, argumentRepair?}。"""
    calls = list(tool_calls or [])
    if not calls:
        ctx = f"（{context}）" if context else ""
        raise ScenePlannerError(
            f"本次响应没有解析到 {SUBMIT_SCENE_PLAN_TOOL_NAME} Tool Call{ctx}。"
            "这不代表模型不支持 Tool Calling，请根据最近请求记录核对返回协议。",
            "TOOL_CALL_MISSING",
            {"path": "toolCalls", "rule": f"必须且只能调用一次 {SUBMIT_SCENE_PLAN_TOOL_NAME}",
             "received": 0, "expected": f"1 个 {SUBMIT_SCENE_PLAN_TOOL_NAME} Tool Call"},
        )
    if len(calls) > 1:
        raise ScenePlannerError(
            f"场景规划必须只提交一次，但模型返回了 {len(calls)} 个 Tool Call。",
            "TOOL_CALL_MULTIPLE",
            {"path": "toolCalls", "rule": f"必须且只能调用一次 {SUBMIT_SCENE_PLAN_TOOL_NAME}",
             "received": len(calls), "expected": f"1 个 {SUBMIT_SCENE_PLAN_TOOL_NAME} Tool Call"},
        )
    call = calls[0] or {}
    name = str(call.get("name") or "")
    if name != SUBMIT_SCENE_PLAN_TOOL_NAME:
        raise ScenePlannerError(
            f"模型调用了错误的 Tool：{name or '未命名'}。",
            "TOOL_CALL_NAME_INVALID",
            {"path": "toolCalls[0].name", "rule": f"必须调用 {SUBMIT_SCENE_PLAN_TOOL_NAME}",
             "received": name, "expected": SUBMIT_SCENE_PLAN_TOOL_NAME},
        )
    parsed = parse_arguments(call.get("arguments"), allow_repair=allow_repair)
    parameters = parsed["parameters"]
    if "images" not in parameters:
        fail_schema("parameters.images", "是必填字段", None)
    tasks = normalize_images(
        parameters["images"],
        max_images=max_images,
        max_plan_images=max_plan_images,
        max_characters_per_image=max_characters_per_image,
        center_mode=center_mode,
        present_characters=present_characters,
    )
    result: dict[str, Any] = {"tasks": tasks}
    if parsed.get("argumentRepair"):
        result["argumentRepair"] = parsed["argumentRepair"]
    return result
