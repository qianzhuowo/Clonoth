# NapCat 容器安全构建与验证

本目录管理 **候选镜像**，不是自动升级器。不得因为 Docker 构建成功或系统 curl
测试通过，就替换运行中的 NapCat。三个可测网络栈全部通过后才可安排生产切换。

## 2026-10-22 实测：禁止直接上线此候选

已构建镜像 `sha256:3a0c7ea39963d47206caa532787795b41367d975e5910025bd554190c51f4c89`：

| 网络栈 | 12 条问题链 | GitHub / PyPI | 结果 |
|---|---|---|---|
| 系统 curl | 全部因证书信任拒绝 | 通过 | 通过 |
| QQ 内嵌 Node HTTPS | 6 条拒绝，6 条仍 HTTP 200 | 通过 | **失败** |
| QQ 内嵌 Chromium net | 6 条拒绝，6 条仍 HTTP 200 | 通过 | **失败** |

仍被接受的是全部非 `-aaa` 的 G3B/G3C DV/OV/EV 测试链。
生产容器没有被此候选替换。必须进一步解决应用运行时信任问题，重新验证，
不能把 `compose.yaml` 当作已通过验收的生产方案。

## 固定制品

- NapCat 上游发布：`v4.18.41`
- 上游镜像：`mlikiowa/napcat-docker@sha256:b101b8968cc31ae428ec0f8e117386e4ced7773b2d8226ed9f7ddfcec55f1b7e`
- 镜像内 QQ：`3.2.34-53644`；实测 Electron `40.0.0`、Node `24.11.1`。
- `NapCat.Shell.zip` SHA256：`6801f93e533846cea05f4f531874a0e84371ab2331c5585b57cd7e3b869cf96b`
- Ubuntu CA 包：`20260816ubuntu0.22.04.1`，SHA256：
  `29ca037f49f7a4b41d67ad72946c2e541f3ae52d8f6ab1495d2af09aa391144b`。

镜像摘要来自 Docker Registry；NapCat 制品摘要来自 GitHub release asset，证书包摘要
核对了 Ubuntu 签名的软件包索引。固定基础镜像和包字节避免 `latest` 或无版本 `apt upgrade`
漂移；不承诺 Docker 层时间戳逐位可复现。上游制品若下架，构建应失败而非改用未知版本。

## 构建与验收（Linux 服务器）

```sh
docker build --pull=false -t clonoth-napcat:4.18.41-ca20261022 .
IMAGE_ID=$(docker image inspect --format '{{.Id}}' clonoth-napcat:4.18.41-ca20261022)
# 输出目录应在 Git 之外；记录中不包含真实账号配置。
install -d -m 700 "$VALIDATION_DIR"   # Git 之外的 root-only 目录
python3 verify_image.py --image "$IMAGE_ID" --output "$VALIDATION_DIR"
```

检查器创建无账号挂载的短期容器，不启动 NapCat 登录模块，分别检查：

1. 系统 curl 默认 CA；
2. QQ 同一可执行文件内的 Node `https` 默认校验；
3. QQ 同一可执行文件内的 Chromium `net.request` 默认校验。

每层检查公开的 12 条问题证书链及 GitHub/PyPI 两个正常站点。错误域名、证书过期、
DNS/网络故障或超时不能证明修复。JSON 报告记录不可变 image ID，失败返回非零。
探针不使用真实 QQ 数据，不能证明腾讯私有原生网络库和实际登录后的所有调用均安全。

QQ 不支持用 `ELECTRON_RUN_AS_NODE=1` 获得独立 Node 模式；检查器将只读探针挂到
隔离容器的 `/opt/QQ/resources/app/loadNapCat.js`，配合独立 Xvfb 执行，退出后删除测试容器
及其匿名卷。`runtime-version.cjs` 采用相同方法仅读取版本，建议关闭网络运行。

**系统 CA 更新不会自动替换内嵌 Node/Chromium 的信任库。** 不关闭证书校验，
不把 `NODE_EXTRA_CA_CERTS` 视为撤销信任措施，不强行替换 QQ 内嵌 Electron。

## 生产切换前的人工检查清单

`compose.yaml` 是无公网端口的部署模板，必须提供经过验证的不可变 image ID 和现有账号。
不要向 Git 提交账号、token、`.env`、备份或实际 OneBot/QQ 配置。

- 保存旧容器完整 inspect（含环境秘密，必须 root-only）、旧 image ID 和镜像。
- 准备独立于 SSH 的回滚流程；不要用 `docker compose down -v` 删除账号数据。
- 短暂停止原容器后，一致性备份 `$NAPCAT_DATA_DIR/config`、`$NAPCAT_DATA_DIR/qq` 及适配器 `.env`。
- 用 `secrets.token_urlsafe(48)` 等生成随机 token，在每个启用的反向 WS 配置中设置
  `token`，并在适配器 `.env` 设置相同的 `ONEBOT_ACCESS_TOKEN`；文件权限限定为 0600。
- 所有配置完成且经过验证后，先重启适配器，再启动新容器；禁止两容器共用账号目录同时运行。
- 无 token 和错误 token 的握手必须被拒，正确反向 WS 必须恢复。测试不能伪造真实消息事件。
- 保持登录数据挂载、附件只读挂载和不发布端口；默认 bridge 方案需配合宿主防火墙。
- 如需回滚，先停止新容器，再恢复配置/数据与旧容器；旧镜像可能重新引入证书风险，必须明确记录。
- 登录状态恢复不能保证；若要求扫码，等待用户配合，不删除登录数据或反复登录。
- QQ 收发必须在用户指定私聊/测试群验证；仅有 WS 连接不代表收发验收完成。

## 本地离线测试

```sh
pytest -q tests/test_napcat_*.py
```

实际部署状态与验证结果请见对应服务器 root-only 检查目录，不以本文件表示已上线。
