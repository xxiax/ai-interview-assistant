/**
 * 极简 Markdown 解析(纯函数,零依赖)。
 *
 * 只覆盖系统提示词约束的答案格式:**bold**、*italic*、`code`、#/##/### 标题、
 * - 列表、1. 列表、``` 围栏代码块、空行分段与段内换行。
 *
 * 安全模型:输出是结构化 token(而非 HTML 字符串),由 React 组件映射为元素;
 * 文本只作为 React 文本子节点渲染,框架自动转义。`<script>`、`onerror=` 等
 * 内容只会成为字面文本——不存在 innerHTML/dangerouslySetInnerHTML 通道,
 * 因此无需 DOMPurify 等 sanitizer,也没有 XSS 执行面。
 */

export type MarkdownBlock =
  | { type: 'heading'; level: number; text: string }
  | { type: 'paragraph'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'code'; lines: string[]; lang: string }

export type InlineToken =
  | { kind: 'text'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'em'; text: string }
  | { kind: 'code'; text: string }

// 标题:# ~ ####(后接空白;单独的 # 不算)
const HEADING_RE = /^(#{1,4})\s+(.*)$/
// 无序列表:- / * / +(标记后必须有空白,因此行首 *emphasis* 不会被误判)
const BULLET_RE = /^[-*+]\s+(.*)$/
// 有序列表:1. / 1、 / 1)
const ORDERED_RE = /^\d+(?:[.、)])\s+(.*)$/
// 围栏代码块:行首(去空白)``` ,后面可跟语言标记
const FENCE_RE = /^```/
// 围栏语言标记:```python / ```ts 。只取首个单词,忽略 ```js {highlight=1} 之类附加参数
const FENCE_LANG_RE = /^```+\s*([A-Za-z0-9+#._-]*)/
// 行内标记:代码优先(反引号内的 ** * 保持字面),其次粗体、斜体
const INLINE_RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)/g

/** 块级解析:行流 → 块数组。相邻同类列表行合并为一个块,空行分段。 */
export function parseBlocks(source: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = []
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i++
      continue
    }
    // 围栏代码块:内容原样保留(不做行内解析),直到闭合围栏或末尾
    if (FENCE_RE.test(line.trim())) {
      const lang = (FENCE_LANG_RE.exec(line.trim())?.[1] ?? '').toLowerCase()
      const buf: string[] = []
      i++
      while (i < lines.length && !FENCE_RE.test(lines[i].trim())) {
        buf.push(lines[i])
        i++
      }
      i++ // 跳过闭合围栏(未闭合时容错读到末尾)
      blocks.push({ type: 'code', lines: buf, lang })
      continue
    }
    const heading = HEADING_RE.exec(line)
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2].trim() })
      i++
      continue
    }
    const orderedMarker = ORDERED_RE.test(line)
    if (orderedMarker || BULLET_RE.test(line)) {
      const items: string[] = []
      while (i < lines.length) {
        const m = orderedMarker ? ORDERED_RE.exec(lines[i]) : BULLET_RE.exec(lines[i])
        if (!m) break
        items.push(m[1].trim())
        i++
      }
      blocks.push({ type: 'list', ordered: orderedMarker, items })
      continue
    }
    // 段落:累积到空行或任意块级起始符;段内单个换行保留(组件渲染为 <br/>)
    const buf: string[] = [line]
    i++
    while (
      i < lines.length &&
      lines[i].trim() &&
      !HEADING_RE.test(lines[i]) &&
      !BULLET_RE.test(lines[i]) &&
      !ORDERED_RE.test(lines[i]) &&
      !FENCE_RE.test(lines[i].trim())
    ) {
      buf.push(lines[i])
      i++
    }
    blocks.push({ type: 'paragraph', text: buf.join('\n') })
  }
  return blocks
}

/** 行内解析:单行文本 → token 序列(不含换行)。 */
export function parseInline(line: string): InlineToken[] {
  const tokens: InlineToken[] = []
  let last = 0
  for (const m of line.matchAll(INLINE_RE)) {
    const idx = m.index ?? 0
    if (idx > last) tokens.push({ kind: 'text', text: line.slice(last, idx) })
    const raw = m[0]
    if (raw.startsWith('`')) tokens.push({ kind: 'code', text: raw.slice(1, -1) })
    else if (raw.startsWith('**')) tokens.push({ kind: 'strong', text: raw.slice(2, -2) })
    else tokens.push({ kind: 'em', text: raw.slice(1, -1) })
    last = idx + raw.length
  }
  if (last < line.length) tokens.push({ kind: 'text', text: line.slice(last) })
  return tokens
}
