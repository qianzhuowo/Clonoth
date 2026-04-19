"""工具格式抽象层。

提供 ToolFormatter ABC，封装工具定义注入、响应解析、结果格式化三个环节。
当前实现：
  - NativeToolFormatter: 使用 OpenAI function calling（现有行为）
  - JsonToolFormatter:   使用 JSON 文本块内嵌工具调用

设计目标：
  让 ai_step 的推理循环不直接依赖 OpenAI tool calling 格式，
  后续切换到 JSON 文本工具调用时只需替换 formatter 实例。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from providers.base import ProviderResponse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  工具调用统一表示
# ---------------------------------------------------------------------------

@dataclass
class ParsedToolCall:
    """从 LLM 响应中解析出的工具调用，与 provider 格式无关。"""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
#  抽象基类
# ---------------------------------------------------------------------------

class ToolFormatter(ABC):
    """工具调用格式的抽象层。

    六个核心职责：
    1. inject_tool_definitions: 将工具定义注入到 LLM 调用参数中
    2. parse_tool_calls: 从 LLM 响应中提取工具调用
    3. format_tool_result: 将工具执行结果格式化为消息
    4. build_assistant_message: LLM 回复后，构建写入对话历史的消息
    5. build_retry_hint: 纯文本响应需要重试时的提示文案
    6. message_to_llm: 将存储格式的消息转换为当前模式的 LLM API 格式（反序列化方向）
    """

    # 子类应覆盖此属性，标识当前 formatter 的模式名（"native" / "json"）。
    # build_llm_messages 根据此值判断消息是否需要跨模式转换。
    mode: str = ""

    @abstractmethod
    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """将工具定义注入到调用参数中。

        返回 (修改后的 system_prompt, API tools 参数或 None)。
        native 模式: prompt 不变, 返回 tools 列表。
        json 模式:   工具定义注入 prompt, 返回 None（不传 tools 参数）。
        """
        ...

    @abstractmethod
    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """从 LLM 响应中提取工具调用。

        native 模式: 直接转换 response.tool_calls。
        json 模式:   从 response.text 中解析特定标记块。
        """
        ...

    @abstractmethod
    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """将工具执行结果格式化为消息 dict。

        native 模式: 返回 {role: "user", content: ...}（当前行为）。
        json 模式:   返回带特定标记的文本消息。
        """
        ...

    def build_assistant_message(
        self, response: ProviderResponse, text: str, tool_calls: list,
    ) -> dict[str, Any]:
        """L1 存储层（通用）：content 只存纯文本，tool_calls 存为同级结构化属性。

        存储逻辑与格式无关，基类统一实现。子类负责 L2 读取（message_to_llm）
        时按各自格式将 tool_calls 还原为模型可消费的文本。

        Args:
            response: LLM 原始响应对象（未使用，保留签名兼容）。
            text: 响应中的纯文本部分。
            tool_calls: 响应中的工具调用列表（ToolCall 对象）。

        Returns:
            消息 dict，包含 role、content，以及可选的 tool_calls 列表。
        """
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": text.strip() if text else "",
        }
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": dict(tc.arguments or {})}
                for tc in tool_calls
            ]
        return msg

    @abstractmethod
    def build_retry_hint(self) -> str:
        """纯文本响应需要重试时的提示文案。"""
        ...

    @abstractmethod
    def message_to_llm(self, message_dict: dict) -> dict:
        """将存储格式的消息 dict 转换为 LLM API 可消费的格式（反序列化方向）。

        剥离 _ 开头的内部字段（_meta, _dynamic, _ephemeral 等），
        原样透传 role 和 content。不做跨模式 role 修补——
        那是 build_llm_messages 路由层的职责。
        """
        ...

    def get_plain_text(self, response: ProviderResponse) -> str | None:
        """从响应中提取纯文本部分，去除工具调用标记。

        默认实现直接返回 response.text。子类可覆盖。
        """
        return response.text


# ---------------------------------------------------------------------------
#  Native 模式：OpenAI function calling（现有行为）
# ---------------------------------------------------------------------------

class NativeToolFormatter(ToolFormatter):
    """使用 OpenAI 原生 function calling 的工具格式。

    这是当前系统的默认行为，不改变任何现有逻辑。
    """

    # 标识当前 formatter 模式，供 build_llm_messages 判断是否需要跨模式转换
    mode = "native"

    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """Native 模式：prompt 不变，原样返回 tools 列表。"""
        return system_prompt, tools if tools else None

    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """Native 模式：直接转换 ProviderResponse.tool_calls。"""
        if not response.tool_calls:
            return []
        return [
            ParsedToolCall(
                id=tc.id,
                name=tc.name,
                arguments=dict(tc.arguments or {}),
            )
            for tc in response.tool_calls
        ]

    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """Native 模式：与现有 ai_step 中的格式一致。"""
        return {
            "role": "user",
            "content": f'Tool result for "{call.name}":\n{result_text}',
        }

    def message_to_llm(self, message_dict: dict) -> dict:
        """Native（Fake Native）模式 L2 读取转换。

        从同级 tool_calls 字段读取工具调用，拼接为 [Tool call history record] 文本。
        兼容旧消息：没有 tool_calls 字段时，回退到 _meta.raw_parts。
        """
        clean = {k: v for k, v in message_dict.items()
                 if not k.startswith('_') and k != 'tool_calls'}

        # 优先从同级 tool_calls 字段读取（新格式）
        tool_calls = list(message_dict.get('tool_calls', []))

        # 回退：旧消息没有 tool_calls 字段，从 _meta.raw_parts 提取
        if not tool_calls:
            meta = message_dict.get('_meta', {})
            if isinstance(meta, dict):
                for part in meta.get('raw_parts', []):
                    if isinstance(part, dict) and 'tool_calls' in part:
                        tool_calls.extend(part['tool_calls'])

        if tool_calls:
            parts: list[str] = []
            original_content = clean.get('content', '')
            if original_content and original_content != '[tool_call]':
                parts.append(original_content)
            for tc in tool_calls:
                tc_args = json.dumps(tc.get('arguments', {}), ensure_ascii=False)
                parts.append(
                    f"[Tool call history record: {tc.get('name', '')} "
                    f"was executed with args: {tc_args}]"
                )
            clean['content'] = "\n".join(parts) or '[tool_call]'
            clean['role'] = 'user'

        return clean

    def build_retry_hint(self) -> str:
        """Native 模式：提示使用 finish 工具。
        改动：加强提示措辞，中英双语明确拒绝纯文本，提高模型遵从率。
        """
        return (
            "[SYSTEM] Your response was rejected because you did not use the finish tool. "
            "Plain text output is NOT accepted as a valid response. "
            "You MUST call the finish tool to submit your response, or use reply for intermediate progress. "
            "If you need more information, call finish with your question as text.\n\n"
            "请使用 finish 工具提交你的最终回复。直接输出纯文本不会被系统接受。"
        )


# ---------------------------------------------------------------------------
#  JSON 模式：文本内嵌工具调用（预留接口）
# ---------------------------------------------------------------------------

class JsonToolFormatter(ToolFormatter):
    """使用 JSON 文本块内嵌工具调用的格式。

    适用于不支持原生 function calling 的模型，
    或需要绕过 function calling 限制的场景。

    工具定义注入 system prompt，模型以 <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>>
    格式输出工具调用，引擎从文本中解析出调用请求。
    """

    # 标识当前 formatter 模式，供 build_llm_messages 判断是否需要跨模式转换
    mode = "json"

    CALL_OPEN_TAG = "<<<TOOL_CALL>>>"
    CALL_CLOSE_TAG = "<<<END_TOOL_CALL>>>"

    _TOOL_CALL_PATTERN = re.compile(
        r'<<<TOOL_CALL>>>\s*(\{.*?\})\s*<<<END_TOOL_CALL>>>',
        re.DOTALL,
    )
    _TOOL_CALL_UNCLOSED = re.compile(
        r'<<<TOOL_CALL>>>\s*(\{.*)',
        re.DOTALL,
    )

    # ------------------------------------------------------------------
    #  inject_tool_definitions
    # ------------------------------------------------------------------

    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """将工具定义转为 markdown 文本，追加到 system_prompt 末尾。

        返回 (修改后的 system_prompt, None)。不传 API tools 参数。
        """
        if not tools:
            return system_prompt, None

        lines: list[str] = [
            "",
            "",
            "[TOOL_USE_INSTRUCTIONS]",
            "",
            "你可以使用以下工具。需要调用工具时，在回复中输出以下格式的 JSON 块：",
            "",
            "<<<TOOL_CALL>>>",
            '{"name": "工具名称", "arguments": {参数对象}}',
            "<<<END_TOOL_CALL>>>",
            "",
            "规则：",
            "1. 每个工具调用必须用 <<<TOOL_CALL>>> 和 <<<END_TOOL_CALL>>> 包裹",
            "2. JSON 必须合法，name 是工具名称，arguments 是参数对象",
            "3. 可以在一条回复中调用多个工具，每个用独立的边界标记包裹",
            "4. 可以在工具调用前后附带文字说明",
            "5. 完成任务时，必须调用 finish 工具提交最终结果",
            "6. 发送中间进度时，使用 reply 工具",
            "",
            "## 可用工具",
            "",
        ]

        for tool in tools:
            lines.append(self._render_tool(tool))

        lines.append("[/TOOL_USE_INSTRUCTIONS]")

        return system_prompt + "\n".join(lines), None

    def _render_tool(self, tool: dict[str, Any]) -> str:
        """将单个工具 spec 渲染为 markdown 文本。

        支持两种输入格式：
        - OpenAI 格式: {type: "function", function: {name, description, parameters}}
        - Registry 格式: {name, description, input_schema}
        """
        if "function" in tool:
            func = tool["function"]
            name = func.get("name", "")
            description = func.get("description", "")
            schema = func.get("parameters", {})
        else:
            name = tool.get("name", "")
            description = tool.get("description", "")
            schema = tool.get("input_schema", {})

        parts: list[str] = [f"### {name}", ""]
        if description:
            parts.append(description)
            parts.append("")

        properties = schema.get("properties", {})
        required_set = set(schema.get("required", []))

        if properties:
            parts.append("参数：")
            for pname, pinfo in properties.items():
                ptype = pinfo.get("type", "any")
                req_label = "必需" if pname in required_set else "可选"
                pdesc = pinfo.get("description", "")
                parts.append(f"- {pname} ({ptype}, {req_label}): {pdesc}")
            parts.append("")

        example_args = self._generate_example_args(schema)
        example_json = json.dumps(
            {"name": name, "arguments": example_args}, ensure_ascii=False,
        )
        parts.append("示例：")
        parts.append("<<<TOOL_CALL>>>")
        parts.append(example_json)
        parts.append("<<<END_TOOL_CALL>>>")
        parts.append("")

        return "\n".join(parts)

    @staticmethod
    def _generate_example_args(schema: dict[str, Any]) -> dict[str, Any]:
        """根据 JSON Schema 自动生成示例参数（仅必需参数）。"""
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        result: dict[str, Any] = {}
        for pname, pinfo in props.items():
            if pname not in required:
                continue
            ptype = pinfo.get("type", "string")
            if "enum" in pinfo and pinfo["enum"]:
                result[pname] = pinfo["enum"][0]
            elif ptype == "string":
                result[pname] = ""
            elif ptype == "integer":
                result[pname] = 0
            elif ptype == "number":
                result[pname] = 0
            elif ptype == "boolean":
                result[pname] = False
            elif ptype == "array":
                result[pname] = []
            elif ptype == "object":
                result[pname] = {}
            else:
                result[pname] = ""
        return result

    # ------------------------------------------------------------------
    #  parse_tool_calls
    # ------------------------------------------------------------------

    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """从 response.text 中解析 <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>> 块。

        容错策略：
        1. 标准正则匹配所有完整块
        2. JSON 解析失败时尝试 raw_decode 提取第一个对象
        3. 检测到开始标记但无结束标记时，尝试到文本末尾截取

        正文处理策略：
        模型在工具调用之外输出的自由正文（plain text）不会被投递给用户，
        但会通过 build_assistant_message 保留在 assistant 消息的 content 字段中，
        使 LLM 在下一轮能看到自己说过的话（经 message_to_llm 重建为模型可消费格式）。
        用户可见的输出只通过 finish / reply 工具调用产生。
        """
        if not response.text:
            return []

        calls: list[ParsedToolCall] = []

        for match in self._TOOL_CALL_PATTERN.finditer(response.text):
            raw_json = match.group(1)
            parsed = self._try_parse_json(raw_json)
            if parsed is None:
                continue
            name = parsed.get("name")
            if not name:
                continue
            arguments = parsed.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ParsedToolCall(
                id=f"jt_{uuid.uuid4().hex[:8]}",
                name=str(name),
                arguments=arguments,
            ))

        # 回退：检测到开始标记但没有结束标记
        if not calls:
            match = self._TOOL_CALL_UNCLOSED.search(response.text)
            if match:
                raw_json = match.group(1).strip()
                parsed = self._try_parse_json(raw_json)
                if parsed and parsed.get("name"):
                    arguments = parsed.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {}
                    calls.append(ParsedToolCall(
                        id=f"jt_{uuid.uuid4().hex[:8]}",
                        name=str(parsed["name"]),
                        arguments=arguments,
                    ))

        # 自由正文不合成为 reply：正文保留在 build_assistant_message 存储的
        # assistant 消息 content 中，LLM 下轮可见，用户不可见。
        # 原 Fix 2 在此处把正文合成为 reply tool_call 追加到 calls 尾部，
        # 导致模型已显式调用 reply 时产生重复消息，现已删除。

        return calls

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | None:
        """尝试从文本中解析 JSON 对象，带容错。"""
        # 标准解析
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # 回退：用 raw_decode 提取第一个 JSON 对象
        try:
            decoder = json.JSONDecoder()
            idx = text.find("{")
            if idx >= 0:
                obj, _ = decoder.raw_decode(text, idx)
                if isinstance(obj, dict):
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass
        log.warning("JsonToolFormatter: JSON 解析失败: %.200s", text)
        return None

    def get_plain_text(self, response: ProviderResponse) -> str | None:
        """从响应中提取纯文本，去除工具调用块。"""
        if not response.text:
            return None
        plain = self._TOOL_CALL_PATTERN.sub("", response.text).strip()
        # 也清理未闭合的块
        plain = self._TOOL_CALL_UNCLOSED.sub("", plain).strip()
        return plain if plain else None

    # ------------------------------------------------------------------
    #  format_tool_result
    # ------------------------------------------------------------------

    def build_assistant_message(
        self, response: ProviderResponse, text: str, tool_calls: list,
    ) -> dict[str, Any]:
        """JSON 模式：content 只存剔除 <<<TOOL_CALL>>> 标记后的纯文本。

        resp.text 在 JSON 模式下包含完整的 tool call 标记块，
        直接存入 content 会导致 message_to_llm 重建时产生重复。
        此处用 get_plain_text 剥离标记，只保留自由正文部分。
        """
        plain = (self.get_plain_text(response) or "") if response else (text or "")
        return super().build_assistant_message(response, plain, tool_calls)

    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """格式化工具结果为 user 消息。与 NativeToolFormatter 保持一致。"""
        return {
            "role": "user",
            "content": f'Tool result for "{call.name}":\n{result_text}',
        }

    def message_to_llm(self, message_dict: dict) -> dict:
        """JSON 模式 L2 读取转换：从 tool_calls 重建 <<<TOOL_CALL>>> 块拼入 content。

        从同级 tool_calls 字段读取工具调用。
        兼容旧消息：没有 tool_calls 字段时，回退到 _meta.raw_parts。
        """
        clean = {k: v for k, v in message_dict.items()
                 if not k.startswith('_') and k != 'tool_calls'}

        # 优先从同级 tool_calls 字段读取（新格式）
        tool_calls = list(message_dict.get('tool_calls', []))

        # 回退：旧消息没有 tool_calls 字段，从 _meta.raw_parts 提取
        if not tool_calls:
            meta = message_dict.get('_meta', {})
            if isinstance(meta, dict):
                for part in meta.get('raw_parts', []):
                    if isinstance(part, dict) and 'tool_calls' in part:
                        tool_calls.extend(part['tool_calls'])

        if tool_calls:
            parts = []
            content = clean.get('content', '')
            if content:
                # 防御性清理：剥离可能残留的 tool call 标记，避免与下方重建的重复
                content = self._TOOL_CALL_PATTERN.sub("", content).strip()
                content = self._TOOL_CALL_UNCLOSED.sub("", content).strip()
                if content:
                    parts.append(content)
            for tc in tool_calls:
                tc_json = json.dumps(
                    {"name": tc.get("name", ""), "arguments": tc.get("arguments", {})},
                    ensure_ascii=False,
                )
                parts.append(f"<<<TOOL_CALL>>>\n{tc_json}\n<<<END_TOOL_CALL>>>")
            clean['content'] = "\n".join(parts)

        return clean

    def build_retry_hint(self) -> str:
        """JSON 模式：提示使用 <<<TOOL_CALL>>> 格式。
        改动：加强提示措辞，英文为主，直接给出可复制的工具调用模板，提高模型遵从率。
        """
        return (
            "[SYSTEM] Your response was rejected because you did not use the finish tool. "
            "Plain text output is NOT accepted. You MUST use the tool call format below:\n"
            "<<<TOOL_CALL>>>\n"
            '{"name": "finish", "arguments": {"text": "your response here"}}\n'
            "<<<END_TOOL_CALL>>>\n\n"
            "If you need more information from the user, put your question in the text parameter."
        )


# ---------------------------------------------------------------------------
#  工厂函数
# ---------------------------------------------------------------------------

def create_tool_formatter(mode: str = "native") -> ToolFormatter:
    """根据 mode 创建对应的 ToolFormatter 实例。

    Args:
        mode: "native" 或 "json"。默认 "native"。

    Returns:
        ToolFormatter 实例。
    """
    if mode == "json":
        return JsonToolFormatter()
    return NativeToolFormatter()


# ---------------------------------------------------------------------------
#  反序列化辅助：将存储消息列表转换为 LLM 可消费格式
# ---------------------------------------------------------------------------

def build_llm_messages(
    messages: list[dict],
    current_formatter: ToolFormatter,
) -> list[dict]:
    """将消息列表转换为 LLM 可消费的 dict 列表（反序列化方向）。

    三层管道路由：每条消息根据自身 _meta.tool_mode 选择对应的 formatter，
    每个 formatter 只处理自己模式的消息，内部零 if 补丁。

    路由规则：
    - 没有 _meta 或 tool_mode 的老消息 → NativeToolFormatter（老消息都是 native 格式）
    - tool_mode='native' → NativeToolFormatter
    - tool_mode='json' → JsonToolFormatter

    跳过 _ephemeral 消息（retry hint 等），保留 _dynamic 消息（动态上下文）。
    """
    result: list[dict] = []
    for msg in messages:
        # 跳过 ephemeral 消息（如 retry hint），它们不应进入 LLM 历史。
        # _dynamic 消息（动态上下文）保留——它们需要被 LLM 看到，
        # message_to_llm 会剥离 _dynamic 标记键但保留消息本身。
        if msg.get('_ephemeral'):
            continue

        # 根据消息自身记录的 tool_mode 选择 formatter
        msg_mode = ''
        meta = msg.get('_meta', {})
        if isinstance(meta, dict):
            msg_mode = meta.get('tool_mode', '')

        # 【重构】老消息（无 _meta / 无 tool_mode）默认视为 native 格式，
        # 确保它们始终由 NativeToolFormatter 透传，不会被错误地路由到
        # JsonToolFormatter 做不属于它的跨模式 role 修补。
        if not msg_mode:
            msg_mode = 'native'

        if msg_mode == current_formatter.mode:
            msg_formatter = current_formatter
        else:
            msg_formatter = create_tool_formatter(msg_mode)

        result.append(msg_formatter.message_to_llm(msg))
    return result
