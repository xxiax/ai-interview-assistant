# 后端核心实施计划：归档状态

> 原计划日期：2026-08-13
> 最近按代码复核：2026-09-14
> 状态：核心后端已实现。本文件不再是可执行接口规范。

原始计划完成后，后端经历了安全、协议、可靠性、成本控制和 ASR 路由修订。旧计划中的部分示例曾包含无鉴权请求、明文 API Key、简化音频消息和不完整的恢复语义，已经不符合当前代码，因此不应复制到客户端或新服务中。当前工作树还包含尚未完成生产收敛的 FunASR 与代理改动；本归档只记录现状，不代表它们已经通过部署审查。

当前开发入口：

1. [后端权威 README](../../../backend/README.md)
2. [当前架构快照](../specs/2026-08-13-ai-interview-assistant-design.md)
3. `backend/app/` 实现
4. `backend/tests/` 行为证据

如果文档冲突，以代码和测试为准。

## 1. 原计划目标

后端核心要提供：

- 会话管理。
- 电脑/移动来源的实时音频入口。
- Groq Whisper 转写。
- LLM 答案生成。
- 可选搜索增强。
- 会后复盘。
- LLM/Search 配置管理。
- SQLite 持久化。

这些是 2026-08-13 的原始目标。当前实现默认使用 `AI_ASR_ENGINE=funasr` 的 WAV 路径，多模态 LLM 和 Groq 作为可选引擎保留，并支持 LLM/Search/ASR/Network 四类配置；这些扩展不是原计划当时的承诺。

## 2. 完成情况

| 工作项 | 状态 | 当前实现 |
|---|---|---|
| FastAPI 应用与生命周期 | 完成 | `app/main.py` |
| SQLite schema 与迁移 | 完成 | `app/db.py` |
| 会话状态机 | 完成 | `idle -> recording -> ended` |
| REST 会话 API | 完成并扩展 | 创建、读取、开始、结束、历史/对账，以及 idle/ended 会话删除 |
| WebSocket | 完成并升级 | 首包认证、`v:1`、事件重放、严格模型 |
| 音频处理 | 完成并升级 | UUID 幂等、摘要冲突、排序、背压、对账、重试、`cancel_audio_source` 水位取消和当前任务中止 |
| ASR | 完成但仍需生产收敛 | 默认 FunASR WAV 路径、可选多模态 LLM、Groq 非 WAV 路径和全路径 `ffprobe` 校验 |
| 问题识别 | 已移除 | 实时链路不再判断问题，FunASR partial 只显示，final 直接驱动 AI |
| LLM 答案 | 完成并加固 | OpenAI-compatible、输入隔离、输出/成本上限 |
| 搜索 | 完成 | Google，可选且故障降级；Bing 新配置已退役；只是搜索增强，不是向量 RAG |
| 复盘 | 完成并升级 | ended 限制、输入上限、幂等、single-flight |
| 配置管理 | 完成并扩展 | LLM/Search/ASR/Network 四类配置，Fernet 密钥存储、脱敏读取、激活和删除；Network 秘密模型仍需收敛 |
| 认证与限流 | 完成 | REST Bearer、WS Token、Origin/Host 和速率限制 |
| 成本控制 | 完成 | 并发门 + SQLite 持久化预算 |
| 备份恢复 | 完成代码与测试 | `backend/scripts/` |
| 自动化测试 | 当前通过 | 2026-09-14 Python 3.10：后端 `260 passed`，`ruff` 通过；目标 Python 3.12 需重新复现当前测试集 |
| 容器生产实测 | 暂缓 | 资产存在，Docker daemon 不可用时未实测 |

## 3. 原计划之后形成的关键约束

### 认证

- 所有业务 REST API 都需要 `Authorization: Bearer <token>`。
- WebSocket 不能在 URL 中携带业务 Token；连接后的第一条消息必须是 `authenticate`。
- Token 至少 32 字符。

### 音频协议

当前 `audio_chunk` 必须包含：

- `v: 1`
- UUID `chunk_id`
- `source`
- `codec`
- 每来源连续 `chunk_seq`
- 带时区 `captured_at`
- `duration_ms`
- Base64 `data`

只发送 `source + data` 的旧客户端不会工作。

当前 Tauri 客户端只采集电脑播放声，由 WASAPI loopback 送入后端并标记为 `source=pc`；当前不启用麦克风，也不做说话人分离。

当前 LivePage 会把后端/Tauri 命令产生的标准 `Error` 或原始字符串统一归一化后显示；字符串错误不再因为读取不存在的 `.message` 而变成空 toast。

当前协议还包含：

~~~json
{"v":1,"type":"cancel_audio_source","source":"pc","through_chunk_seq":17,"reason":"capture_stopped"}
~~~

该消息以包含式水位取消单一来源的 queued、带可重试错误的 failed 和当前 ASR 任务，并持久化 cancelled ACK；原因只允许 `capture_stopped`、`source_disabled`。同水位重复请求幂等，水位内迟到旧片直接取消，更高序号可用于下一轮采集。

后端取消水位本身仍是进程内状态。已经落库 cancelled 的分片可跨重启恢复，但尚未 reserve 的空洞取消范围不会跨后端重启；这属于需要部署故障测试覆盖的边界。

### 事件恢复

- `session_state`、`chunk_ack`、`transcript`、`answer` 持久化。
- 客户端必须保存 `event_id` 并处理重放。
- 音频还必须用 `chunk_id/chunk_seq` 单独对账。
- 服务重启/关闭中断的分片可以用同一身份重试，不是永久失败终态。
- Tauri 客户端停止、切到 `mobile` 或退出实时页时先关闭 capture gate，再发送取消水位；页面重进、重连或应用重启不自动恢复旧采集。
- `processing_failed` 在协议层可重试，但当前 Tauri 对同一分片累计 3 次后停止自动重试，且 `queued` ACK 不清空失败计数；首个 ASR 故障会熔断采集并取消积压。

### 配置秘密

- API Key 从普通配置 JSON 中拆出，使用 Fernet 加密。
- 读取配置不会返回 API Key。
- LLM Base URL 经过 HTTPS、DNS 和私网 SSRF 校验；当前代码不维护额外的主机白名单。
- 当前配置类型已经扩展为 `llm`、`search`、`asr`、`network`，并支持删除接口。
- Network 配置的请求模型仍要求 `api_key`，但运行时代理代码只消费 `proxy_url`；不要把代理凭据嵌入公开 URL 数据，这部分不是已完成的生产秘密设计。

### 当前 ASR 路由

- 默认 `AI_ASR_ENGINE=funasr` 且 `AI_FUNASR_STREAM=true`；WAV 按 `session + source` 复用 FunASR WebSocket，并按语音段维持开放式 utterance（段首 `start`、中间只推 PCM、`speech_end` 才 `stop`）。整题合并由客户端 `speech_end` + 后端 `QuestionThread` 完成：网关的累计 `partial` 每长出一截就让问题 `revision += 1` 并立即并发提交一次 LLM SSE 请求（会话内默认并发 3，以 `request_id` 隔离并在前端各占**一张卡内的一段答案**、以 `thread_id` 聚合成一张卡），宽限期到期后只把最新完成的一版写入 `answers`。只有显式设置 `AI_ASR_ENGINE=llm` 时才使用激活 LLM 配置的主 `model` 做多模态转写。
- 激活的 ASR 配置选择 Whisper 模型或配置名称包含 `groq` 时，WAV 改走 Groq；旧 Groq/Paraformer 组合会使用 `whisper-large-v3`。受支持的非 WAV 编码当前也走 Groq。
- 所有路径先经过 `ffprobe` 容器、编码、音轨和时长校验。
- ASR 代理优先级为 `AI_OUTBOUND_PROXY`、active Network 配置、Windows 当前用户系统代理；兼容系统代理单地址和 `http=...;https=...` 写法。
- FunASR 地址由 `AI_FUNASR_URL` 配置；`AI_FUNASR_TOKEN` 必须通过 Secret Manager 注入，源码不再保留凭据回退值。

### 搜索增强，不是向量 RAG

- Google 搜索最多返回 5 条经过形状和长度限制的结果供 LLM 参考；Bing 新配置已退役。
- 当前没有文档摄取、Embedding、向量数据库、召回器或引用生成管线。
- 自动回答默认不搜索；重新生成和复盘只有显式启用时才搜索，普通搜索失败降级为纯 LLM。

### 成本

- LLM/Search/ASR 都有限并发。
- 用量预算持久化，不能通过重启进程清零。
- 复盘相同内容幂等，避免重复付费。

## 4. 当前测试验收

在 `backend/`：

~~~powershell
python -m pytest -q
~~~

2026-09-14 对当前工作树在 Python 3.10 的结果为：

~~~text
260 passed
~~~

旧文档中的较小测试数字均属于更早快照。本轮 `ruff` 已通过但未生成覆盖率报告；目标 Python 3.12 仍需重新运行当前 260 项测试。

测试覆盖的验收面包括：

- 启动安全配置和匿名健康端点。
- REST/WS 认证与协议拒绝。
- 状态机和 ended 写屏障。
- 分片幂等、乱序、缺口、背压、取消水位、当前任务取消、迟到旧片和恢复。
- `speech_end` 边界校验、来源限制，以及问题线程累积、`revision` 递增、旧版不覆盖新版、宽限关闭后只落库最新一版、全部失败不落库。
- 事件重放和连接水位。
- 媒体伪造检测、FunASR 假 WebSocket 协议、Groq/Paraformer 旧配置兼容、Windows 系统代理解析和 Groq 回退。
- LLM/Search 输入输出边界与降级。
- 四类配置的秘密处理、激活/删除、SSRF、预算和备份恢复。

该测试结果不替代真实 FunASR、Groq、LLM、Google Search、HTTP/SOCKS 代理、客户端或容器端到端测试。

## 5. 后端仍可继续改进的事项

这些不是原计划遗漏的“未完成核心功能”，而是后续成熟化工作：

- 定期轮换由 `AI_FUNASR_TOKEN` 注入的 FunASR 凭据，禁止在文档或日志中复制原值。
- 在真实镜像中复核 `python-socks` 等代理运行时依赖已按生产锁文件和哈希安装。
- 重新设计 Network 配置的秘密字段、代理 URL 凭据规则和配置失败后是否允许静默直连。
- 对真实 FunASR/Groq/LLM/Google Search/代理做受控集成测试。
- 在真机长时间面试中调参 `AI_QUESTION_THREAD_GRACE_SECONDS`（6 秒）与客户端静音阈值，并统一 `security.py` 的 0.5–30 启动校验与 `realtime._question_grace_seconds()` 的 0.1 运行时下限。
- 让问题线程状态（`thread_id`、待完成 revision、最新完成结果）在后端重启后可恢复，或至少向客户端明确暴露"未入库分段已丢失"。
- 在真实客户端验证停止、切 mobile、退出页面和服务端取消并发，确认自动化覆盖的队列/outstanding 计数修复在长时间运行中不泄漏。
- 增加结构化日志、指标、追踪和告警。
- 建立显式数据库迁移版本。
- 在多用户需求出现时重新设计身份、权限和数据所有权。
- 在多实例需求出现时迁移共享数据库、消息总线和分布式限流。
- 在目标 Python 3.12 重新运行当前测试、覆盖率与静态检查。
- 主要客户端完成后，恢复容器构建、TLS、备份恢复和故障演练；在真实验证前保持“部署资产存在、上线未实证”的状态。

## 6. 使用本归档的规则

- 可以用它了解后端最初的建设目标。
- 不要从旧 Git 版本复制其中历史代码示例。
- 不要据此推断当前请求字段、响应字段或错误语义。
- 新实现计划必须从 [backend/README.md](../../../backend/README.md) 和当前测试重新生成。
