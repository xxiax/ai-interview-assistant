/**
 * Markdown 渲染组件:token → React 元素。
 *
 * 文本仅作为 React 文本子节点输出(自动转义),全程不接触 innerHTML,
 * 因此 <script> 之类内容只会字面显示,无 XSS 执行面。
 *
 * 代码高亮同样走 token(`shared/highlight.ts`),不引 highlight.js:
 * 那类库输出 HTML 字符串,接上去就要开 dangerouslySetInnerHTML,把上面这条
 * 安全结论作废。
 */
import { Fragment, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { parseBlocks, parseInline, type MarkdownBlock } from '../shared/markdown'
import { highlight, resolveLang, type HighlightKind } from '../shared/highlight'

// 行内 code / 围栏 code 统一样式 token,与暗色主题一致。
// code-block / code-lang / inline-code 是给 CSS 用的稳定钩子:悬浮窗
// (body.overlay-root)里靠它们把主窗口的实色浅底与彩色高亮覆盖成黑白灰。
const INLINE_CODE_CLASS =
  'inline-code rounded bg-surface-hover px-1 py-0.5 font-mono text-[12px] text-ink-primary'
const CODE_BLOCK_CLASS = 'overflow-x-auto px-3 py-2.5 font-mono text-[12px] leading-5 text-ink-primary'

/**
 * 高亮配色。用 Tailwind 语义 token 而不是硬编码 hex,跟着主题走。
 * 只有 5 种颜色:面试时是扫读代码,颜色越多越难抓重点。
 */
const HIGHLIGHT_CLASS: Record<HighlightKind, string> = {
  plain: '',
  comment: 'text-ink-faint italic',
  string: 'text-good',
  number: 'text-warn',
  keyword: 'text-brand font-medium',
  builtin: 'text-ink-secondary'
}

/** 围栏代码块。带语言标签和复制按钮:笔试题的代码要能一键拿走。 */
function CodeBlock({ lines, lang }: { lines: string[]; lang: string }) {
  const [copied, setCopied] = useState(false)
  const code = lines.join('\n')
  const resolved = resolveLang(lang)
  const tokens = highlight(code, lang)

  const copy = () => {
    // clipboard 在非安全上下文/无权限时会 reject,失败就别假装成功。
    void navigator.clipboard
      ?.writeText(code)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => undefined)
  }

  return (
    <div className="code-block group relative overflow-hidden rounded-lg border border-stroke-subtle bg-surface">
      <div className="flex items-center justify-between border-b border-stroke-subtle/60 px-3 py-1">
        <span className="code-lang font-mono text-[10px] uppercase tracking-wider text-ink-faint">
          {resolved ?? lang.trim() ?? ''}
        </span>
        <button
          type="button"
          onClick={copy}
          title={copied ? '已复制' : '复制代码'}
          aria-label={copied ? '已复制' : '复制代码'}
          className="code-copy flex h-6 items-center gap-1 rounded px-1.5 text-[10px] text-ink-faint transition-colors hover:bg-surface-hover hover:text-ink-primary"
        >
          {copied ? <Check size={11} className="text-good" /> : <Copy size={11} />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className={CODE_BLOCK_CLASS}>
        <code>
          {tokens.map((token, i) =>
            token.kind === 'plain' ? (
              <Fragment key={i}>{token.text}</Fragment>
            ) : (
              <span key={i} className={HIGHLIGHT_CLASS[token.kind]}>
                {token.text}
              </span>
            )
          )}
        </code>
      </pre>
    </div>
  )
}

function InlineText({ text }: { text: string }) {
  const parts = text.split('\n')
  return (
    <>
      {parts.map((p, i) => (
        <Fragment key={i}>
          {i > 0 && <br />}
          {renderInline(p)}
        </Fragment>
      ))}
    </>
  )
}

function renderInline(line: string): ReactNode[] {
  return parseInline(line).map((token, i) => {
    switch (token.kind) {
      case 'strong':
        return (
          <strong key={i} className="font-semibold text-ink-primary">
            {token.text}
          </strong>
        )
      case 'em':
        return (
          <em key={i} className="italic">
            {token.text}
          </em>
        )
      case 'code':
        return (
          <code key={i} className={INLINE_CODE_CLASS}>
            {token.text}
          </code>
        )
      default:
        return <Fragment key={i}>{token.text}</Fragment>
    }
  })
}

function Block({ block }: { block: MarkdownBlock }) {
  switch (block.type) {
    case 'heading': {
      // 在 feed 的 13px 语境下压缩层级差,## 比 13px 略大,层级越高越接近正文
      const size = ['15px', '14px', '13px', '13px'][block.level - 1] ?? '13px'
      const Tag = (block.level <= 2 ? 'h3' : 'h4') as 'h3' | 'h4'
      return (
        <Tag
          className="font-semibold text-ink-primary"
          style={{ fontSize: size, lineHeight: 1.5 }}
        >
          {renderInline(block.text)}
        </Tag>
      )
    }
    case 'list': {
      const items = block.items.map((item, i) => (
        <li key={i} className="flex gap-2">
          {block.ordered ? (
            <span className="tnum shrink-0 text-ink-muted">{i + 1}.</span>
          ) : (
            <span
              aria-hidden
              className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-ink-muted"
            />
          )}
          <span className="min-w-0 flex-1">{renderInline(item)}</span>
        </li>
      ))
      return block.ordered ? (
        <ol className="ml-1 space-y-1">{items}</ol>
      ) : (
        <ul className="ml-1 space-y-1">{items}</ul>
      )
    }
    case 'code':
      return <CodeBlock lines={block.lines} lang={block.lang} />
    default:
      return (
        <p className="whitespace-pre-wrap break-words">
          <InlineText text={block.text} />
        </p>
      )
  }
}

/**
 * 统一 Markdown 渲染入口。
 * `variant` 仅影响块间距:答案卡内紧凑,复盘长文稍宽松。
 */
export default function Markdown({
  source,
  variant = 'compact'
}: {
  source: string
  variant?: 'compact' | 'relaxed' | 'answer'
}) {
  const blocks = parseBlocks(source)
  if (blocks.length === 0) return null
  return (
    <div
      // select-text：正文放开文本选择（用户要能划选复制答案），body 默认
      // user-select:none 只保住 chrome（工具条/标题栏/按钮）不可选。
      className={`select-text ${variant === 'answer' ? 'text-sm leading-7 text-ink-primary/90' : 'text-[13px] leading-6 text-ink-secondary'} ${
        variant === 'relaxed' ? 'space-y-4' : variant === 'answer' ? 'space-y-2.5' : 'space-y-2'
      }`}
    >
      {blocks.map((block, i) => (
        <Block key={i} block={block} />
      ))}
    </div>
  )
}
