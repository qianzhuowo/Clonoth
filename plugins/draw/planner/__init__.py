"""NovelAI 场景规划协议（移植自 LittleWhiteBox modules/draw/shared，Apache-2.0，见 NOTICE）。

纯函数层：不读文件、不访问网络、不依赖引擎运行时。
- profile：后端可消费的字段能力（center/interact/uc/post_prompt）。
- contract：submit_scene_plan 的校验合同、错误码、纠错文案、JSON 修复。
- tool：submit_scene_plan 的工具 schema 与字段说明覆写。
- layout：请求块布局（内置三阶段 + 自定义消息）的规范化与投影。
- handoff / frame / assembly / messages：预置交换消息链的模板与组装、消息链断言。
"""
