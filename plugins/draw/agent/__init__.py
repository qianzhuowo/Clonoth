"""绘图 agent：在 before_prompt_build 钩子里接管 draw.novelai_planner 的独立运行时。

- protocol/：工具协议（原生 function calling / 文本 JSON 块），运行时内部统一用
  引擎的"存储格式"消息（assistant.tool_calls=[{id,name,arguments(dict)}]、role=tool），
  发给模型前由协议层转换。
- runtime.py：规划循环（模型调用 → 解析 → 校验 → 纠错重试 / 自定义工具执行 / 打断消费）。
- entry.py：钩子入口，组装材料、跑规划、调后端生图、返回 finish。
"""
