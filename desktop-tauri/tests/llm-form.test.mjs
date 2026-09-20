/**
 * 任务 B 回归测试:LLM 获取模型预检 + 全局提示词设置入口。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const { checkFetchModelsPrecondition, configSavePayload, llmSubmitData } = await import('../src/pages/llm-form.ts')
const settingsSource = await readFile(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8')
const bridgeSource = await readFile(new URL('../src/api/bridge.ts', import.meta.url), 'utf8')
const homeSource = await readFile(new URL('../src/pages/HomePage.tsx', import.meta.url), 'utf8')

// ---------- 获取模型预检 ----------

test('checkFetchModelsPrecondition: 新增模式 apiKey 为空不发请求', () => {
  const pre = checkFetchModelsPrecondition({ baseUrl: 'https://api.deepseek.com/v1', apiKey: '', isEdit: false })
  assert.equal(pre.ok, false)
  assert.equal(pre.reason, '请先填写 API Key 再获取模型')
})

test('checkFetchModelsPrecondition: 编辑模式 apiKey 留空允许仅带 baseUrl 调用', () => {
  const pre = checkFetchModelsPrecondition({ baseUrl: 'https://api.deepseek.com/v1', apiKey: '', isEdit: true })
  assert.deepEqual(pre, { ok: true })
})

test('checkFetchModelsPrecondition: baseUrl 为空一律拒绝', () => {
  for (const isEdit of [false, true]) {
    const pre = checkFetchModelsPrecondition({ baseUrl: '  ', apiKey: 'sk-x', isEdit })
    assert.equal(pre.ok, false, `isEdit=${isEdit}`)
  }
})

test('checkFetchModelsPrecondition: 齐全输入通过', () => {
  assert.deepEqual(
    checkFetchModelsPrecondition({ baseUrl: 'https://api.x.com/v1', apiKey: 'sk-x', isEdit: false }),
    { ok: true }
  )
})

test('llmSubmitData: 提交 LLM API 配置和默认低思考强度', () => {
  const data = llmSubmitData({
    baseUrl: ' https://api.x.com/v1 ',
    apiKey: 'sk-x',
    model: ' deepseek-chat ',
    authField: 'X-API-Key'
  })
  assert.deepEqual(data, {
    base_url: 'https://api.x.com/v1',
    api_key: 'sk-x',
    model: 'deepseek-chat',
    auth_field: 'X-API-Key',
    reasoning_effort: 'low'
  })
})

test('llmSubmitData: 保留用户选择的思考强度', () => {
  assert.equal(
    llmSubmitData({
      baseUrl: 'https://api.x.com/v1',
      apiKey: 'sk-x',
      model: 'gpt-5-mini',
      authField: 'Authorization',
      reasoningEffort: 'high'
    }).reasoning_effort,
    'high'
  )
})

// ---------- SettingsPage / bridge 源码断言 ----------

test('SettingsPage: LLM 配置不再包含音频模型，获取模型传认证 Header并使用共享错误处理', () => {
  assert.ok(!settingsSource.includes('audio_model'))
  assert.match(settingsSource, /api\.configs\.fetchModels\([\s\S]*?baseUrl\.trim\(\),[\s\S]*?apiKey\.trim\(\),[\s\S]*?authField[\s\S]*?\)/)
  assert.match(settingsSource, /checkFetchModelsPrecondition\(\{[\s\S]*?isEdit: canReuseSavedKey/)
  // 获取模型失败路径必须复用共享 errorMessage,不得裸抛
  const fetchBody = settingsSource.slice(
    settingsSource.indexOf('const handleFetchModels'),
    settingsSource.indexOf('const handleActivate')
  )
  assert.match(fetchBody, /toast\('error', errorMessage\(err\)\)/)
  assert.match(settingsSource, /思考强度/)
  assert.match(settingsSource, /reasoningEffort/)
})

test('SettingsPage: 获取模型按钮和文本模型位于 API Key 之后', async () => {
  const src = await readFile(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8')
  // 布局顺序: API Key 输入 -> 获取模型按钮 -> 文本模型
  const apiKeyPos = src.indexOf('label="API Key"')
  const fetchBtnPos = src.indexOf('\n                获取模型')
  const modelInputPos = src.indexOf('label="模型"')
  assert.ok(apiKeyPos > 0 && fetchBtnPos > apiKeyPos && modelInputPos > fetchBtnPos,
    '获取模型按钮与模型选择应位于 API Key 下边')
  assert.match(settingsSource, /datalist id="llm-model-options"/)
})

test('bridge: configs.fetchModels 调用 llm_fetch_models 命令并传 baseUrl/apiKey', () => {
  assert.match(
    bridgeSource,
    /fetchModels: \(baseUrl: string, apiKey: string, authField: string\) =>\s*\n\s*invoke<\{ models: string\[\] \}>\('llm_fetch_models', \{ baseUrl, apiKey, authField \}\)/
  )
})

test('SettingsPage: 代理编辑读取 proxy_url 且不显示已退役 Bing', () => {
  assert.match(settingsSource, /isNetwork \? \(item\.data\.proxy_url \?\? ''\)/)
  assert.ok(!settingsSource.includes('Bing Web Search'))
  assert.ok(!settingsSource.includes("'bing'"))
})

test('SettingsPage: 代理配置不渲染搜索引擎和 Search ID', () => {
  const networkBranch = settingsSource.slice(
    settingsSource.indexOf('{isNetwork ? (', settingsSource.indexOf('配置名称')),
    settingsSource.indexOf(') : isLlm ?', settingsSource.indexOf('{isNetwork ? ('))
  )
  assert.match(networkBranch, /label="代理地址"/)
  assert.doesNotMatch(networkBranch, /搜索引擎|Custom Search ID|Search ID/)
})

test('SettingsPage: 全局提示词独立于 LLM 配置且不显示 hr 分隔线', () => {
  assert.match(settingsSource, /id="global-system-prompt"/)
  assert.match(settingsSource, /<PromptCard \/>/)
  assert.ok(!settingsSource.includes('<hr className="border-stroke-subtle" />'))
  assert.ok(!settingsSource.includes('llm-system-prompt'))
})

test('HomePage: 搜索结果变少后仍保留搜索框并允许继续分页', () => {
  assert.match(homeSource, /\(sessions\.length > 3 \|\| query\.trim\(\)\)/)
  assert.match(homeSource, /const canLoadMore = sessions\.length >= 50 && !reachedEndRef\.current/)
  assert.match(homeSource, /query && canLoadMore/)
})

// ---------- S5:编辑提交语义(行为测试,替代源码字符串断言) ----------

test('configSavePayload: 新增模式提交原载荷,不带 config_id', () => {
  // 用真实的 llmSubmitData 组装 LLM 配置体,覆盖"新增 → 不发送 config_id"
  const body = {
    name: '主力模型',
    data: llmSubmitData({
      baseUrl: 'https://api.deepseek.com/v1',
      apiKey: 'sk-x',
      model: 'deepseek-chat',
      authField: 'Authorization'
    }),
    is_active: true
  }
  const payload = configSavePayload(body, null)
  assert.ok(!('config_id' in payload), '新增不得带 config_id,否则后端误当更新')
  assert.deepEqual(payload, {
    name: '主力模型',
    data: {
      base_url: 'https://api.deepseek.com/v1',
      api_key: 'sk-x',
      model: 'deepseek-chat',
      auth_field: 'Authorization',
      reasoning_effort: 'low'
    },
    is_active: true
  })
})

test('configSavePayload: 编辑模式注入 editTarget.id 作为 config_id', () => {
  const body = { name: '代理', data: { proxy_url: 'http://127.0.0.1:7890', api_key: 'placeholder' }, is_active: false }
  const payload = configSavePayload(body, { id: 42 })
  assert.deepEqual(payload, { ...body, config_id: 42 })
  // 原载荷不被原地修改
  assert.ok(!('config_id' in body))
})

test('SettingsPage: 保存统一走 configSavePayload(编辑语义接线)', async () => {
  const src = await readFile(new URL('../src/pages/SettingsPage.tsx', import.meta.url), 'utf8')
  assert.match(src, /api\.configs\.save\(type, configSavePayload\(body, editTarget\)\)/)
})
