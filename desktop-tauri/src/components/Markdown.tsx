/**
 * Markdown 渲染组件:token → React 元素。
 *
 * 文本仅作为 React 文本子节点输出(自动转义),全程不接触 innerHTML,
 * 因此 <script> 之类内容只会字面显示,无 XSS 执行面。
 */
import { Fragment, type ReactNode } from 'react'
import { parseBlocks, parseInline, type MarkdownBlock } from '../shared/markdown'

// 行内 code / 围栏 code 统一样式 token,与暗色主题一致
const INLINE_CODE_CLASS =
  'rounded bg-surface-hover px-1 py-0.5 font-mono text-[12px] text-ink-primary'
const CODE_BLOCK_CLASS = 'overflow-x-auto rounded-lg border border-stroke-subtle bg-surface px-3 py-2.5 font-mono text-[12px] leading-5 text-ink-primary'

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
      return (
        <pre className={CODE_BLOCK_CLASS}>
          <code>{block.lines.join('\n')}</code>
        </pre>
      )
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
      className={`${variant === 'answer' ? 'text-sm leading-7 text-ink-primary/90' : 'text-[13px] leading-6 text-ink-secondary'} ${
        variant === 'relaxed' ? 'space-y-4' : variant === 'answer' ? 'space-y-2.5' : 'space-y-2'
      }`}
    >
      {blocks.map((block, i) => (
        <Block key={i} block={block} />
      ))}
    </div>
  )
}
