# AI 辅助面试工具

个人自用工具：面试中实时转写 + AI 生成答案提示，结束后生成复盘报告。

## 核心能力

- **实时转写**：面试中音频实时转文字，双端同步显示
- **实时答案**：自动识别面试官问题，LLM 生成回答要点
- **双端收音**：可选「手机 / 电脑 / 双端」，双端收音时音频带来源标识
- **收音切换**：面试中途可随时切换收音端，音频流无缝接续
- **双端同步**：PC 与手机看到同一场面试，实时同步
- **事后复盘**：面试结束后生成复盘报告（结合搜索资料）
- **配置管理**：统一配置页管理 LLM 与搜索 API

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI (Python) + SQLite |
| PC 端 | Electron + React + Ant Design + Tailwind CSS |
| 移动端 | Flutter (iOS + Android) |
| ASR | Groq Whisper API（免费） |
| LLM | 自有中转站（OpenAI 兼容格式） |
| 搜索 | 可选增强，无 key 降级纯 LLM |
| 部署 | Docker + 云服务器 |

## 文档

- [项目大纲](OUTLINE.md)
- [详细设计](docs/superpowers/specs/2026-08-13-ai-interview-assistant-design.md)

## 开发阶段

1. 后端核心（MVP）：FastAPI + WebSocket + ASR + LLM + 会话管理
2. PC 端（Electron）：收音 + 实时显示 + 会话控制
3. 移动端（Flutter）：收音 + 实时显示 + 会话控制
4. 双端同步完善：收音端切换 + 断线重连 + 双端收音
5. 复盘功能：搜索集成 + 复盘报告 + 历史会话
6. 部署上线：Docker + HTTPS/WSS + 双端打包
