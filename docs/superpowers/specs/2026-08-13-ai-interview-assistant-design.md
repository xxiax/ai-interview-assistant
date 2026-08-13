# AI 辅助面试工具 — 详细设计文档

> 日期：2026-08-13
> 状态：待评审
> 前置文档：`OUTLINE.md`（项目大纲）

---

## 1. 项目概述

### 1.1 定位
个人自用工具，AI 辅助面试。核心价值是**快**（实时性优先），同时尽量保证准确率。

### 1.2 核心能力
| 能力 | 说明 |
|------|------|
| 实时转写 | 面试中音频实时转文字，双端同步显示 |
| 实时答案 | 自动识别面试官问题，LLM 生成回答要点 |
| 双端收音 | 可选「手机 / 电脑 / 双端」，双端收音时音频带来源标识 |
| 收音切换 | 面试中途可随时切换收音端，音频流无缝接续 |
| 双端同步 | PC 与手机看到同一场面试，实时同步 |
| 事后复盘 | 面试结束后生成复盘报告（结合搜索资料） |
| 配置管理 | 统一配置页管理 LLM 与搜索 API |

### 1.3 双端收音的真实目的
**备用机制**：电脑端不可用时，手机端顶上。标识的意义是区分音频来源（哪端在收音），而非说话人分离。

---

## 2. 系统架构

### 2.1 总体架构（云端部署）

```
┌─────────────┐     WebSocket      ┌──────────────────┐
│  Electron   │◄──────────────────►│                  │
│  (PC 端)    │                    │   云端后端        │
└─────────────┘                    │  (FastAPI)        │
                                   │  · ASR 转写       │
┌─────────────┐     WebSocket      │  · LLM 生成答案   │
│  Flutter    │◄──────────────────►│  · 搜索          │
│  (手机端)    │   (互联网)         │  · 会话管理/同步  │
└─────────────┘                    └──────────────────┘
```

### 2.2 组件划分

| 组件 | 技术 | 职责 |
|------|------|------|
| 后端服务 | FastAPI (Python) | ASR 转写、LLM 生成、搜索、会话管理、双端同步 |
| PC 端 | Electron + React + antd + tailwind | 收音、显示转写/答案、控制会话、配置管理 |
| 移动端 | Flutter (iOS + Android) | 收音、显示转写/答案、控制会话、配置管理 |
| 数据库 | SQLite | 会话记录、转写历史、复盘报告、配置 |

### 2.3 数据流

```
音频流(PC/手机) → WebSocket → 后端
  → ASR 转写 → 转写文本
  → LLM 生成答案 → 答案文本
  → 广播给双端（WebSocket）
  → 存入数据库
```

---

## 3. 功能需求

### 3.1 会话管理
- 创建/结束一场面试会话
- 会话包含：标题、时间、收音端配置、转写记录、答案记录
- 双端加入同一会话（通过会话码/链接）
- 会话状态：`idle → recording → ended`

### 3.2 收音与音频流
- 收音端选项：`手机 | 电脑 | 双端`
- 双端收音时，音频带来源标识（`source: pc | mobile`）
- 中途可切换收音端，音频流无缝接续
- 音频格式：PCM/Opus 分片上传，WebSocket 流式传输
- 分片策略：每片 1-3 秒，兼顾实时性与转写准确率

### 3.3 实时转写（ASR）
- 免费云端 API：**Groq Whisper**（whisper-large-v3，快 + 准）
- 备选：Google Speech-to-Text（每月 60 分钟免费）
- 流式转写：音频分片 → 增量转写 → 实时显示
- 双端收音时，按来源标识区分转写段落
- 转写结果带时间戳，用于复盘对齐

### 3.4 实时答案生成
- 自动识别面试官问题（基于转写文本）
- LLM 生成回答要点（快，优先速度）
- 答案实时推送到双端显示
- 支持手动重新生成 / 补充信息
- 问题识别策略：检测问句（？/吗/呢/如何/为什么等），或转写停顿后触发

### 3.5 事后复盘
- 面试结束后，基于完整转写生成复盘报告
- 结合搜索资料（可选）生成更准确的答案
- 复盘报告包含：逐题问答、回答评估、改进建议

### 3.6 双端同步
- 转写、答案、会话状态实时同步（WebSocket 广播）
- 双端看到同一场面试
- 断线重连、消息补发（基于消息序号）

### 3.7 配置管理
- 统一配置页，管理 LLM 与搜索 API
- **LLM 配置**（类似 cc-switch）：
  - 请求地址（Base URL）
  - API Key
  - API 格式（OpenAI 兼容 / Anthropic 等）
  - 认证字段（Header 名称等）
  - 模型名称
- **搜索 API 配置**：
  - 搜索引擎选型（Google / Bing / SerpAPI）
  - API Key
  - 自定义参数
- 支持多套配置切换（类似 cc-switch 的多 provider 管理）
- 配置保存在后端，双端共享

---

## 4. 技术选型

### 4.1 后端
- **框架**：FastAPI (Python)
- **WebSocket**：FastAPI 原生支持
- **ASR**：Groq Whisper API（免费、快）
- **LLM**：自有中转站（OpenAI 兼容格式，类似 cc-switch 管理）
- **搜索**：可选增强，默认 LLM 直接回答；无 key 时降级为纯 LLM
  - Google Custom Search JSON API（免费 100 次/天）
  - Bing Web Search API（免费 1000 次/月）
  - SerpAPI（免费 100 次/月）
- **数据库**：SQLite（单用户够用）
- **部署**：Docker + 云服务器（2h4g 够用）

### 4.2 PC 端（Electron）
- Electron + **React**（前端框架）
- **UI**：Ant Design + Tailwind CSS
- 麦克风收音：Web Audio API / MediaRecorder
- WebSocket 客户端

### 4.3 移动端（Flutter）
- Flutter（跨平台 iOS/Android，**双平台都要**）
- 麦克风收音：Flutter 录音插件
- WebSocket 客户端

### 4.4 UI 组件库（PC 端 React）
- **Ant Design**：企业级组件库，表单/表格/配置页成熟
- **Tailwind CSS**：灵活的自定义样式，与 antd 互补
- 组合：antd 提供成熟组件 + tailwind 提供自定义样式

### 4.5 页面结构（PC 端）

```
┌─────────────────────────────────────┐
│  侧边栏导航          │  主内容区      │
│  ├ 面试（实时）      │              │
│  ├ 历史会话          │              │
│  └ 设置（配置页）    │              │
└─────────────────────────────────────┘
```

| 页面 | 内容 |
|------|------|
| **面试（实时）** | 转写流、答案卡片、收音端切换（手机/电脑/双端）、会话控制 |
| **历史会话** | 会话列表、复盘报告、历史转写查看 |
| **设置（配置页）** | LLM 配置 + 搜索 API 配置（统一一个页面） |

移动端（Flutter）页面结构：
- **面试页**：转写流 + 答案卡片 + 收音端切换
- **历史会话**：会话列表 + 复盘报告
- **设置页**：LLM + 搜索 API 配置（与 PC 端一致，双端共享配置）

---

## 5. 数据模型

### 5.1 数据库表

**sessions（会话）**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | TEXT (UUID) | 主键 |
| title | TEXT | 会话标题 |
| status | TEXT | idle / recording / ended |
| radio_mode | TEXT | pc / mobile / both |
| created_at | DATETIME | 创建时间 |
| ended_at | DATETIME | 结束时间 |

**transcripts（转写记录）**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| session_id | TEXT | 外键 → sessions |
| source | TEXT | pc / mobile |
| text | TEXT | 转写文本 |
| timestamp | DATETIME | 时间戳 |
| seq | INTEGER | 消息序号（用于断线补发） |

**answers（答案记录）**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| session_id | TEXT | 外键 → sessions |
| question | TEXT | 识别出的问题 |
| answer | TEXT | LLM 生成的答案 |
| source | TEXT | llm / search+llm |
| created_at | DATETIME | 创建时间 |

**configs（配置）**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键 |
| type | TEXT | llm / search |
| name | TEXT | 配置名称 |
| data | TEXT (JSON) | 配置内容（base_url, api_key, format, auth_field, model 等） |
| is_active | BOOLEAN | 是否当前启用 |

### 5.2 WebSocket 消息协议

**客户端 → 服务端**
```json
{ "type": "audio_chunk", "session_id": "...", "source": "pc", "data": "<base64>" }
{ "type": "set_radio_mode", "session_id": "...", "mode": "both" }
{ "type": "regenerate_answer", "session_id": "...", "question": "..." }
{ "type": "end_session", "session_id": "..." }
```

**服务端 → 客户端**
```json
{ "type": "transcript", "session_id": "...", "source": "pc", "text": "...", "seq": 12 }
{ "type": "answer", "session_id": "...", "question": "...", "answer": "...", "seq": 13 }
{ "type": "session_state", "session_id": "...", "status": "recording", "radio_mode": "both" }
{ "type": "error", "message": "..." }
```

---

## 6. 部署方案

### 6.1 云端部署
- 云服务器（2h4g）+ Docker 部署 FastAPI 后端
- 域名 + HTTPS（WebSocket 需要 WSS）
- 环境变量管理 API key

### 6.2 双端连接
- PC/手机通过互联网连接云端后端
- 手机用蜂窝网络也能用

---

## 7. 开发阶段规划

### 阶段 1：后端核心（MVP）
- FastAPI 服务 + WebSocket
- ASR 转写（Groq Whisper）
- LLM 答案生成
- 会话管理 + 数据库

### 阶段 2：PC 端（Electron）
- 收音 + 音频流上传
- 转写/答案实时显示
- 会话控制

### 阶段 3：移动端（Flutter）
- 收音 + 音频流上传
- 转写/答案实时显示
- 会话控制

### 阶段 4：双端同步完善
- 收音端切换（手机/电脑/双端）
- 断线重连、消息补发
- 双端收音 + 来源标识

### 阶段 5：复盘功能
- 搜索集成
- 复盘报告生成
- 历史会话查看

### 阶段 6：部署上线
- Docker 部署到云服务器
- HTTPS/WSS 配置
- 双端打包发布

---

## 8. 风险与注意事项

### 8.1 技术风险
- **ASR 免费额度**：Groq 免费额度有限，需监控用量
- **实时性**：云端转写有网络延迟，需优化分片策略
- **双端收音**：双端同时收音时音频同步/去重

### 8.2 隐私
- 面试音频/转写存云端，注意数据安全
- 个人使用，可考虑加密存储

### 8.3 合规
- 面试录音需注意当地法律法规（个人使用风险较低）

---

## 9. 待确认事项

- [x] 移动端目标平台：iOS + Android 都要
- [x] LLM API 来源：自有中转站（OpenAI 兼容格式，类似 cc-switch）
- [x] 搜索 API：无 key 时降级为纯 LLM 回答（默认方案）
- [x] 数据库选型：SQLite
- [x] 云服务器规格：2h4g 够用
- [x] PC 端技术栈：React + Ant Design + Tailwind CSS
- [ ] Groq Whisper 免费额度确认
- [ ] 搜索 API 是否接入（可选增强，不接也能用）
- [ ] 中转站 API 地址与 key 配置方式
