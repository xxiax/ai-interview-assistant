import assert from 'node:assert/strict'
import test from 'node:test'
import { registerTsExtensionResolve } from './helpers.mjs'

await registerTsExtensionResolve()

const { parseBlocks, parseInline } = await import('../src/shared/markdown.ts')

test('parseBlocks: **bold** paragraph with inline strong token', () => {
  const [block] = parseBlocks('优先保证 **时间复杂度** 与可读性')
  assert.equal(block.type, 'paragraph')
  const tokens = parseInline(block.text)
  assert.deepEqual(tokens, [
    { kind: 'text', text: '优先保证 ' },
    { kind: 'strong', text: '时间复杂度' },
    { kind: 'text', text: ' 与可读性' }
  ])
})

test('parseInline: *italic* and `code` tokens', () => {
  assert.deepEqual(parseInline('*重点* 看这里'), [
    { kind: 'em', text: '重点' },
    { kind: 'text', text: ' 看这里' }
  ])
  assert.deepEqual(parseInline('用 `Map<id, T>` 存储'), [
    { kind: 'text', text: '用 ' },
    { kind: 'code', text: 'Map<id, T>' },
    { kind: 'text', text: ' 存储' }
  ])
})

test('parseInline: code span keeps ** literal (code takes precedence)', () => {
  assert.deepEqual(parseInline('`a ** b`'), [{ kind: 'code', text: 'a ** b' }])
})

test('parseBlocks: ## / ### headings with level', () => {
  const blocks = parseBlocks('## 核心思路\n正文\n### 细节\n- 项目')
  assert.deepEqual(
    blocks.map((b) => b.type),
    ['heading', 'paragraph', 'heading', 'list']
  )
  assert.equal(blocks[0].level, 2)
  assert.equal(blocks[2].level, 3)
  assert.equal(blocks[0].text, '核心思路')
})

test('parseBlocks: adjacent bullet lines merge into one list block', () => {
  const blocks = parseBlocks('建议:\n- 先澄清需求\n- 再给方案\n- 最后补充风险')
  assert.equal(blocks.length, 2)
  assert.equal(blocks[0].type, 'paragraph')
  assert.equal(blocks[1].type, 'list')
  assert.equal(blocks[1].ordered, false)
  assert.deepEqual(blocks[1].items, ['先澄清需求', '再给方案', '最后补充风险'])
})

test('parseBlocks: ordered list (1. / 2.) marked ordered', () => {
  const [list] = parseBlocks('1. 第一步\n2. 第二步').slice(-1)
  assert.equal(list.type, 'list')
  assert.equal(list.ordered, true)
  assert.deepEqual(list.items, ['第一步', '第二步'])
})

test('parseBlocks: line starting with *emphasis* is not treated as bullet', () => {
  // BULLET_RE 要求标记后有空白;*重点* 是斜体,不是列表
  const [block] = parseBlocks('*重点*内容开头的段落')
  assert.equal(block.type, 'paragraph')
})

test('parseBlocks: fenced code block captured verbatim, no inline parsing', () => {
  const blocks = parseBlocks('前文\n```\nconst x = <script>alert(1)</script>\n**not bold**\n```\n后文')
  assert.equal(blocks.length, 3)
  assert.equal(blocks[1].type, 'code')
  assert.deepEqual(blocks[1].lines, [
    'const x = <script>alert(1)</script>',
    '**not bold**'
  ])
  assert.equal(blocks[2].text, '后文')
})

test('parseBlocks: unterminated fence degrades to end of input', () => {
  const [code] = parseBlocks('```\nabc')
  assert.equal(code.type, 'code')
  assert.deepEqual(code.lines, ['abc'])
})

test('parseBlocks: blank lines split paragraphs; single newline stays in one paragraph', () => {
  const blocks = parseBlocks('第一段\n第二行\n\n第二段')
  assert.equal(blocks.length, 2)
  assert.equal(blocks[0].text, '第一段\n第二行')
  assert.equal(blocks[1].text, '第二段')
})

test('parseBlocks: CRLF normalized', () => {
  const [block] = parseBlocks('a\r\nb\r\n\r\nc')
  assert.equal(block.text, 'a\nb')
})

test('parseBlocks: malicious markup stays plain text (no HTML output surface)', () => {
  // 输出是结构化 token 而非 HTML 字符串;<script> 只能成为字面文本
  const blocks = parseBlocks('<script>alert(1)</script>\n- <img src=x onerror=alert(1)>')
  assert.equal(blocks[0].type, 'paragraph')
  assert.equal(blocks[0].text, '<script>alert(1)</script>')
  assert.deepEqual(blocks[1].items, ['<img src=x onerror=alert(1)>'])
})

test('parseInline: unterminated markers stay literal', () => {
  assert.deepEqual(parseInline('2 ** 3 = 8'), [{ kind: 'text', text: '2 ** 3 = 8' }])
  assert.deepEqual(parseInline('a * b'), [{ kind: 'text', text: 'a * b' }])
})

test('parseInline: empty and plain lines return single text token', () => {
  assert.deepEqual(parseInline('普通文本'), [{ kind: 'text', text: '普通文本' }])
})

test('parseBlocks: empty input yields no blocks', () => {
  assert.deepEqual(parseBlocks(''), [])
  assert.deepEqual(parseBlocks('\n\n  \n'), [])
})
