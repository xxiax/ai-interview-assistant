import { useEffect, useState } from 'react'
import {
  AudioLines,
  CheckCircle2,
  Globe,
  KeyRound,
  Pencil,
  Plus,
  Save,
  Server,
  Sparkles,
  Trash2,
  XCircle
} from 'lucide-react'
import { api } from '../api/bridge'
import { useSettingsStore } from '../stores/settings'
import { errorMessage } from '../shared/errors'
import type { ConfigItem, ConfigType } from '../shared/types'
import { Button, Input, Modal, CenterSpin } from '../components/ui'
import { checkFetchModelsPrecondition, configSavePayload, llmSubmitData, type ReasoningEffort } from './llm-form'

function toast(kind: string, message: string) {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}

// ---------- 访问令牌卡片 ----------

function TokenCard() {
  const { settings, loading, load, save, check, checking, lastCheck } = useSettingsStore()
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void load()
  }, [load])

  const handleSave = async () => {
    setSaving(true)
    try {
      await save({ token: token.trim() || undefined })
      setToken('')
      toast('success', '设置已保存')
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleCheck = async () => {
    const result = await check()
    if (result.ok) toast('success', '连接正常')
  }

  if (loading && !settings) return <CenterSpin />

  return (
    <section className="rounded-2xl border border-stroke bg-surface-card p-6">
      <div className="mb-5 flex items-center gap-2.5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand/12 text-brand">
          <Server size={16} />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink-primary">访问令牌</h2>
          <p className="text-xs text-ink-faint">
            后端固定运行在本机 http://127.0.0.1:8000,只需配置令牌(加密存储于本机)
          </p>
        </div>
      </div>

      <div className="space-y-4">
        <div>
          <Input
            label="访问令牌"
            type="password"
            placeholder={settings?.hasToken ? '已保存(留空则不修改)' : '至少 32 个字符的服务令牌'}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            hint="令牌通过系统凭据管理器(Windows 凭据管理器)加密保存,不会明文落盘"
          />
          {settings && (
            <div className="mt-2 inline-flex items-center gap-1.5 text-xs">
              {settings.hasToken ? (
                <>
                  <CheckCircle2 size={13} className="text-good" />
                  <span className="text-good">令牌已配置</span>
                </>
              ) : (
                <>
                  <XCircle size={13} className="text-bad" />
                  <span className="text-bad">令牌未配置</span>
                </>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 pt-1">
          <Button icon={<Save size={14} />} loading={saving} onClick={() => void handleSave()}>
            保存
          </Button>
          <Button icon={<Sparkles size={14} />} loading={checking} onClick={() => void handleCheck()}>
            连接测试
          </Button>
          {lastCheck && (
            <span className={`ml-1 text-xs ${lastCheck.ok ? 'text-good' : 'text-bad'}`}>
              {lastCheck.ok ? '连接正常:服务可达且令牌有效' : lastCheck.reason}
            </span>
          )}
        </div>
      </div>
    </section>
  )
}

function PromptCard() {
  const [prompt, setPrompt] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void api.settings.prompt
      .get()
      .then((result) => setPrompt(result.prompt))
      .catch((err) => toast('error', '读取全局提示词失败：' + errorMessage(err)))
      .finally(() => setLoading(false))
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      const result = await api.settings.prompt.set(prompt)
      setPrompt(result.prompt)
      toast('success', result.prompt ? '全局提示词已保存' : '全局提示词已清除')
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="rounded-2xl border border-stroke bg-surface-card p-6">
      <div className="mb-5 flex items-center gap-2.5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand/12 text-brand">
          <Sparkles size={16} />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink-primary">全局提示词</h2>
          <p className="text-xs text-ink-faint">统一作用于每次实时回答和搜索增强回答</p>
        </div>
      </div>
      <div className="space-y-3">
        <label
          htmlFor="global-system-prompt"
          className="block text-[13px] font-medium text-ink-secondary"
        >
          角色与回答要求
        </label>
        <textarea
          id="global-system-prompt"
          value={prompt}
          maxLength={8000}
          rows={5}
          disabled={loading}
          placeholder="例如：你是一名资深后端面试官，请快速、简洁地给出可执行的回答要点。"
          onChange={(e) => setPrompt(e.target.value)}
          className="w-full resize-y rounded-lg border border-stroke bg-surface-card px-3 py-2 text-sm leading-6 text-ink-primary placeholder:text-ink-faint transition-colors duration-150 hover:border-[#35415f] focus:border-brand focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
        />
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs text-ink-faint">
            留空并保存即可恢复系统默认提示词。
          </span>
          <Button
            icon={<Save size={14} />}
            loading={saving || loading}
            disabled={loading}
            onClick={() => void handleSave()}
          >
            保存
          </Button>
        </div>
      </div>
    </section>
  )
}

// ---------- 配置卡片 ----------

function ConfigCard({ type }: { type: ConfigType }) {
  const [items, setItems] = useState<ConfigItem[]>([])
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<ConfigItem | null>(null)
  const [deleting, setDeleting] = useState(false)
  const isLlm = type === 'llm'
  const isAsr = type === 'asr'
  const isNetwork = type === 'network'

  // 表单
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [authField, setAuthField] = useState('Authorization')
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('low')
  const [engine, setEngine] = useState<'google'>('google')
  const [cx, setCx] = useState('')
  const [isActive, setIsActive] = useState(true)
  // 编辑回填:正在编辑的配置(密钥不回显,留空表示沿用;后端按 name 重新保存)
  const [editTarget, setEditTarget] = useState<ConfigItem | null>(null)
  // 获取模型列表
  const [modelOptions, setModelOptions] = useState<string[]>([])
  const [fetchingModels, setFetchingModels] = useState(false)

  const load = async () => {
    try {
      setItems(await api.configs.list(type))
    } catch (err) {
      toast('error', errorMessage(err))
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type])

  const handleSave = async () => {
    if (!name.trim()) return
    setSaving(true)
    try {
      const body = isLlm
        ? {
            name: name.trim(),
            data: llmSubmitData({
              baseUrl,
              apiKey,
              model,
              authField,
              reasoningEffort
            }),
            is_active: isActive
          }
        : isAsr
          ? {
              name: name.trim(),
              data: {
                api_key: apiKey,
                model: model.trim() || 'whisper-large-v3'
              },
              is_active: isActive
            }
          : isNetwork
            ? {
                name: name.trim(),
                data: {
                  proxy_url: baseUrl.trim(),
                  api_key: 'placeholder'
                },
                is_active: isActive
              }
            : {
              name: name.trim(),
              data: {
                engine,
                api_key: apiKey,
                cx: cx.trim()
              },
              is_active: isActive
            }
      await api.configs.save(type, configSavePayload(body, editTarget))
      toast(
        'success',
        isLlm ? 'LLM 配置已保存' : isAsr ? 'ASR 配置已保存' : isNetwork ? '代理配置已保存' : '搜索配置已保存'
      )
      closeForm()
      void load()
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  // 关闭/取消后清空表单,避免下次打开残留上次输入
  const closeForm = () => {
    setOpen(false)
    setName('')
    setApiKey('')
    setBaseUrl('')
    setModel('')
    setCx('')
    setAuthField('Authorization')
    setReasoningEffort('low')
    setIsActive(true)
    setEditTarget(null)
    setModelOptions([])
  }

  // 编辑已有配置:回填公开字段(密钥不回填,留空表示不修改)
  const openEdit = (item: ConfigItem) => {
    setEditTarget(item)
    setName(item.name)
    setBaseUrl(String(isNetwork ? (item.data.proxy_url ?? '') : (item.data.base_url ?? '')))
    setModel(String(item.data.model ?? ''))
    setAuthField(String(item.data.auth_field ?? 'Authorization'))
    const configuredReasoningEffort = item.data.reasoning_effort
    setReasoningEffort(
      configuredReasoningEffort === 'medium' || configuredReasoningEffort === 'high'
        ? configuredReasoningEffort
        : 'low'
    )
    setIsActive(item.is_active)
    setApiKey('')
    setCx(String(item.data.cx ?? ''))
    setOpen(true)
  }

  // 获取模型列表:成功后 model 变为 datalist 下拉可选
  const handleFetchModels = async () => {
    const normalizeBaseUrl = (value: string) => value.trim().replace(/\/+$/, '')
    const canReuseSavedKey =
      !!editTarget &&
      normalizeBaseUrl(String(editTarget.data.base_url ?? '')) === normalizeBaseUrl(baseUrl)
    const pre = checkFetchModelsPrecondition({
      baseUrl,
      apiKey,
      isEdit: canReuseSavedKey
    })
    if (!pre.ok) {
      toast('warning', pre.reason)
      return
    }
    setFetchingModels(true)
    try {
      const { models } = await api.configs.fetchModels(
        baseUrl.trim(),
        apiKey.trim(),
        authField
      )
      setModelOptions(models)
      if (models.length === 0) toast('info', '该服务未返回任何模型')
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      setFetchingModels(false)
    }
  }

  const handleActivate = async (id: number) => {
    try {
      await api.configs.activate(type, id)
      toast('success', '已激活')
      void load()
    } catch (err) {
      toast('error', errorMessage(err))
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.configs.delete(type, deleteTarget.id)
      setDeleteTarget(null)
      toast('success', '配置已删除')
      void load()
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <section className="rounded-2xl border border-stroke bg-surface-card p-6">
      <div className="mb-5 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${isAsr ? 'bg-[#34d399]/12 text-[#34d399]' : isNetwork ? 'bg-[#38bdf8]/12 text-[#38bdf8]' : 'bg-[#a78bfa]/12 text-[#a78bfa]'}`}>
            {isLlm ? <Sparkles size={16} /> : isAsr ? <AudioLines size={16} /> : isNetwork ? <Globe size={16} /> : <Globe size={16} />}
          </div>
          <div>
            <h2 className="text-sm font-semibold text-ink-primary">
              {isLlm ? 'LLM 配置' : isAsr ? '语音转写(ASR)' : isNetwork ? '网络代理' : '搜索配置'}
              {!isLlm && !isAsr && !isNetwork && (
                <span className="ml-1.5 text-[11px] font-normal text-ink-faint">可选</span>
              )}
            </h2>
            <p className="text-xs text-ink-faint">
              {isLlm
                ? '生成答案与复盘'
                : isAsr
                  ? '这里只保存 Groq 凭据；实际引擎由后端 AI_ASR_ENGINE 决定'
                  : isNetwork
                    ? '默认直连;访问受限网络时在此配置代理'
                    : '搜索增强功能需要'}
            </p>
          </div>
        </div>
        <Button
          variant="ghost"
          icon={<Plus size={14} />}
          className="!px-3 !py-1.5 !text-xs"
          onClick={() => setOpen(true)}
        >
          新增
        </Button>
      </div>

      {items.length === 0 ? (
        <p className="py-6 text-center text-xs text-ink-faint">
          暂无{isLlm ? ' LLM ' : isAsr ? ' ASR ' : isNetwork ? '代理' : '搜索'}配置
        </p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div
              key={item.id}
              className="flex items-center gap-3 rounded-lg border border-stroke-subtle bg-surface/50 px-4 py-3"
            >
              <KeyRound
                size={14}
                className={item.secret_configured ? 'text-good' : 'text-bad'}
              />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium text-ink-primary">{item.name}</div>
                <div className="truncate text-xs text-ink-faint">
                  {isNetwork
                    ? String(item.data.proxy_url ?? '-')
                    : isLlm || isAsr
                      ? String(item.data.model ?? '-')
                      : String(item.data.engine ?? '-')}
                </div>
              </div>
              {item.is_active ? (
                <span className="rounded-full bg-brand/15 px-2.5 py-0.5 text-[11px] font-medium text-brand">
                  使用中
                </span>
              ) : (
                <Button
                  variant="ghost"
                  className="!px-2.5 !py-1 !text-xs"
                  onClick={() => void handleActivate(item.id)}
                >
                  激活
                </Button>
              )}
              <button
                onClick={() => openEdit(item)}
                aria-label={`编辑配置 ${item.name}`}
                title="编辑配置"
                className="shrink-0 cursor-pointer rounded-lg p-1.5 text-ink-faint transition-colors hover:bg-brand/10 hover:text-brand"
              >
                <Pencil size={14} />
              </button>
              <button
                onClick={() => setDeleteTarget(item)}
                aria-label={`删除配置 ${item.name}`}
                title="删除配置"
                className="shrink-0 cursor-pointer rounded-lg p-1.5 text-ink-faint transition-colors hover:bg-bad/10 hover:text-bad"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* 删除确认 */}
      <Modal
        open={!!deleteTarget}
        title="删除配置"
        onClose={() => setDeleteTarget(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)}>
              取消
            </Button>
            <Button variant="danger" loading={deleting} onClick={() => void handleDelete()}>
              确认删除
            </Button>
          </>
        }
      >
        <p className="text-[13px] leading-6 text-ink-secondary">
          确定删除配置「{deleteTarget?.name}」吗?
          <br />
          {deleteTarget?.is_active ? (
            <span className="text-warn">该配置正在使用中,删除后对应功能将不可用,直到激活其他配置。</span>
          ) : (
            <span className="text-ink-faint">删除后不可恢复。</span>
          )}
        </p>
      </Modal>

      <Modal
        open={open}
        title={
          editTarget
            ? isLlm
              ? '编辑 LLM 配置'
              : isAsr
                ? '编辑 ASR 配置'
                : isNetwork
                  ? '编辑代理配置'
                  : '编辑搜索配置'
            : isLlm
              ? '新增 LLM 配置'
              : isAsr
                ? '新增 ASR 配置'
                : isNetwork
                  ? '新增代理配置'
                  : '新增搜索配置'
        }
        onClose={closeForm}
        width={480}
        footer={
          <>
            <Button variant="ghost" onClick={closeForm}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={saving}
              disabled={
                !name.trim() ||
                (!isNetwork && !editTarget && !apiKey.trim()) ||
                (isLlm && (!baseUrl.trim() || !model.trim())) ||
                (isNetwork && !baseUrl.trim())
              }
              onClick={() => void handleSave()}
            >
              保存
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input
            label="配置名称"
            placeholder={
              isLlm ? '如:DeepSeek' : isAsr ? '如:Groq' : isNetwork ? '如:本机 Clash' : '如:Google 搜索'
            }
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          {isNetwork ? (
            <Input
              label="代理地址"
              placeholder="http://127.0.0.1:7897"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              hint="支持 http / https / socks5 / socks5h;留空不保存则默认直连"
            />
          ) : isLlm ? (
            <>
              <Input
                label="Base URL"
                placeholder="https://api.deepseek.com/v1"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                hint="必须为 HTTPS；服务端会拒绝本机、私网和保留地址"
              />
              <div>
                <span className="mb-1.5 block text-[13px] font-medium text-ink-secondary">
                  认证 Header
                </span>
                <select
                  value={authField}
                  onChange={(e) => setAuthField(e.target.value)}
                  className="w-full cursor-pointer rounded-lg border border-stroke bg-surface-card px-3 py-2 text-sm text-ink-primary focus:border-brand focus:outline-none"
                >
                  <option value="Authorization">Authorization (Bearer)</option>
                  <option value="X-API-Key">X-API-Key</option>
                  <option value="API-Key">API-Key</option>
                </select>
              </div>
            </>
          ) : isAsr ? (
            <>
              <Input
                label="模型(留空使用默认)"
                placeholder="whisper-large-v3"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                hint="Groq 支持的 Whisper 模型名"
              />
            </>
          ) : (
            <>
              <div>
                <span className="mb-1.5 block text-[13px] font-medium text-ink-secondary">
                  搜索引擎
                </span>
                <div className="grid grid-cols-2 gap-2" role="radiogroup">
                  {(['google'] as const).map((e) => (
                    <button
                      key={e}
                      role="radio"
                      aria-checked={engine === e}
                      onClick={() => setEngine(e)}
                      className={`cursor-pointer rounded-lg border px-3 py-2.5 text-xs font-medium capitalize transition-all ${
                        engine === e
                          ? 'border-brand bg-brand/10 text-brand'
                          : 'border-stroke bg-surface-card text-ink-secondary hover:bg-surface-hover'
                      }`}
                    >
                      Google Custom Search
                    </button>
                  ))}
                </div>
              </div>
              <Input label="Custom Search ID(Google 必填)" placeholder="cx 参数" value={cx} onChange={(ev) => setCx(ev.target.value)} />
            </>
          )}
          {!isNetwork && (
            <Input
              label="API Key"
              type="password"
              placeholder={
                isAsr
                  ? 'gsk_ 开头的 Groq Key'
                  : editTarget
                    ? '留空表示沿用已保存的密钥'
                    : '密钥加密存储,保存后不再显示'
              }
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              hint={isAsr ? '仅 AI_ASR_ENGINE=groq 时使用；密钥加密存储于服务端' : undefined}
            />
          )}
          {isLlm && (
            <>
              <Button
                variant="default"
                icon={<Sparkles size={14} />}
                loading={fetchingModels}
                disabled={!baseUrl.trim()}
                onClick={() => void handleFetchModels()}
                className="!py-1.5 !text-xs"
              >
                获取模型
              </Button>
              {editTarget && !apiKey && (
                <p className="text-xs text-ink-faint">
                  编辑模式:API Key 留空表示沿用已保存的密钥;获取模型将直接使用 Base URL 调用。
                </p>
              )}
              <Input
                label="模型"
                placeholder="如:deepseek-chat"
                value={model}
                list={modelOptions.length > 0 ? 'llm-model-options' : undefined}
                onChange={(e) => setModel(e.target.value)}
                />
                {modelOptions.length > 0 && (
                  <datalist id="llm-model-options">
                    {modelOptions.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
              )}
              <div>
                <span className="mb-1.5 block text-[13px] font-medium text-ink-secondary">
                  思考强度
                </span>
                <select
                  value={reasoningEffort}
                  onChange={(e) => setReasoningEffort(e.target.value as ReasoningEffort)}
                  className="w-full cursor-pointer rounded-lg border border-stroke bg-surface-card px-3 py-2 text-sm text-ink-primary focus:border-brand focus:outline-none"
                >
                  <option value="low">低（速度优先）</option>
                  <option value="medium">中（平衡）</option>
                  <option value="high">高（更充分）</option>
                </select>
                <p className="mt-1.5 text-xs text-ink-faint">
                  默认低。仅对支持 reasoning_effort 的 GPT-5、o1、o3、o4 系列模型生效，普通模型会自动忽略。
                </p>
              </div>
            </>
          )}
          <label className="flex cursor-pointer items-center gap-2 text-[13px] text-ink-secondary">
            <input
              type="checkbox"
              checked={isActive}
              onChange={(e) => setIsActive(e.target.checked)}
              className="h-4 w-4 cursor-pointer accent-[#4f7cff]"
            />
            设为当前使用(同类型仅一个激活)
          </label>
        </div>
      </Modal>
    </section>
  )
}

export default function SettingsPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-2xl space-y-5 px-8 py-8">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-ink-primary">设置</h1>
          <p className="mt-1 text-[13px] text-ink-faint">访问令牌与 AI 配置</p>
        </div>
        <TokenCard />
        <PromptCard />
        <ConfigCard type="asr" />
        <ConfigCard type="network" />
        <ConfigCard type="llm" />
        <ConfigCard type="search" />
      </div>
    </div>
  )
}
