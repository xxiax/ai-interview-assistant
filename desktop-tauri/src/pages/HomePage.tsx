import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronRight, ChevronDown, Plus, RefreshCw, Search, Mic, Trash2 } from 'lucide-react'
import dayjs from 'dayjs'
import { api } from '../api/bridge'
import { useSessionsStore } from '../stores/sessions'
import { Badge, Button, Empty, Input, Modal, CenterSpin } from '../components/ui'
import { errorMessage } from '../shared/errors'
import type { Session } from '../shared/types'

const STATUS: Record<string, { tone: 'idle' | 'recording' | 'ended'; text: string; dot: boolean; pulse: boolean }> = {
  idle: { tone: 'idle', text: '未开始', dot: false, pulse: false },
  recording: { tone: 'recording', text: '进行中', dot: true, pulse: true },
  ended: { tone: 'ended', text: '已结束', dot: true, pulse: false }
}

function SessionRow({
  session,
  onEnter,
  onDelete
}: {
  session: Session
  onEnter: (s: Session) => void
  onDelete: (s: Session) => void
}) {
  const st = STATUS[session.status] ?? STATUS.idle
  return (
    <div className="group flex w-full items-center gap-4 rounded-xl border border-stroke bg-surface-card px-5 py-4 transition-all duration-150 hover:border-[#35415f] hover:bg-surface-hover">
      <button
        onClick={() => onEnter(session)}
        className="flex min-w-0 flex-1 cursor-pointer items-center gap-4 text-left"
      >
        <div
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${
            session.status === 'recording' ? 'bg-brand/15 text-brand' : 'bg-surface-hover text-ink-faint'
          }`}
        >
          <Mic size={18} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-ink-primary">{session.title}</div>
          <div className="tnum mt-0.5 text-xs text-ink-faint">
            {dayjs(session.created_at).format('YYYY-MM-DD HH:mm')}
            {session.ended_at && ` · 结束于 ${dayjs(session.ended_at).format('HH:mm')}`}
          </div>
        </div>
        <Badge tone={st.tone} dot={st.dot} pulse={st.pulse}>
          {st.text}
        </Badge>
        <ChevronRight
          size={16}
          className="shrink-0 text-ink-faint transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-ink-muted"
        />
      </button>
      <button
        onClick={(e) => {
          e.stopPropagation()
          onDelete(session)
        }}
        aria-label={`删除会话 ${session.title}`}
        title={session.status === 'recording' ? '进行中的会话需先结束' : '删除会话'}
        disabled={session.status === 'recording'}
        className="shrink-0 cursor-pointer rounded-lg p-2 text-ink-faint opacity-0 transition-all duration-150 hover:bg-bad/10 hover:text-bad focus-visible:opacity-100 group-hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-0"
      >
        <Trash2 size={15} />
      </button>
    </div>
  )
}

export default function HomePage({ mode = 'active' }: { mode?: 'active' | 'history' }) {
  const navigate = useNavigate()
  const { sessions, loading, load, create, remove } = useSessionsStore()
  // 最近一次列表请求是否不足一页:为 true 说明已到末尾,无需再加载
  const reachedEndRef = useRef(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [creating, setCreating] = useState(false)
  const [query, setQuery] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null)
  const [deleting, setDeleting] = useState(false)
  const isHistory = mode === 'history'

  useEffect(() => {
    reachedEndRef.current = false
    void load(50, 0)
  }, [load])

  const enter = (s: Session) =>
    navigate(s.status === 'ended' ? `/session/${s.id}` : `/live/${s.id}`)

  const handleCreate = async () => {
    const trimmed = title.trim()
    if (!trimmed) return
    setCreating(true)
    try {
      const session = await create(trimmed)
      setCreateOpen(false)
      setTitle('')
      navigate(`/live/${session.id}`)
    } catch (err) {
      window.dispatchEvent(
        new CustomEvent('app-toast', { detail: { kind: 'error', message: errorMessage(err) } })
      )
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await remove(deleteTarget.id)
      setDeleteTarget(null)
      window.dispatchEvent(
        new CustomEvent('app-toast', { detail: { kind: 'success', message: '会话已删除' } })
      )
    } catch (err) {
      window.dispatchEvent(
        new CustomEvent('app-toast', { detail: { kind: 'error', message: errorMessage(err) } })
      )
    } finally {
      setDeleting(false)
    }
  }

  const filtered = sessions.filter((s) => (isHistory ? s.status === 'ended' : s.status !== 'ended'))
    .filter((s) => !query.trim() || s.title.toLowerCase().includes(query.trim().toLowerCase()))
  const canLoadMore = sessions.length >= 50 && !reachedEndRef.current

  const handleLoadMore = async () => {
    const before = sessions.length
    const page = await api.sessions.list(50, before)
    reachedEndRef.current = page.length < 50
    if (page.length > 0) {
      useSessionsStore.setState((s) => ({ sessions: [...s.sessions, ...page] }))
    }
  }

  const [loadingMore, setLoadingMore] = useState(false)

  const loadMore = async () => {
    if (loadingMore) return
    setLoadingMore(true)
    try {
      await handleLoadMore()
    } catch (err) {
      window.dispatchEvent(
        new CustomEvent('app-toast', { detail: { kind: 'error', message: errorMessage(err) } })
      )
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-8 py-8">
        {/* 页头 */}
        <div className="mb-6 flex items-end justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-ink-primary">
              {isHistory ? '面试历史' : '面试会话'}
            </h1>
            <p className="mt-1 text-[13px] text-ink-faint">
              {isHistory ? '已结束的面试存档,可查看转写、回答与复盘' : '创建新面试,或从进行中的会话继续'}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              icon={<RefreshCw size={14} />}
              onClick={() => {
                reachedEndRef.current = false
                void load(50, 0)
              }}
            >
              刷新
            </Button>
            {!isHistory && (
              <Button variant="primary" icon={<Plus size={15} />} onClick={() => setCreateOpen(true)}>
                新建面试
              </Button>
            )}
          </div>
        </div>

        {/* 搜索 */}
        {(sessions.length > 3 || query.trim()) && (
          <div className="relative mb-4">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={isHistory ? '搜索历史标题…' : '搜索面试标题…'}
              aria-label="搜索面试标题"
              className="w-full rounded-lg border border-stroke bg-surface-card py-2 pl-9 pr-3 text-sm text-ink-primary placeholder:text-ink-faint transition-colors focus:border-brand focus:outline-none"
            />
          </div>
        )}

        {/* 列表 */}
        {loading && sessions.length === 0 ? (
          <CenterSpin hint={isHistory ? '加载历史…' : '加载会话…'} />
        ) : filtered.length === 0 ? (
          <Empty
            title={
              query
                ? '没有匹配的面试'
                : isHistory
                  ? '还没有历史记录'
                  : '还没有面试记录'
            }
            hint={
              query
                ? undefined
                : isHistory
                  ? '结束一场面试后,它会出现在这里'
                  : '创建第一场面试,开始实时转写与 AI 答案提示'
            }
            action={
              query && canLoadMore ? (
                <Button
                  variant="ghost"
                  icon={<ChevronDown size={14} />}
                  loading={loadingMore}
                  onClick={() => void loadMore()}
                >
                  加载更多
                </Button>
              ) : (
                !query &&
                !isHistory && (
                  <Button variant="primary" icon={<Plus size={15} />} onClick={() => setCreateOpen(true)}>
                    创建第一场面试
                  </Button>
                )
              )
            }
          />
        ) : (
          <div className="space-y-2.5">
            {filtered.map((s) => (
              <SessionRow key={s.id} session={s} onEnter={enter} onDelete={setDeleteTarget} />
            ))}
            {/* 翻页:列表按 50 条分页拉取,未到末尾时可继续加载 */}
            {canLoadMore && (
              <div className="flex justify-center pt-2">
                <Button
                  variant="ghost"
                  icon={<ChevronDown size={14} />}
                  loading={loadingMore}
                  onClick={() => void loadMore()}
                >
                  加载更多
                </Button>
              </div>
            )}
            {reachedEndRef.current && (
              <p className="pt-1 text-center text-xs text-ink-faint">已加载全部会话</p>
            )}
          </div>
        )}
      </div>

      {/* 新建弹窗 */}
      <Modal
        open={createOpen}
        title="新建面试"
        onClose={() => {
          setCreateOpen(false)
          setTitle('')
        }}
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={creating}
              disabled={!title.trim()}
              onClick={() => void handleCreate()}
            >
              创建并进入
            </Button>
          </>
        }
      >
        <Input
          label="面试标题"
          placeholder="如:某公司后端一面"
          value={title}
          maxLength={200}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void handleCreate()}
          autoFocus
        />
      </Modal>

      {/* 删除确认 */}
      <Modal
        open={!!deleteTarget}
        title="删除面试"
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
          确定删除「{deleteTarget?.title}」吗?
          <br />
          <span className="text-ink-faint">转写、回答、复盘等全部数据将一并删除,不可恢复。</span>
        </p>
      </Modal>
    </div>
  )
}
