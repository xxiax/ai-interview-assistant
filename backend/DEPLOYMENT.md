# 后端部署参考（当前暂缓）

## 当前结论

容器和生产上线不是当前开发优先级。仓库已经准备 Docker、Compose、Caddy、健康检查以及 SQLite 备份/恢复资产，但截至 2026-09-14：

2026-09-14 使用本地 Python 3.10 对当前工作树执行完整后端自动化测试，结果为 **`260 passed`**，`ruff` 通过。本轮没有生成覆盖率报告；正式发布仍需在目标 Python 3.12 复现当前测试集。
- 当前环境的 Docker daemon 不可用，因此没有完成真实镜像构建、Compose 启动、Caddy TLS、容器健康检查或公网 WSS 验证。
- 代理秘密模型和真实外部服务链路仍有部署前必须解决的收敛项。
- 本文件是未来恢复上线工作时的操作基线，不是“已经可以直接上线”的证明。

主要功能完成前，不建议继续扩展容器编排。当前后端契约见 [README.md](README.md)。

## 已存在的部署资产

| 文件 | 当前用途 |
|---|---|
| `Dockerfile` | Python 3.12.11、非 root UID 10001、ffmpeg/ffprobe、从生产锁文件安装依赖、单 worker |
| `compose.prod.yml` | API、Caddy、持久卷、私有网络、健康检查和一次性备份工具 |
| `Caddyfile` | 域名入口、自动 TLS 和反向代理 |
| `.env.production.example` | 生产变量模板，不包含真实秘密 |
| `requirements.lock` | 当前生产依赖及哈希，已包含 `python-socks`；仍需在真实镜像构建中复核安装结果 |
| `scripts/backup_sqlite.py` | 使用 SQLite backup API 创建一致性备份 |
| `scripts/restore_sqlite.py` | 校验备份并恢复到目标数据库 |
| `scripts/healthcheck.py` | 使用生产 Host 与 HTTPS 代理头执行容器内就绪探针 |

代码层面的安全意图包括只读根文件系统、移除 capabilities、`no-new-privileges`、内部网络和 Caddy TLS。这些配置仍需在真实宿主机上验证。

## 当前阻塞项

以下问题解决前，不应把当前工作树构建为可分发的生产镜像：

1. 当前默认使用 FunASR 转写；只有设置 `AI_ASR_ENGINE=llm` 时才启用多模态 LLM。FunASR Token 只从 `AI_FUNASR_TOKEN` 读取，源码没有默认凭据。
2. `python-socks` 已进入生产锁文件和哈希集合，但仍需在真实镜像构建和代理端到端测试中确认 SOCKS 路径可用。
3. LLM 配置的实时答案/复盘思考强度默认为 `low`，可选 `medium`/`high`；仅 GPT-5、o1、o3、o4 系列模型发送 `reasoning_effort`，普通 OpenAI-compatible 模型保持 `temperature` 兼容路径。Network 配置当前要求保存一个加密 `api_key`，但运行时只使用公开的 `proxy_url`；代理秘密、URL 内嵌凭据、失败后是否允许直连等策略尚未完成生产设计。当前 FunASR 同机隧道使用 `ws://127.0.0.1:10096/ws`。
4. 当前 ASR 路径为：默认 `AI_ASR_ENGINE=funasr` 且 `AI_FUNASR_STREAM=true`，WAV 按 `session + source` 复用 FunASR WebSocket，一个语音段对应一个 utterance：段首一次 `start`，中间分片只推裸 PCM，客户端 `speech_end` 才 `stop` 取段末 `final`。累计 `partial` 经几何节流后形成多个 LLM revision；它们互不取消，在会话并发上限内同时运行，线程关闭后只落库最高成功版本。容量评估必须按“每问题 N 次 LLM 调用”而不是“一次”计算，N 由倍率和句读密度决定。只有 `AI_ASR_ENGINE=llm` 才复用激活 LLM 主模型做多模态转写，`groq` 或非 WAV 编码走 Groq。
5. ASR 代理优先级为 `AI_OUTBOUND_PROXY`、active Network 配置、Windows 当前用户系统代理；Linux 容器没有 Windows 代理回退，生产必须显式配置环境变量或 Network 配置。代理单地址和 `http=...;https=...` 解析已有自动化测试，但真实生产代理仍未验收。
6. Google 只是可选搜索增强，不是向量 RAG；Bing 新配置已退役，部署验收不应假设存在本地知识库、Embedding 或向量数据库。
7. `cancel_audio_source` 的水位目前只保存在后端进程内；已落库 cancelled 状态可恢复，但尚未 reserve 的空洞取消范围不会跨服务重启。部署故障测试必须覆盖取消与重启并发，不能只验证正常重连。
8. 问题线程（`thread_id`、累积问题、待完成 revision、最新完成结果、宽限定时器）同样只在进程内。滚动重启会丢失所有未关闭线程尚未入库的分段答案，已入库 `answer` 不受影响。这也是必须 `--workers 1` 的原因之一。

## 恢复部署工作前的准入条件

在执行上线步骤前，应先满足：

1. 主要客户端已能完成真实的创建会话、收音、重连、对账、停止取消、结束和复盘流程；Windows 默认播放设备 WASAPI loopback、腾讯会议对方声音和静音不成片仍需在目标机器验收。
2. 真实 FunASR、Groq、LLM、可选 Google Search 和代理配置仍需做受控端到端测试。
3. 生产域名、DNS、服务器、防火墙、秘密管理和数据保留策略已确定。
4. Docker Engine 与 Compose 可用，并有权限读取部署环境文件。
5. 已决定 SQLite 数据卷、备份卷和异机备份位置。
6. 已接受单 worker、单实例、单 Token 架构限制；否则先完成架构升级。
7. FunASR Token 不得存在于源码；`AI_FUNASR_TOKEN` 必须通过秘密管理注入并按供应商流程轮换，不要把 Token 写入日志或文档。所有运行时依赖已纳入带哈希的生产锁文件。
8. 当前工作树已在目标 Python 3.12 运行测试并通过静态检查；正式发布仍必须从干净提交复现同一结果。

FunASR 与后端位于同一台物理机器不一定共享同一个 loopback：后端直接作为宿主机进程运行时使用 `ws://127.0.0.1:10096/ws`；后端位于 Compose 容器而 SSH 隧道/FunASR 位于宿主机时，必须使用 `ws://host.docker.internal:10096/ws`。两个独立容器则应使用 Compose 服务名。

## 未来首次部署流程

### 1. 准备域名和网络

- 将域名 A/AAAA 记录指向服务器。
- 对公网只开放 TCP 80/443 和需要时的 UDP 443。
- 不直接暴露 Uvicorn 的 8000 端口。

### 2. 准备秘密

从 `.env.production.example` 复制一份到服务器的秘密管理器或权限受限文件，至少设置：

- `AI_PUBLIC_HOST`
- `AI_ALLOWED_ORIGINS`
- `AI_AUTH_TOKEN`
- `AI_CONFIG_ENCRYPTION_KEY`
- `AI_ASR_ENGINE`
- `AI_FUNASR_TOKEN`（FunASR 路径必填）
- `GROQ_API_KEY`

访问 Token 至少 32 字符。Fernet 密钥一旦用于加密配置，不得在没有迁移密文的情况下随意更换。FunASR 令牌不得复制到源码、工单或日志。

### 3. 构建和启动

在 `backend/` 目录执行：

~~~bash
docker compose --env-file /secure/path/ai-interview.env -f compose.prod.yml build --pull
docker compose --env-file /secure/path/ai-interview.env -f compose.prod.yml up -d
docker compose --env-file /secure/path/ai-interview.env -f compose.prod.yml ps
~~~

构建前必须重新生成并审查 `requirements.lock`，确认代理路径所需的运行时依赖已带版本与哈希进入锁文件。随后检查构建日志确实使用该锁文件和哈希校验，并确认 API 容器只启动一个 Uvicorn worker。

### 4. 验证

至少验证：

~~~bash
curl --fail https://api.example.com/health/live
curl --fail https://api.example.com/health/ready
~~~

还必须从真实客户端网络完成：

- Bearer REST 请求。
- 四类配置 `llm/search/asr/network` 的保存、脱敏读取、激活和删除。
- idle/ended 会话删除，以及 recording 会话删除被拒绝。
- `wss://<host>/ws/{session_id}` 首包认证。
- 音频上传、`chunk_ack`、转写、答案和 `event_id` 重放。
- 电脑播放声由 WASAPI loopback 单独采集，并确认目标播放端点可被稳定捕获。
- 人为触发一个由 Tauri 以字符串返回的命令错误，确认 LivePage 归一化后显示完整非空 toast，而不是依赖 `.message`。
- `cancel_audio_source(source, through_chunk_seq, reason)` 对 queued、可重试 failed 和当前 ASR 任务的取消，以及迟到旧分片不复活。
- `AI_ASR_ENGINE=funasr` 的默认 WAV 路径、可选多模态 LLM、Groq 非 WAV 路径，以及 HTTP/SOCKS 代理路径。
- 名称为 Groq、模型误存 Paraformer 的旧配置兼容，以及显式代理优先级；Windows 本机测试还应覆盖系统代理读取，容器只验收显式代理配置。
- Google 搜索增强启用、未启用和上游失败降级；确认系统没有依赖不存在的向量 RAG 组件。
- 服务重启后的分片对账和重试。
- Caddy 证书签发/续期、Host/Origin 白名单和安全响应头。

仅健康端点成功不足以证明业务可上线。

## 备份

不要在服务运行时直接复制 `interview.db`、`-wal`、`-shm` 文件组合。使用 SQLite backup API：

~~~bash
docker compose --env-file /secure/path/ai-interview.env -f compose.prod.yml --profile tools run --rm backup
~~~

Compose 默认保留 14 份，可通过 `AI_BACKUP_KEEP` 调整。当前 backup 服务把备份写到 Docker `backups` 卷；正式上线前还需把备份同步到故障域之外，并定期验证可恢复性。

本地直接调用脚本的形式：

~~~bash
python scripts/backup_sqlite.py --source /data/interview.db --destination-dir /backups --keep 14
~~~

脚本会：

- 使用 SQLite backup API 生成一致快照。
- 对备份执行 `PRAGMA quick_check`。
- `fsync` 完成的文件。
- 按时间清理超过保留数量的旧备份。

## 恢复

恢复属于有状态、可能覆盖数据的操作，必须先停止 API，并保留当前数据库的额外备份。

示例：

~~~bash
docker compose --env-file /secure/path/ai-interview.env -f compose.prod.yml stop api
python scripts/restore_sqlite.py --backup /backups/interview-YYYYMMDDTHHMMSSZ.db --destination /data/interview.db --force
docker compose --env-file /secure/path/ai-interview.env -f compose.prod.yml start api
~~~

恢复脚本会先校验源备份，使用 backup API 写入临时目标，再执行 `quick_check`。实际在容器卷中操作时，需要使用拥有对应卷挂载的受控工具容器；不要把示例宿主机路径直接照搬。

恢复后应验证：

1. `/health/ready`。
2. 会话、转写、答案、复盘和配置元数据可读。
3. Fernet 密钥仍能解密 `config_secrets`。
4. 事件游标和音频分片状态能正常对账。

## 升级与回滚

未来部署新版本时：

1. 先生成并异机保存数据库备份。
2. 构建不可变镜像并记录镜像摘要。
3. 在同等配置的预发布环境运行测试和业务冒烟。
4. 停止旧 API、启动新版本并观察启动迁移。
5. 验证健康检查、WebSocket 和关键业务流。

当前数据库迁移在应用启动时执行，主要是新增表/列/索引和数据修复，没有独立迁移版本框架。任何未来破坏性 schema 变更都必须先引入显式迁移和回滚方案。

如果新版本失败，应回到上一镜像；只有确认 schema/data 已被不兼容地修改时，才使用经过验证的备份恢复。不要把回滚镜像和恢复数据库混为同一步骤。

## 生产限制

- 只能运行一个 API worker 和一个有状态实例。
- 进程内 WebSocket 广播、答案/音频队列和滑动窗口限流不能跨实例共享。
- SQLite 数据卷是单点；备份不等于高可用。
- 全局 Bearer Token 泄漏会暴露全部业务接口。
- 没有内置用户审计、指标平台、集中日志或告警。
- `/health/ready` 检查 SQLite，并在 FunASR 模式下探测 FunASR TCP/TLS 端点；它不会发送 Token 或业务音频，不能证明鉴权、识别质量、代理或完整 ASR/LLM/Search 链路正常。
- 容器健康并不代表实时音频链路健康。
- Google 搜索增强不是向量 RAG，当前没有文档库、Embedding 服务或向量数据库可用性检查。

## 恢复上线审查时必须补做

- `docker build` 与 `docker compose config`/`up` 实测。
- 镜像漏洞、SBOM、基础镜像、锁文件和运行时导入完整性复核，特别是 HTTP/SOCKS 代理依赖。
- 非 root、只读文件系统、capabilities 和网络隔离验证。
- TLS、WSS、代理头、Host、Origin、CORS 和请求体限制验证。
- FunASR 凭据外置与轮换、Groq 回退、四类配置和代理失败策略验证。
- 备份、异机复制、恢复和灾难演练。
- 长连接、音频队列、服务重启、磁盘满、第三方超时和预算耗尽压测。
- 日志脱敏、监控、告警和密钥轮换流程。
- 在目标 Python 3.12 重新运行当前 `228` 项测试、覆盖率和 `ruff`，而不是引用更早的历史结果。

完成这些验证前，部署状态应继续标记为“资产已准备、上线未验证”。
