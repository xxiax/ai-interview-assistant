/**
 * 会话级答题背景（岗位 JD + 简历）编辑弹窗。
 *
 * 背景写在 sessions 表上而不是全局设置，因为每场面试的岗位和投递简历都不同。
 * 后端 `_answer_payload_fields` 只在非空时把字段塞进 prompt，所以留空等于回到通用答案。
 * 任何会话状态都允许写入：面试中途粘贴 JD 也要能立刻生效。
 */
import { useEffect, useState } from 'react'
import { Briefcase, FileText } from 'lucide-react'
import { Button, Modal } from './ui'
import { api } from '../api/bridge'
import { errorMessage } from '../shared/errors'
import type { Session } from '../shared/types'

/** 与后端 db.MAX_SESSION_CONTEXT_CHARS / models.max_length 对齐。 */
export const MAX_SESSION_CONTEXT_CHARS = 8000

function CountedTextarea({
  id,
  label,
  icon,
  hint,
  placeholder,
  value,
  onChange
}: {
  id: string
  label: string
  icon: React.ReactNode
  hint: string
  placeholder: string
  value: string
  onChange: (next: string) => void
}) {
  const over = value.length > MAX_SESSION_CONTEXT_CHARS
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-2">
        <label
          htmlFor={id}
          className="flex items-center gap-1.5 text-[13px] font-medium text-ink-secondary"
        >
          <span className="text-ink-muted">{icon}</span>
          {label}
        </label>
        <span
          className={`tnum ml-auto text-[11px] ${over ? 'text-bad' : 'text-ink-faint'}`}
          aria-live="polite"
        >
          {value.length} / {MAX_SESSION_CONTEXT_CHARS}
        </span>
      </div>
      <textarea
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value.slice(0, MAX_SESSION_CONTEXT_CHARS))}
        rows={6}
        placeholder={placeholder}
        className="w-full resize-y rounded-lg border border-stroke bg-surface-card px-3 py-2 text-[13px] leading-6 text-ink-primary placeholder:text-ink-faint transition-colors duration-150 hover:border-[#35415f] focus:border-brand focus:outline-none"
      />
      <span className="mt-1.5 block text-xs text-ink-faint">{hint}</span>
    </div>
  )
}

export default function SessionContextModal({
  open,
  sessionId,
  session,
  onClose,
  onSaved
}: {
  open: boolean
  sessionId: string
  /** 已知的会话快照，用于回填输入框；缺省时按空白处理。 */
  session?: Session | null
  onClose: () => void
  onSaved?: (session: Session) => void
}) {
  const [jobDescription, setJobDescription] = useState('')
  const [resume, setResume] = useState('')
  const [saving, setSaving] = useState(false)

  // 每次打开都从最新快照回填，避免上次编辑后未保存的残留内容误导用户。
  useEffect(() => {
    if (!open) return
    setJobDescription(session?.job_description ?? '')
    setResume(session?.resume ?? '')
  }, [open, session?.job_description, session?.resume])

  const save = async () => {
    setSaving(true)
    try {
      const next = await api.sessions.setContext(sessionId, jobDescription, resume)
      onSaved?.(next)
      onClose()
      window.dispatchEvent(
        new CustomEvent('app-toast', {
          detail: {
            kind: 'success',
            message:
              jobDescription.trim() || resume.trim()
                ? '答题背景已保存，后续答案会贴合该岗位'
                : '答题背景已清空，后续答案回到通用模式'
          }
        })
      )
    } catch (err) {
      window.dispatchEvent(
        new CustomEvent('app-toast', { detail: { kind: 'error', message: errorMessage(err) } })
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title="答题背景"
      width={620}
      onClose={saving ? undefined : onClose}
      footer={
        <>
          <Button variant="ghost" disabled={saving} onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" loading={saving} onClick={() => void save()}>
            保存
          </Button>
        </>
      }
    >
      <p className="mb-4 text-[13px] leading-6 text-ink-secondary">
        填了岗位 JD 和简历，AI 就会照着这个岗位的要求和你的真实经历来答；
        留空则生成通用答案。面试中途也能改，保存后立刻对下一个问题生效。
      </p>
      <div className="space-y-4">
        <CountedTextarea
          id="session-job-description"
          label="岗位 JD"
          icon={<Briefcase size={13} />}
          hint="粘贴招聘页面的职位描述与任职要求即可，不需要整理格式。"
          placeholder="如：负责高并发交易系统的服务端开发，要求熟悉 Go / MySQL / Redis…"
          value={jobDescription}
          onChange={setJobDescription}
        />
        <CountedTextarea
          id="session-resume"
          label="简历"
          icon={<FileText size={13} />}
          hint="AI 只会引用这里写到的项目和数字，不会替你编造经历。"
          placeholder="如：3 年后端经验，主导过订单系统拆分，QPS 从 2k 提到 1.2w…"
          value={resume}
          onChange={setResume}
        />
      </div>
    </Modal>
  )
}
