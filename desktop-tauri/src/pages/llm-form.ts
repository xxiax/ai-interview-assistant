/**
 * LLM 表单"获取模型列表"预检的纯逻辑(便于 node:test 行为测试)。
 *
 * 预检规则:
 * - baseUrl 为空:任何模式都不发请求(没有可查询的端点);
 * - 新增模式(isEdit=false 且 apiKey 为空):后端无已存密钥可用,不发请求;
 * - 编辑模式且 apiKey 留空:只有 Base URL 未改变时才允许调用,
 *   由后端复用已保存密钥；修改 Base URL 后必须显式填写新密钥。
 */
export function checkFetchModelsPrecondition(input: {
  baseUrl: string
  apiKey: string
  isEdit: boolean
}): { ok: true } | { ok: false; reason: string } {
  if (!input.baseUrl.trim()) return { ok: false, reason: '请先填写 Base URL 再获取模型' }
  if (!input.apiKey.trim() && !input.isEdit) {
    return { ok: false, reason: '请先填写 API Key 再获取模型' }
  }
  return { ok: true }
}

export type ReasoningEffort = 'low' | 'medium' | 'high'

/**
 * 配置保存载荷的编辑语义:
 * - 新增(editTarget 为空)→ 原载荷提交,不带 config_id;
 * - 编辑 → 注入 editTarget.id 作为 config_id,后端按 id 更新,
 *   否则会新建一条重复配置。
 */
export function configSavePayload<B extends object>(
  body: B,
  editTarget: { id: number } | null | undefined
): B | (B & { config_id: number }) {
  return editTarget ? { ...body, config_id: editTarget.id } : body
}

/** 提交载荷。 */
export function llmSubmitData(input: {
  baseUrl: string
  apiKey: string
  model: string
  authField: string
  reasoningEffort?: ReasoningEffort
}): {
  base_url: string
  api_key: string
  model: string
  auth_field: string
  reasoning_effort: ReasoningEffort
} {
  return {
    base_url: input.baseUrl.trim(),
    api_key: input.apiKey,
    model: input.model.trim(),
    auth_field: input.authField,
    reasoning_effort: input.reasoningEffort ?? 'low'
  }
}
