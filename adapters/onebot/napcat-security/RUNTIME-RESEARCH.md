# 2026-10-22 运行时加固实验记录（未部署）

## 状态

**不是可上线的完整修复。生产 NapCat 未停止、未重建、未修改登录数据。**

实验运行于无账号挂载的短期容器，固定基础 image ID：
`sha256:3a0c7ea39963d47206caa532787795b41367d975e5910025bd554190c51f4c89`。

代码：`runtime-policy.cjs`、`node-preload.cjs`、`research-main.cjs`、`run_research.py`。
公共 CA 证据：`ca-evidence.json`；运行前验证固定 SHA256 并重新计算各证书 DER/SPKI。
不下载或使用泄露私钥，不按测试域名做策略判断。

## 策略

- Node 使用官方 `tls.setDefaultCACertificates()` 从当前默认根集合中减去受影响公钥，绝不添加根。
- Chromium 检查 presented/validated 两条链的 SPKI；明确匹配则拒绝，其余交回 `callback(-3)` 默认验证。绝不返回 `0`。
- 默认会话及 `session-created` 新会话注册策略。
- 显式 Worker preload 单独安装 Node 策略，不能假定主线程修改自动传播。

## 两次实测

每路径包括12条问题链、GitHub/PyPI、过期/错误主机名/自签名负对照。

- 基线：主线程、Chromium 默认/新会话、默认 Worker 都接受6条非AAA问题链。
- 加固：Node主线程全部17项通过。
- 加固：Chromium默认/新会话均拒绝12条问题链，正常2站通过；负对照有间歇连接重置，保留为 inconclusive，不能计作通过。第二次新会话17项全通过，但默认会话仍有1项重置。
- 显式初始化Worker：第一次17项全通过；第二次错误主机名负对照超时，不能视作通过。
- **默认未初始化Worker：两次仍接受6条问题链。**

策略离线测试8项通过（加上已有探针/检查器共22项）。

服务器日志保存在 Git 之外的 root-only 检查目录（run1/、run2/）。

## 阻止生产切换的边界

1. NapCat实际包 `napcat.mjs` 确认包含 `worker_threads`、`child_process`、`utilityProcess.fork`；必须逐路径确保在任何TLS连接前安装策略，当前实验没有做到。进一步静态检查区分了用途：唯一直接 `new Worker` 构造的是 `worker/conoutSocketWorker.mjs` 控制台管道线程，不能仅凭它存在就声称正在承担TLS。更关键的是主业务由 `utilityProcess.fork` / `child_process.fork` 创建的 Worker **进程**执行，携带 `NAPCAT_WORKER_PROCESS=1`；父进程策略不能视为自动传入该进程。非utility分支还会显式指定 `execArgv: ['--no-sandbox']`，可能覆盖普通Node preload方案。需验证真正业务入口，而不只验证人工创建的线程。
2. QQ原生 `major.node` 在替换的JavaScript main之前已预加载，JS策略不能证明其私有网络库使用同一信任库，也无法撤回已经建立的TLS连接。
3. 调用方显式传入 `ca`、预先保存的安全上下文、后续替换Chromium验证回调都不在本实验保证内。
4. 公网坏证书负对照不稳定，必须补可控的本地TLS夹具，不能把 reset/timeout 改判成功。
5. 独立审查Agent因代理错误未实际完成审查；不存在“独立审查通过”的结论。

后续应先验证启动期跨线程/子进程引导和原生路径，或采用有官方支持、可实测的运行时更新方案，再制作生产启动封装及一致性QQ备份/独立回滚。不要直接把 `research-main.cjs` 挂到真实账号容器；它仅用于测试并会退出。
