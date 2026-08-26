import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  ChevronDown,
  FileText,
  Globe,
  PlayCircle,
  RefreshCw,
  Sparkles
} from 'lucide-react'
import dayjs from 'dayjs'
import { api } from '../api/bridge'
import { errorMessage } from '../shared/errors'
import type { Answer, Review, Session, Transcript } from '../shared/types'
import { groupFinalTranscripts } from '../shared/transcript-display'
import { Badge, Button, CenterSpin, Empty } from '../components/ui'
import Markdown from '../components/Markdown'
import TranscriptFeed from './feeds/TranscriptFeed'
import AnswerFeed from './feeds/AnswerFeed'

type Tab = 'transcripts' | 'answers' | 'review'

function toast(kind: string, message: string) {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}

export default function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const [session, setSession] = useState<Session | null>(null)
  const [transcripts, setTranscripts] = useState<Transcript[]>([])
  const [answers, setAnswers] = useState<Answer[]>([])
  const [reviews, setReviews] = useState<Review[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<Tab>('transcripts')
  const [useSearch, setUseSearch] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const transcriptDisplayCount = useMemo(
    () => groupFinalTranscripts(transcripts).length,
    [transcripts]
  )
  // 旧请求的 stale guard:会话切换/卸载后晚到的数据不得覆盖新页面
  const loadSeqRef = useRef(0)

  const loadAll = async () => {
    if (!sessionId) return
    const seq = ++loadSeqRef.current
    setLoading(true)
    try {
      const [s, t, a, r] = await Promise.all([
        api.sessions.get(sessionId),
        api.history.transcripts(sessionId),
        api.history.answers(sessionId),
        api.history.reviews(sessionId)
      ])
      if (loadSeqRef.current !== seq) return
      setSession(s)
      setTranscripts(t)
      setAnswers(a)
      setReviews(r)
    } catch (err) {
      if (loadSeqRef.current === seq) toast('error', errorMessage(err))
    } finally {
      if (loadSeqRef.current === seq) setLoading(false)
    }
  }

  useEffect(() => {
    void loadAll()
    return () => {
      loadSeqRef.current += 1 // 卸载/切会话:作废在途请求
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  const handleGenerateReview = async () => {
    if (!sessionId) return
    setGenerating(true)
    try {
      await api.history.generateReview(sessionId, useSearch)
      toast('success', '复盘已生成')
      setReviews(await api.history.reviews(sessionId))
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      setGenerating(false)
    }
  }

  if (loading) return <CenterSpin hint="加载会话…" />
  if (!session)
    return <Empty title="会话不存在" action={<Button onClick={() => navigate('/')}>返回列表</Button>} />

  const latest = reviews[0]
  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: 'transcripts', label: '转写', count: transcriptDisplayCount },
    { key: 'answers', label: '回答', count: answers.length },
    { key: 'review', label: '复盘', count: reviews.length }
  ]

  return (
    <div className="flex h-full flex-col">
      {/* 头部 */}
      <header className="border-b border-stroke bg-surface-raised px-5 py-4">
        <div className="flex items-center justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <button
              onClick={() => navigate('/')}
              aria-label="返回"
              className="cursor-pointer rounded-lg p-1.5 text-ink-muted transition-colors hover:bg-surface-hover hover:text-ink-primary"
            >
              <ArrowLeft size={17} />
            </button>
            <h1 className="truncate text-[15px] font-semibold text-ink-primary">{session.title}</h1>
            <Badge tone={session.status === 'ended' ? 'ended' : session.status === 'recording' ? 'recording' : 'idle'} dot pulse={session.status === 'recording'}>
              {session.status === 'ended' ? '已结束' : session.status === 'recording' ? '进行中' : '未开始'}
            </Badge>
            {session.status === 'recording' && (
              <Button
                variant="ghost"
                icon={<PlayCircle size={14} />}
                className="!px-2 !py-1 !text-xs"
                onClick={() => navigate(`/live/${session.id}`)}
              >
                进入面试
              </Button>
            )}
          </div>
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => void loadAll()}>
            刷新
          </Button>
        </div>
        <div className="tnum mt-2 flex gap-6 pl-10 text-xs text-ink-faint">
          <span>创建 {dayjs(session.created_at).format('YYYY-MM-DD HH:mm')}</span>
          <span>结束 {session.ended_at ? dayjs(session.ended_at).format('HH:mm') : '—'}</span>
          <span>转写 {transcriptDisplayCount}</span>
          <span>回答 {answers.length}</span>
        </div>
      </header>

      {/* 标签栏 */}
      <div className="flex items-center gap-1 border-b border-stroke bg-surface px-5" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={`relative cursor-pointer px-4 py-3 text-[13px] font-medium transition-colors ${
              tab === t.key ? 'text-brand' : 'text-ink-muted hover:text-ink-secondary'
            }`}
          >
            {t.label}
            <span className="tnum ml-1.5 text-[11px] text-ink-faint">{t.count}</span>
            {tab === t.key && (
              <span className="absolute inset-x-3 -bottom-px h-0.5 rounded-full bg-brand" />
            )}
          </button>
        ))}
      </div>

      {/* 内容 */}
      <div className="min-h-0 flex-1">
        {tab === 'transcripts' && <TranscriptFeed transcripts={transcripts} />}
        {tab === 'answers' && <AnswerFeed answers={answers} />}
        {tab === 'review' && (
          <div className="h-full overflow-y-auto px-6 py-5">
            <div className="mx-auto max-w-3xl">
              {latest ? (
                <article className="rounded-xl border border-stroke bg-surface-card p-5">
                  <div className="mb-3 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <FileText size={15} className="text-brand" />
                      <span className="text-[13px] font-medium text-ink-primary">
                        最新复盘 · {dayjs(latest.created_at).format('YYYY-MM-DD HH:mm')}
                      </span>
                      <span
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                          latest.source === 'search+llm'
                            ? 'bg-[#a78bfa]/12 text-[#a78bfa]'
                            : 'bg-good/10 text-good'
                        }`}
                      >
                        {latest.source === 'search+llm' ? <Globe size={10} /> : <Sparkles size={10} />}
                        {latest.source === 'search+llm' ? '搜索增强' : 'LLM'}
                      </span>
                    </div>
                  </div>
                  <Markdown source={latest.content} variant="relaxed" />
                </article>
              ) : (
                <Empty title="尚未生成复盘" hint="复盘基于完整转写与回答,由 LLM 生成改进建议" />
              )}

              {/* 历史复盘折叠 */}
              {reviews.length > 1 && (
                <div className="mt-4">
                  <button
                    onClick={() => setHistoryOpen(!historyOpen)}
                    className="flex cursor-pointer items-center gap-1.5 text-xs text-ink-faint transition-colors hover:text-ink-secondary"
                  >
                    <ChevronDown
                      size={13}
                      className={`transition-transform duration-150 ${historyOpen ? '' : '-rotate-90'}`}
                    />
                    历史复盘({reviews.length - 1})
                  </button>
                  {historyOpen && (
                    <div className="mt-3 space-y-2.5 animate-fade-in">
                      {reviews.slice(1).map((r) => (
                        <div key={r.id} className="rounded-lg border border-stroke-subtle bg-surface-card/60 p-3.5">
                          <div className="tnum mb-1.5 text-[11px] text-ink-faint">
                            {dayjs(r.created_at).format('YYYY-MM-DD HH:mm:ss')} ·{' '}
                            {r.source === 'search+llm' ? '搜索增强' : 'LLM'}
                          </div>
                          <div className="line-clamp-4 text-xs leading-5 text-ink-muted">
                            {r.content.slice(0, 300)}
                            {r.content.length > 300 ? '…' : ''}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* 生成操作 */}
              <div className="mt-6 flex items-center gap-4 border-t border-stroke-subtle pt-5">
                <Button
                  variant="primary"
                  icon={<Sparkles size={14} />}
                  loading={generating}
                  disabled={session.status !== 'ended' || transcripts.length === 0}
                  onClick={() => void handleGenerateReview()}
                >
                  {latest ? '重新生成复盘' : '生成复盘'}
                </Button>
                <label className="flex cursor-pointer items-center gap-2 text-[13px] text-ink-secondary">
                  <input
                    type="checkbox"
                    checked={useSearch}
                    onChange={(e) => setUseSearch(e.target.checked)}
                    className="h-4 w-4 cursor-pointer accent-[#4f7cff]"
                  />
                  搜索增强
                </label>
                {session.status !== 'ended' && (
                  <span className="text-xs text-ink-faint">仅已结束会话可生成复盘</span>
                )}
                {session.status === 'ended' && transcripts.length === 0 && (
                  <span className="text-xs text-ink-faint">会话无转写记录,无法复盘</span>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
