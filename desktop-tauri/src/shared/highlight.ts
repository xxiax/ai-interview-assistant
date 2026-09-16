/**
 * 代码高亮（纯函数，零依赖）。
 *
 * 为什么自己写而不是引 highlight.js / shiki：
 * 1. 安全模型必须和 `markdown.ts` 保持一致。那两个库都产出 HTML 字符串，接上去
 *    就得开 `dangerouslySetInnerHTML`，把现在"零 innerHTML 通道"的结论作废。
 *    这里输出的是 token 数组，由 React 渲染成文本子节点，自动转义。
 * 2. 悬浮窗是第二个 webview，多一份几百 KB 的语法包意味着两份内存。
 * 3. 面试答案里的代码就是几十行的示例，不需要完整语法树级别的准确度。
 *
 * 覆盖范围是词法级：注释、字符串、数字、关键字、内置名。不做语义分析，
 * 因此 `class` 当属性名时也会被标成关键字。这个误差在阅读示例代码时无所谓。
 */

export type HighlightKind = 'plain' | 'comment' | 'string' | 'number' | 'keyword' | 'builtin'

export interface HighlightToken {
  kind: HighlightKind
  text: string
}

interface LangSpec {
  /** 行注释前缀，按长度降序匹配（`///` 要先于 `//`）。 */
  lineComments: string[]
  blockComment?: [string, string]
  /** 单行字符串定界符。 */
  strings: string[]
  /** 跨行字符串定界符（Python 三引号、JS 模板串）。 */
  multiline: string[]
  keywords: Set<string>
  builtins: Set<string>
}

const words = (source: string) => new Set(source.split(/\s+/).filter(Boolean))

const JS_KEYWORDS = words(`
  abstract as async await break case catch class const continue debugger declare default delete do
  else enum export extends finally for from function get if implements import in infer instanceof
  interface is keyof let namespace new of private protected public readonly return satisfies set
  static super switch this throw try type typeof var void while with yield
`)
const JS_BUILTINS = words(`
  Array Boolean Date Error JSON Map Math Number Object Promise RegExp Set String Symbol WeakMap
  console document false globalThis Infinity NaN null number string boolean any unknown never
  undefined true window
`)

const PY_KEYWORDS = words(`
  and as assert async await break class continue def del elif else except finally for from global
  if import in is lambda match nonlocal not or pass raise return try while with yield
`)
const PY_BUILTINS = words(`
  False None True bool bytes dict enumerate float int len list print range self set sorted str
  sum super tuple type zip
`)

const RUST_KEYWORDS = words(`
  as async await break const continue crate dyn else enum extern fn for if impl in let loop match
  mod move mut pub ref return self Self static struct super trait type unsafe use where while
`)
const RUST_BUILTINS = words(`
  bool char f32 f64 i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize str String Vec Option
  Result Some None Ok Err Box true false
`)

const GO_KEYWORDS = words(`
  break case chan const continue default defer else fallthrough for func go goto if import
  interface map package range return select struct switch type var
`)
const GO_BUILTINS = words(`
  append bool byte cap close complex copy delete error false float32 float64 int int8 int16 int32
  int64 len make map new nil panic print println recover rune string true uint uintptr
`)

const JAVA_KEYWORDS = words(`
  abstract assert break case catch class const continue default do else enum extends final finally
  for goto if implements import instanceof interface native new package private protected public
  return static strictfp super switch synchronized this throw throws transient try var volatile while
`)
const JAVA_BUILTINS = words(`
  boolean byte char double false float int long null short String System true void Integer Double
  List Map Object Optional Set String
`)

const C_KEYWORDS = words(`
  alignas alignof auto bool break case catch char class const constexpr continue default delete do
  double else enum explicit export extern float for friend goto if inline int long mutable
  namespace new noexcept nullptr operator private protected public register return short signed
  sizeof static struct switch template this throw try typedef typename union unsigned using virtual
  void volatile while
`)
const C_BUILTINS = words(`
  cout cerr cin endl false NULL nullptr printf size_t std string true uint32_t uint64_t vector
`)

const SQL_KEYWORDS = words(`
  ALTER AND AS ASC BY CASE COMMIT CREATE DELETE DESC DISTINCT DROP ELSE END EXISTS FROM FULL GROUP
  HAVING IN INDEX INNER INSERT INTO IS JOIN LEFT LIKE LIMIT NOT NULL OFFSET ON OR ORDER OUTER
  PRIMARY RIGHT ROLLBACK SELECT SET TABLE THEN UNION UNIQUE UPDATE VALUES VIEW WHEN WHERE WITH
`)
const SQL_BUILTINS = words(`
  avg boolean char coalesce count date decimal float int integer json max min now numeric sum text
  timestamp uuid varchar
`)

const SH_KEYWORDS = words(`
  case do done elif else esac fi for function if in local return then until while
`)
const SH_BUILTINS = words(`
  cat cd cp curl echo export grep jq kill ls mkdir mv npm printf pwd python rm sed set source
  sudo test touch which
`)

const JS_SPEC: LangSpec = {
  lineComments: ['//'],
  blockComment: ['/*', '*/'],
  strings: ['"', "'"],
  multiline: ['`'],
  keywords: JS_KEYWORDS,
  builtins: JS_BUILTINS
}

const SPECS: Record<string, LangSpec> = {
  javascript: JS_SPEC,
  typescript: JS_SPEC,
  json: {
    lineComments: [],
    strings: ['"'],
    multiline: [],
    keywords: words('true false null'),
    builtins: new Set()
  },
  python: {
    lineComments: ['#'],
    strings: ['"', "'"],
    multiline: ['"""', "'''"],
    keywords: PY_KEYWORDS,
    builtins: PY_BUILTINS
  },
  rust: {
    lineComments: ['///', '//!', '//'],
    blockComment: ['/*', '*/'],
    strings: ['"'],
    multiline: [],
    keywords: RUST_KEYWORDS,
    builtins: RUST_BUILTINS
  },
  go: {
    lineComments: ['//'],
    blockComment: ['/*', '*/'],
    strings: ['"', "'"],
    multiline: ['`'],
    keywords: GO_KEYWORDS,
    builtins: GO_BUILTINS
  },
  java: {
    lineComments: ['//'],
    blockComment: ['/*', '*/'],
    strings: ['"', "'"],
    multiline: [],
    keywords: JAVA_KEYWORDS,
    builtins: JAVA_BUILTINS
  },
  cpp: {
    lineComments: ['//'],
    blockComment: ['/*', '*/'],
    strings: ['"', "'"],
    multiline: [],
    keywords: C_KEYWORDS,
    builtins: C_BUILTINS
  },
  sql: {
    lineComments: ['--'],
    blockComment: ['/*', '*/'],
    strings: ["'", '"'],
    multiline: [],
    keywords: SQL_KEYWORDS,
    builtins: SQL_BUILTINS
  },
  shell: {
    lineComments: ['#'],
    strings: ['"', "'"],
    multiline: [],
    keywords: SH_KEYWORDS,
    builtins: SH_BUILTINS
  }
}

/** 语言别名。LLM 写围栏标记时用什么写法都有，别名表比要求它统一现实。 */
const ALIASES: Record<string, string> = {
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  node: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  py3: 'python',
  python3: 'python',
  rs: 'rust',
  golang: 'go',
  kt: 'java',
  kotlin: 'java',
  cs: 'cpp',
  csharp: 'cpp',
  'c++': 'cpp',
  cc: 'cpp',
  h: 'cpp',
  hpp: 'cpp',
  c: 'cpp',
  objc: 'cpp',
  swift: 'java',
  postgres: 'sql',
  postgresql: 'sql',
  mysql: 'sql',
  sqlite: 'sql',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  console: 'shell',
  shellsession: 'shell',
  yaml: 'json',
  yml: 'json',
  jsonc: 'json'
}

/**
 * 解析围栏语言标记。返回 null 表示"不认识这门语言，按纯文本渲染"。
 *
 * 不认识就不高亮，而不是硬套 C 家族规则：把 Markdown、diff、日志按 C 词法
 * 上色只会制造随机颜色噪声，比全灰更难读。
 */
export function resolveLang(lang: string): string | null {
  const key = lang.trim().toLowerCase()
  if (!key) return null
  const resolved = ALIASES[key] ?? key
  return resolved in SPECS ? resolved : null
}

const IDENT_START = /[A-Za-z_$]/
const IDENT_BODY = /[A-Za-z0-9_$]/

/** 数字：十进制、十六进制、指数、下划线分隔（Rust/JS 都允许）。 */
const NUMBER_RE = /^(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?\d+)?)/

/**
 * 单遍词法扫描。注释和字符串优先，因此 `// let x = 1` 整行都是注释，
 * `"// not a comment"` 整段都是字符串。
 *
 * 未闭合的字符串/注释一律吃到文本结尾，不抛错：流式输出时代码块经常是半截的。
 */
export function highlight(code: string, lang: string): HighlightToken[] {
  const resolved = resolveLang(lang)
  if (!resolved) return code ? [{ kind: 'plain', text: code }] : []
  const spec = SPECS[resolved]
  const tokens: HighlightToken[] = []
  let plain = ''

  const flush = () => {
    if (plain) {
      tokens.push({ kind: 'plain', text: plain })
      plain = ''
    }
  }
  const push = (kind: HighlightKind, text: string) => {
    if (!text) return
    flush()
    tokens.push({ kind, text })
  }

  let i = 0
  while (i < code.length) {
    const rest = code.slice(i)

    const lineComment = spec.lineComments.find((prefix) => rest.startsWith(prefix))
    if (lineComment) {
      const end = code.indexOf('\n', i)
      const stop = end === -1 ? code.length : end
      push('comment', code.slice(i, stop))
      i = stop
      continue
    }

    if (spec.blockComment && rest.startsWith(spec.blockComment[0])) {
      const [open, close] = spec.blockComment
      const end = code.indexOf(close, i + open.length)
      const stop = end === -1 ? code.length : end + close.length
      push('comment', code.slice(i, stop))
      i = stop
      continue
    }

    const multi = spec.multiline.find((delim) => rest.startsWith(delim))
    if (multi) {
      const end = code.indexOf(multi, i + multi.length)
      const stop = end === -1 ? code.length : end + multi.length
      push('string', code.slice(i, stop))
      i = stop
      continue
    }

    const quote = spec.strings.find((delim) => rest.startsWith(delim))
    if (quote) {
      let j = i + quote.length
      // 单行字符串遇到换行就收口：漏掉的引号不该把后面几十行全染成字符串。
      while (j < code.length && code[j] !== '\n') {
        if (code[j] === '\\') {
          j += 2
          continue
        }
        if (code.startsWith(quote, j)) {
          j += quote.length
          break
        }
        j += 1
      }
      push('string', code.slice(i, Math.min(j, code.length)))
      i = Math.min(j, code.length)
      continue
    }

    const char = code[i]
    if (/\d/.test(char)) {
      const match = NUMBER_RE.exec(rest)
      if (match) {
        push('number', match[0])
        i += match[0].length
        continue
      }
    }

    if (IDENT_START.test(char)) {
      let j = i + 1
      while (j < code.length && IDENT_BODY.test(code[j])) j += 1
      const word = code.slice(i, j)
      // SQL 关键字不分大小写，其余语言分。统一先按原样查，SQL 再查大写形式。
      const kind: HighlightKind = spec.keywords.has(word)
        ? 'keyword'
        : spec.builtins.has(word)
          ? 'builtin'
          : resolved === 'sql' && spec.keywords.has(word.toUpperCase())
            ? 'keyword'
            : resolved === 'sql' && spec.builtins.has(word.toLowerCase())
              ? 'builtin'
              : 'plain'
      if (kind === 'plain') plain += word
      else push(kind, word)
      i = j
      continue
    }

    plain += char
    i += 1
  }

  flush()
  return tokens
}
