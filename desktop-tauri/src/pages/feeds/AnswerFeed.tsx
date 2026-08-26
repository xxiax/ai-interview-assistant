import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import dayjs from 'dayjs'
import { Globe, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import type { Answer, StreamingAnswer } from '../../shared/types'
import { api } from '../../api/bridge'
import { errorMessage } from '../../shared/errors'
import { Button, Empty } from '../../components/ui'
import Markdown from '../../components/Markdown'

function toast(kind: string, message: string) {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}

export default function AnswerFeed({
  answers,
  streamingAnswers = []
}: {
  answers: Answer[]
  streamingAnswers?: StreamingAnswer[]
}) {
  const feedRef = useRef<HTMLDivElement>(null)
  const followTailRef = useRef(true)
  // 冷却按答案 id 记录:一条重新生成时其余按钮仍可用
  const [regeneratingIds, setRegeneratingIds] = useState<Set<number>>(new Set())
  const cooldownTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())

  useLayoutEffect(() => {
    const feed = feedRef.current
    if (feed && followTailRef.current) feed.scrollTop = feed.scrollHeight
  }, [answers.length, streamingAnswers])

  const handleScroll = () => {
    const feed = feedRef.current
    if (!feed) return
    followTailRef.current = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 64
  }

  // 卸载清理冷却定时器,避免卸载后 setState 脏更新
  useEffect(() => {
    const timers = cooldownTimersRef.current
    return () => {
      for (const t of timers) clearTimeout(t)
      timers.clear()
    }
  }, [])

  const handleRegenerate = async (answer: Answer) => {
    if (regeneratingIds.has(answer.id)) return
    setRegeneratingIds((prev) => new Set(prev).add(answer.id))
    try {
      const ok = await api.live.regenerate(answer.question, false)
      if (!ok) toast('warning', '当前无法发送请求(连接未就绪或队列已满)')
    } catch (err) {
      toast('error', errorMessage(err))
    } finally {
      // 限流 10/min:按钮冷却 6.5s
      const t = setTimeout(() => {
        cooldownTimersRef.current.delete(t)
        setRegeneratingIds((prev) => {
          const next = new Set(prev)
          next.delete(answer.id)
          return next
        })
      }, 6500)
      cooldownTimersRef.current.add(t)
    }
  }

  if (answers.length === 0 && streamingAnswers.length === 0) {
    return (
      <Empty
        title="等待转写…"
        hint="收到面试音频转写后,AI 答案会显示在这里"
      />
    )
  }

  return (
    <div ref={feedRef} onScroll={handleScroll} className="h-full overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-4xl space-y-4">
        {answers.map((a) => {
          const searchEnhanced = a.source === 'search+llm'
          return (
            <article
              key={a.id}
              className="animate-slide-up rounded-xl border border-stroke bg-surface-card p-5 shadow-card"
            >
              <div className="mb-2 flex items-start justify-between gap-2">
                <div className="text-sm font-semibold leading-6 text-ink-primary">
                  {a.question}
                </div>
                <span
                  className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                    searchEnhanced
                      ? 'bg-[#a78bfa]/12 text-[#a78bfa]'
                      : 'bg-good/10 text-good'
                  }`}
                >
                  {searchEnhanced ? <Globe size={10} /> : <Sparkles size={10} />}
                  {searchEnhanced ? '搜索增强' : 'LLM'}
                </span>
              </div>
              {a.thinking && (
                <div className="mb-3 border-l-2 border-ink-faint/30 pl-3 text-xs leading-5 text-ink-faint">
                  <div className="mb-1 text-[11px] font-medium text-ink-muted">思考过程</div>
                  <div className="whitespace-pre-wrap">{a.thinking}</div>
                </div>
              )}
              <Markdown source={a.answer} variant="answer" />
              <div className="mt-3 flex items-center justify-between">
                <span className="tnum text-[11px] text-ink-faint">
                  {dayjs(a.created_at).format('HH:mm:ss')}
                </span>
                <Button
                  variant="ghost"
                  icon={<RefreshCw size={12} />}
                  loading={regeneratingIds.has(a.id)}
                  disabled={regeneratingIds.has(a.id)}
                  onClick={() => void handleRegenerate(a)}
                  className="!px-2 !py-1 !text-xs"
                >
                  重新生成
                </Button>
              </div>
            </article>
          )
        })}
        {streamingAnswers.map((streamingAnswer) => (
          <article
            key={streamingAnswer.request_id}
            className="rounded-xl border border-brand/40 bg-brand/[0.055] p-5 shadow-[0_18px_50px_rgba(0,0,0,0.18)]"
            aria-live="polite"
          >
            <div className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-good">
              <Loader2 size={12} className="animate-spin" />
              AI 正在输出
            </div>
            <div className="mb-3 text-sm font-semibold leading-6 text-ink-primary">
              {streamingAnswer.question}
            </div>
            {streamingAnswer.thinking && (
              <div className="mb-3 border-l-2 border-ink-faint/30 pl-3 text-xs leading-5 text-ink-faint">
                <div className="mb-1 text-[11px] font-medium text-ink-muted">思考过程</div>
                <div className="whitespace-pre-wrap">{streamingAnswer.thinking}</div>
              </div>
            )}
            <Markdown source={streamingAnswer.answer} variant="answer" />
          </article>
        ))}
      </div>
    </div>
  )
}
