import type { Answer, AnswerSource, StreamingAnswer } from './types'

/** 一次 LLM 请求产生的一段答案。同一问题的每一版累计 partial 各占一段。 */
export interface AnswerVersion {
  request_id: string
  revision: number
  /** 这一段实际发给 LLM 的问题文本（该 revision 当时的累计 partial）。 */
  question: string
  answer: string
  done: boolean
  failed: boolean
  /** 被同线程更新 revision 取代：内容冻结保留，不参与展示版挑选。 */
  superseded?: boolean
}

/**
 * 一个问题（一条 `thread_id`）在实时页里的一张卡片：
 * 标题是不断增长的累计问题，卡内按 `revision` 顺序排列多段答案。
 */
export interface AnswerThread {
  key: string
  session_id: string
  question: string
  source: AnswerSource
  versions: AnswerVersion[]
  /** 该问题已落库的最终答案（存在时卡片可显示时间并允许重新生成）。 */
  persisted?: Answer
}

export interface AnswerFeedModel {
  /** 未被任何实时卡片覆盖的历史答案（REST 回填、跨会话重连补拉等）。 */
  historyAnswers: Answer[]
  threads: AnswerThread[]
}

function pickQuestion(current: string, candidate: string): string {
  // 累计 partial 只会变长；取更长的一版，避免更旧的短问题把标题改回去。
  return candidate.length >= current.length ? candidate : current
}

/**
 * 挑出一张卡当前应该展示的那一段（catch-up swap 语义）。
 *
 * 同一线程可能同时存在多段：新版已在流式输出，旧版还没来得及收到
 * superseded 终止帧；或者新版失败/被取消，旧版反而是最长的可读答案。
 * 规则：在**未被取代**的段里挑答案最长的（正在追平的新版一旦长度超过旧版
 * 就自然接管）；并列取 revision 最新的。没有任何一段有答案时退回 revision
 * 最高的段（显示它的生成中/失败状态）。
 */
export function pickDisplayVersion(versions: AnswerVersion[]): AnswerVersion | undefined {
  if (versions.length === 0) return undefined
  let best: AnswerVersion | undefined
  for (const version of versions) {
    if (version.superseded) continue
    if (best === undefined) {
      best = version
      continue
    }
    const a = version.answer.length
    const b = best.answer.length
    if (a > b || (a === b && version.revision > best.revision)) best = version
  }
  return best ?? versions.reduce((hi, v) => (v.revision > hi.revision ? v : hi))
}

/**
 * 把按 `request_id` 存放的实时答案聚合成"一个问题一张卡、卡内多段"的结构，
 * 并挑出已经被卡片覆盖的历史答案，避免同一段内容在页面上出现两次。
 *
 * 分段**不入库**，但在会话期间不会被最终答案清掉：用户要能一直回看每一版。
 */
export function buildAnswerFeed(
  streamingAnswers: StreamingAnswer[],
  answers: Answer[]
): AnswerFeedModel {
  const byKey = new Map<string, AnswerThread>()
  for (const item of streamingAnswers) {
    const key = item.thread_id ?? item.request_id
    const thread = byKey.get(key)
    if (thread === undefined) {
      byKey.set(key, {
        key,
        session_id: item.session_id,
        question: item.question,
        source: item.source,
        versions: [
          {
            request_id: item.request_id,
            revision: item.revision,
            question: item.question,
            answer: item.answer,
            done: item.done,
            failed: item.failed,
            superseded: item.superseded
          }
        ]
      })
      continue
    }
    thread.question = pickQuestion(thread.question, item.question)
    thread.source = item.source
    thread.versions.push({
      request_id: item.request_id,
      revision: item.revision,
      question: item.question,
      answer: item.answer,
      done: item.done,
      failed: item.failed,
      superseded: item.superseded
    })
  }

  const threads = [...byKey.values()]
  for (const thread of threads) {
    thread.versions.sort((a, b) => a.revision - b.revision)
  }

  const covered = new Set<number>()
  for (const answer of answers) {
    const match = threads.find(
      (thread) =>
        (typeof answer.thread_id === 'string' && answer.thread_id === thread.key) ||
        (typeof answer.request_id === 'string' &&
          thread.versions.some((version) => version.request_id === answer.request_id))
    )
    if (match === undefined) continue
    covered.add(answer.id)
    // 同一线程多次落库时保留最后一条（id 递增即时间顺序）。
    if (match.persisted === undefined || match.persisted.id < answer.id) {
      match.persisted = answer
    }
  }

  return {
    historyAnswers: answers.filter((answer) => !covered.has(answer.id)),
    threads
  }
}
