import type { AudioSource, Transcript } from './types'

export interface TranscriptDisplayGroup {
  key: number
  ids: number[]
  source: AudioSource
  text: string
  timestamp: string
  lastChunkSeq: number | null
  lastCapturedAtMs: number
}

const MAX_CONTIGUOUS_GAP_MS = 6_000

function transcriptTime(transcript: Transcript): number {
  const value = Date.parse(transcript.captured_at || transcript.timestamp)
  return Number.isFinite(value) ? value : 0
}

function joinDisplayText(left: string, right: string): string {
  const separator = /[A-Za-z0-9]$/.test(left) && /^[A-Za-z0-9]/.test(right) ? ' ' : ''
  return `${left}${separator}${right}`
}

/** 只合并相邻 final 的视觉呈现，不改变后端分片、历史记录或 LLM 触发。 */
export function groupFinalTranscripts(transcripts: Transcript[]): TranscriptDisplayGroup[] {
  const groups: TranscriptDisplayGroup[] = []
  for (const transcript of transcripts) {
    const capturedAtMs = transcriptTime(transcript)
    const chunkSeq = typeof transcript.chunk_seq === 'number' ? transcript.chunk_seq : null
    const previous = groups.at(-1)
    const consecutiveChunk =
      previous?.lastChunkSeq == null || chunkSeq == null || chunkSeq === previous.lastChunkSeq + 1
    const contiguous =
      previous &&
      previous.source === transcript.source &&
      consecutiveChunk &&
      capturedAtMs >= previous.lastCapturedAtMs &&
      capturedAtMs - previous.lastCapturedAtMs <= MAX_CONTIGUOUS_GAP_MS

    if (contiguous) {
      previous.ids.push(transcript.id)
      previous.text = joinDisplayText(previous.text, transcript.text)
      previous.lastChunkSeq = chunkSeq
      previous.lastCapturedAtMs = capturedAtMs
      continue
    }

    groups.push({
      key: transcript.id,
      ids: [transcript.id],
      source: transcript.source,
      text: transcript.text,
      timestamp: transcript.timestamp,
      lastChunkSeq: chunkSeq,
      lastCapturedAtMs: capturedAtMs
    })
  }
  return groups
}
