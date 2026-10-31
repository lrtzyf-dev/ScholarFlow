import MarkdownIt, { type Env } from 'markdown-it'
import { katex } from '@mdit/plugin-katex'
import hljs from 'highlight.js/lib/core'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import powershell from 'highlight.js/lib/languages/powershell'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import sql from 'highlight.js/lib/languages/sql'
import yaml from 'highlight.js/lib/languages/yaml'
import r from 'highlight.js/lib/languages/r'

for (const [name, language] of Object.entries({ python, javascript, typescript, json, bash, powershell, xml, css, sql, yaml, r })) {
  hljs.registerLanguage(name, language)
}

export interface RenderedAnswer {
  html: string
  codeBlocks: string[]
  tables: string[][][]
}
interface AnswerEnvironment extends Env {
  evidenceIds: Set<string>
  codeBlocks: string[]
  tables: string[][][]
  tableCursor: number
}

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
markdown.use(katex, { delimiters: 'all' as const, trust: false, maxExpand: 200, maxSize: 20, throwOnError: false })

// Replace text tokens only, preserving code and link-label semantics.
markdown.core.ruler.after('linkify', 'evidence_ref', (state) => {
  for (const block of state.tokens) {
    if (!block.children) continue
    const children = []
    let linkDepth = 0
    for (const token of block.children) {
      if (token.type === 'link_open') linkDepth += 1
      if (token.type === 'link_close') linkDepth -= 1
      if (token.type !== 'text' || linkDepth) {
        children.push(token)
        continue
      }
      let offset = 0
      for (const match of token.content.matchAll(/\[\^e(\d+)\]/g)) {
        if (match.index! > offset) {
          const text = new state.Token('text', '', 0)
          text.content = token.content.slice(offset, match.index)
          children.push(text)
        }
        const reference = new state.Token('evidence_ref', '', 0)
        reference.content = `e${match[1]}`
        children.push(reference)
        offset = match.index! + match[0].length
      }
      if (offset < token.content.length) {
        const text = new state.Token('text', '', 0)
        text.content = token.content.slice(offset)
        children.push(text)
      }
    }
    block.children = children
  }
})
markdown.renderer.rules.evidence_ref = (tokens, index, _options, env) => {
  const id = tokens[index].content
  if (!(env?.evidenceIds as Set<string> | undefined)?.has(id)) return `[^${id}]`
  return `<button type="button" class="markdown-citation" data-evidence-id="${id}" title="查看证据 ${id}" aria-label="查看证据 ${id}">${id}</button>`
}
markdown.renderer.rules.link_open = (tokens, index, options, _env, renderer) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return renderer.renderToken(tokens, index, options)
}

markdown.core.ruler.after('evidence_ref', 'answer_tables', (state) => {
  const env = state.env as AnswerEnvironment
  let table: string[][] | null = null
  let row: string[] = []
  let cell = ''
  for (const token of state.tokens) {
    if (token.type === 'table_open') table = []
    else if (token.type === 'tr_open') row = []
    else if (token.type === 'th_open' || token.type === 'td_open') cell = ''
    else if (table && token.type === 'inline') {
      cell += (token.children || []).map((child) => {
        if (child.type === 'softbreak' || child.type === 'hardbreak') return '\n'
        if (child.type === 'evidence_ref') return `[^${child.content}]`
        if (['text', 'code_inline', 'image', 'math_inline'].includes(child.type)) return child.content
        return ''
      }).join('')
    } else if (token.type === 'th_close' || token.type === 'td_close') row.push(cell)
    else if (table && token.type === 'tr_close') table.push(row)
    else if (table && token.type === 'table_close') {
      env.tables.push(table)
      table = null
    }
  }
})

markdown.renderer.rules.table_open = (_tokens, _index, _options, environment) => {
  const env = environment as AnswerEnvironment
  const index = env.tableCursor++
  return `<div class="markdown-table-block"><div class="markdown-block-toolbar"><span>表格</span><span data-table-actions="${index}"></span></div><div class="markdown-table"><table>\n`
}
markdown.renderer.rules.table_close = () => '</table></div></div>\n'
markdown.renderer.rules.fence = markdown.renderer.rules.code_block = (tokens, index, _options, environment) => {
  const env = environment as AnswerEnvironment
  const token = tokens[index]
  const language = (token.info || '').trim().split(/\s+/)[0].slice(0, 80)
  let code = markdown.utils.escapeHtml(token.content)
  // Bound highlighting work and keep unrecognised languages as literal text.
  if (token.content.length <= 100_000 && /^[a-z\d_+#.-]{1,40}$/i.test(language) && hljs.getLanguage(language)) {
    try { code = hljs.highlight(token.content, { language, ignoreIllegals: true }).value } catch { /* Escaped text remains available. */ }
  }
  const id = env.codeBlocks.push(token.content) - 1
  return `<div class="markdown-code"><div class="markdown-block-toolbar"><span>${markdown.utils.escapeHtml(language || 'text')}</span><span data-code-actions="${id}"></span></div><pre><code class="hljs">${code}</code></pre></div>\n`
}

export function renderAnswer(text: string, evidenceIds: string[] = []): RenderedAnswer {
  const env: AnswerEnvironment = { evidenceIds: new Set(evidenceIds), codeBlocks: [], tables: [], tableCursor: 0 }
  const html = markdown.render(text, env)
  return { html, codeBlocks: env.codeBlocks, tables: env.tables }
}

export function renderMarkdown(text: string, evidenceIds: string[] = []) {
  return renderAnswer(text, evidenceIds).html
}
