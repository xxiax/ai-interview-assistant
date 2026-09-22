import { memo, useCallback, useEffect, useRef, useState } from 'react'
import dayjs from 'dayjs'
import { Globe, Loader2, RefreshCw, Sparkles, TriangleAlert } from 'lucide-react'
import type { Answer } from '../../shared/types'
import { pickDisplayVersion, type AnswerThread } from '../../shared/answer-threads'
import { api } from '../../api/bridge'
import { errorMessage } from '../../shared/errors'
import { Button, Empty } from '../../components/ui'
import Markdown from '../../components/Markdown'

function toast(kind: string, message: string) {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}

function SourceBadge({ searchEnhanced }: { searchEnhanced: boolean }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
        searchEnhanced ? 'bg-[#a78bfa]/12 text-[#a78bfa]' : 'bg-good/10 text-good'
      }`}
    >
      {searchEnhanced ? <Globe size={10} /> : <Sparkles size={10} />}
      {searchEnhanced ? '搜索增强' : 'LLM'}
    </span>
  )
}

// ---------- H2:答案卡 memo 化 ----------
//
// token 帧到达时派生层(buildAnswerFeed)会整体重建 threads,但未变化卡片的
// 数据值不变。React.memo 让这些卡片跳过重渲染——正在流式的那张卡之外的卡,
// 连 Markdown 解析缓存都不会被击穿。

/** 历史答案卡:answer 来自 store,条目引用稳定,默认浅比较即生效。 */
const HistoryAnswerCard = memo(function HistoryAnswerCard({
  answer,
  cooling,
  onRegenerate
}: {
  answer: Answer
  cooling: boolean
  onRegenerate: (answer: Answer) => void
}) {
  return (
    <article className="animate-slide-up rounded-xl border border-stroke bg-surface-card p-5 shadow-card">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="select-text text-sm font-semibold leading-6 text-ink-primary">{answer.question}</div>
        <SourceBadge searchEnhanced={answer.source === 'search+llm'} />
      </div>
      <Markdown source={answer.answer} variant="answer" />
      <div className="mt-3 flex items-center justify-between">
        <span className="tnum text-[11px] text-ink-faint">
          {dayjs(answer.created_at).format('HH:mm:ss')}
        </span>
        <Button
          variant="ghost"
          icon={<RefreshCw size={12} />}
          loading={cooling}
          disabled={cooling}
          onClick={() => onRegenerate(answer)}
          className="!px-2 !py-1 !text-xs"
        >
          重新生成
        </Button>
      </div>
    </article>
  )
})

/**
 * 线程卡 props 的值比较:thread 对象每次派生都是新引用,默认浅比较对它无效。
 * 卡片实际只渲染标题/来源/展示版(pickDisplayVersion)内容,按这些字段比较;
 * 全部一致则跳过重渲染。
 */
function threadCardPropsEqual(
  prev: ThreadCardProps,
  next: ThreadCardProps
): boolean {
  if (prev.cooling !== next.cooling || prev.onRegenerate !== next.onRegenerate) return false
  const a = prev.thread
  const b = next.thread
  if (a === b) return true
  if (a.key !== b.key || a.question !== b.question || a.source !== b.source) return false
  if (a.persisted?.id !== b.persisted?.id) return false
  const va = pickDisplayVersion(a.versions)
  const vb = pickDisplayVersion(b.versions)
  if (!va || !vb) return va === vb
  return (
    va.request_id === vb.request_id &&
    va.revision === vb.revision &&
    va.answer === vb.answer &&
    va.done === vb.done &&
    va.failed === vb.failed &&
    va.error === vb.error
  )
}

interface ThreadCardProps {
  thread: AnswerThread
  cooling: boolean
  onRegenerate: (thread: AnswerThread) => void
}

const ThreadCard = memo(function ThreadCard({ thread, cooling, onRegenerate }: ThreadCardProps) {
  const display = pickDisplayVersion(thread.versions)
  return (
    <article
      className="rounded-xl border border-brand/40 bg-brand/[0.055] p-5 shadow-[0_18px_50px_rgba(0,0,0,0.18)]"
      aria-live="polite"
    >
      {/* 标题就是不断增长的累计问题;每来一版更长的 partial 就整体刷新。 */}
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="select-text text-sm font-semibold leading-6 text-ink-primary">
          {thread.question}
        </div>
        <SourceBadge searchEnhanced={thread.source === 'search+llm'} />
      </div>
      {/*
       * catch-up swap：一张卡同一时刻只展示一段——未被取代的段里答案
       * 最长的那个（新版流式追平旧版长度即自然接管，永不闪断）。
       * 标题已经是累计问题，段内不再重复"问题："一行。
       */}
      {display ? (
        <div>
          {display.answer ? (
            <Markdown source={display.answer} variant="answer" />
          ) : (
            <div className="flex items-center gap-1.5 text-[11px] text-ink-faint">
              {display.failed ? (
                <TriangleAlert size={12} className="text-bad" />
              ) : (
                <Loader2 size={12} className="animate-spin" />
              )}
              {display.failed ? display.error || '这一段生成失败' : '正在生成…'}
            </div>
          )}
          {display.answer && display.failed && (
            <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-bad">
              <TriangleAlert size={12} />
              这一段中断，仅显示已生成内容
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-1.5 text-[11px] text-ink-faint">
          <Loader2 size={12} className="animate-spin" />
          正在生成…
        </div>
      )}
      {/*
       * 常驻重新生成：不管答案出来没有、是否还在生成，都能点——
       * 生成卡住时这是唯一的自救手段。后端会取消该线程在途任务。
       */}
      <div className="mt-3 flex items-center justify-end">
        <Button
          variant="ghost"
          icon={<RefreshCw size={12} />}
          loading={cooling}
          disabled={cooling}
          onClick={() => onRegenerate(thread)}
          className="!px-2 !py-1 !text-xs"
        >
          重新生成
        </Button>
      </div>
    </article>
  )
}, threadCardPropsEqual)

export default function AnswerFeed({
  answers,
  threads = [],
  pending = []
}: {
  answers: Answer[]
  /** 实时问题线程：一个问题一张卡，卡内每版答案一段。 */
  threads?: AnswerThread[]
  /** 已发出、还没等到 answer_stream 首帧的手动提问（"正在思考"卡）。 */
  pending?: { id: number; question: string }[]
}) {
  // 冷却按卡记录（历史答案 id / 线程 key）：一张卡重新生成时其余按钮仍可用。
  const [coolingKeys, setCoolingKeys] = useState<Set<string>>(new Set())
  const cooldownTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())

  // 卸载清理冷却定时器,避免卸载后 setState 脏更新
  useEffect(() => {
    const timers = cooldownTimersRef.current
    return () => {
      for (const t of timers) clearTimeout(t)
      timers.clear()
    }
  }, [])

  const startCooldown = useCallback((key: string) => {
    // 后端限流 10/min:按钮冷却 6.5s,连点不触发限流报错。
    const t = setTimeout(() => {
      cooldownTimersRef.current.delete(t)
      setCoolingKeys((prev) => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }, 6500)
    cooldownTimersRef.current.add(t)
    setCoolingKeys((prev) => new Set(prev).add(key))
  }, [])

  // 历史/已入库答案的重新生成：不带 thread_id，独立成卡立即入库。
  // useCallback + 值稳定 props:token 帧重渲染时这些引用不变,memo 卡才跳得过。
  const handleRegenerateAnswer = useCallback(
    async (answer: Answer) => {
      const key = `answer:${answer.id}`
      if (coolingKeys.has(key)) return
      startCooldown(key)
      try {
        const ok = await api.live.regenerate(answer.question, false)
        if (!ok) toast('warning', '当前无法发送请求(连接未就绪或队列已满)')
      } catch (err) {
        toast('error', errorMessage(err))
      }
    },
    [coolingKeys, startCooldown]
  )

  // 问题卡重新生成：带 thread_id，后端 revision+1 并流回同一张卡。
  // 生成中也可点，新旧版本由会话并发上限统一调度。
  const handleRegenerateThread = useCallback(
    async (thread: AnswerThread) => {
      const key = `thread:${thread.key}`
      if (coolingKeys.has(key)) return
      startCooldown(key)
      try {
        const ok = await api.live.regenerate(thread.question, false, thread.key)
        if (!ok) toast('warning', '当前无法发送请求(连接未就绪或队列已满)')
      } catch (err) {
        toast('error', errorMessage(err))
      }
    },
    [coolingKeys, startCooldown]
  )

  if (answers.length === 0 && threads.length === 0 && pending.length === 0) {
    return (
      <Empty
        title="等待转写…"
        hint="收到面试音频转写后,AI 答案会显示在这里"
      />
    )
  }

  return (
    <div className="h-full overflow-y-auto px-6 py-5">
      {/*
       * 最新在最上（2026-09-22 用户拍板）：pending → 实时线程 → 历史答案。
       * 新内容从顶部插入、把旧的往下挤，流式输出不打扰阅读位置——所以这里
       * 没有任何自动滚动。
       */}
      <div className="mx-auto max-w-4xl space-y-4">
        {/*
         * 刚发出的手动提问（pending）：发送成功即出现在这里，answer_stream
         * 首帧到达自动消失、由真卡接棒（真卡对空答案也显示"正在生成…"，
         * 全程无空窗）。用户由此确认"发出去了、AI 在想"，不用反复重发。
         */}
        {pending.map((p) => (
          <article
            key={`pending:${p.id}`}
            className="rounded-xl border border-brand/40 bg-brand/[0.055] p-5 shadow-[0_18px_50px_rgba(0,0,0,0.18)]"
            aria-live="polite"
          >
            <div className="mb-2 select-text text-sm font-semibold leading-6 text-ink-primary">
              {p.question}
            </div>
            <div className="flex items-center gap-1.5 text-[11px] text-ink-faint">
              <Loader2 size={12} className="animate-spin" />
              已发送 · 正在思考…
            </div>
          </article>
        ))}
        {threads.map((thread) => (
          <ThreadCard
            key={thread.key}
            thread={thread}
            cooling={coolingKeys.has(`thread:${thread.key}`)}
            onRegenerate={handleRegenerateThread}
          />
        ))}
        {answers.map((a) => (
          <HistoryAnswerCard
            key={a.id}
            answer={a}
            cooling={coolingKeys.has(`answer:${a.id}`)}
            onRegenerate={handleRegenerateAnswer}
          />
        ))}
      </div>
    </div>
  )
}
