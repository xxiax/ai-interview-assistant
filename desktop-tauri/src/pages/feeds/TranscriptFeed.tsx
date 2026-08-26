import { useLayoutEffect, useRef } from 'react'
import dayjs from 'dayjs'
import { Laptop, Smartphone } from 'lucide-react'
import type { PartialTranscript, Transcript } from '../../shared/types'
import { groupFinalTranscripts } from '../../shared/transcript-display'
import { Empty } from '../../components/ui'

export default function TranscriptFeed({
  transcripts,
  partialTranscript = null
}: {
  transcripts: Transcript[]
  partialTranscript?: PartialTranscript | null
}) {
  const feedRef = useRef<HTMLDivElement>(null)
  const followTailRef = useRef(true)

  useLayoutEffect(() => {
    const feed = feedRef.current
    if (feed && followTailRef.current) feed.scrollTop = feed.scrollHeight
  }, [transcripts.length, partialTranscript?.text])

  const handleScroll = () => {
    const feed = feedRef.current
    if (!feed) return
    followTailRef.current = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 64
  }

  if (transcripts.length === 0 && !partialTranscript) {
    return (
      <Empty
        title="暂无转写内容"
        hint="开始采集后,音频将实时转写并显示在这里"
      />
    )
  }

  const displayGroups = groupFinalTranscripts(transcripts)

  return (
    <div ref={feedRef} onScroll={handleScroll} className="h-full overflow-y-auto px-4 py-3">
      <div className="space-y-3">
        {displayGroups.map((t) => {
          const isPc = t.source === 'pc'
          return (
            <div key={t.key} className="flex gap-3">
              <div
                className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded ${
                  isPc ? 'bg-brand/12 text-brand' : 'bg-[#a78bfa]/12 text-[#a78bfa]'
                }`}
                title={isPc ? '电脑采集' : '手机采集'}
              >
                {isPc ? <Laptop size={11} /> : <Smartphone size={11} />}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-[13px] leading-5 text-ink-secondary">{t.text}</div>
                <div className="tnum mt-0.5 text-[11px] text-ink-faint">
                  {dayjs(t.timestamp).format('HH:mm:ss')}
                </div>
              </div>
            </div>
          )
        })}
        {partialTranscript && (
          <div className="flex gap-3 border-l-2 border-brand/60 pl-3 opacity-70">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] leading-5 italic text-ink-secondary">
                {partialTranscript.text}
              </div>
              <div className="mt-0.5 text-[11px] text-brand">识别中</div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
