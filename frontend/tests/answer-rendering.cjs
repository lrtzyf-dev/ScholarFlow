// Desktop rendering and actions with controlled answer/clipboard/stream fixtures.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const stateMock = require('./desktop-state-mock.cjs')()
const base = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8767'
const output = path.resolve(__dirname, '../../verification')
const code = 'def score(correct, total):\n    return correct / total  # 实验准确率\n'
const rich = '## 实验结果\n\n准确率 $a = \\frac{n}{N}$，比较两组实验结果 [^e1]。\n\n'
  + '| 方法 | 准确率 | 备注 |\n| --- | --- | --- |\n| **基线** | 0.82 | 含逗号,与"引号" |\n| 改进方法 | 0.91 | [来源](https://example.com) |\n| =1+1 | -1.5 | $x^2$ |\n\n'
  + '```python\n' + code + '```\n\n'
  + '$$\n\\Delta = a_2 - a_1 = 0.09\n$$\n\n'
  + '> ```unknown\n> <img src=x onerror=alert(1)>\n> ```\n\n'
  + '<script>window.injected = true</script>'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, acceptDownloads: true })
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget { close() {} }
    window.copiedText = []
    window.clipboardMode = 'modern'
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async text => {
        if (window.clipboardMode === 'pending') await new Promise(resolve => { window.releaseClipboard = resolve })
        else if (window.clipboardMode !== 'modern') throw Error('Clipboard denied')
        window.copiedText.push(text)
      },
    } })
    document.execCommand = command => {
      if (command !== 'copy' || window.clipboardMode === 'denied') return false
      window.copiedText.push(document.activeElement.value)
      return true
    }
    window.answerStreams = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, options) => {
      if (String(input) !== '/api/chat/stream') return originalFetch(input, options)
      const stream = new ReadableStream({ start(controller) {
        window.answerStreams.push(controller)
        options.signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')), { once: true })
      } })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
    window.feedAnswer = (events, close = false) => {
      const controller = window.answerStreams.at(-1)
      for (const event of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      if (close) controller.close()
    }
  })
  const conversation = { id: 'c_render', title: '实验结果', message_count: 1 }
  await page.route('**/api/**', async route => {
    if (await stateMock(route)) return
    const request = route.request(), url = new URL(request.url())
    const json = data => route.fulfill({ json: data })
    if (url.pathname === '/api/conversations/c_render') return json({ ...conversation, messages: [{
      id: 'qa_render', question: '比较实验结果', answer: rich, evidence: [{ id: 'e1', paper_id: 'p1', paper_title: '实验数据', page: 1, quote: '原始实验记录' }],
      skills: [], refusal: 0, latency_ms: 1,
    }] })
    if (url.pathname === '/api/conversations/page') return json({ conversations: [conversation], total: 1, pinned_total: 0, next_cursor: null })
    if (url.pathname === '/api/conversations/c_render/activity') return json(conversation)
    if (url.pathname === '/api/conversations/c_render/project') return json({ project: null })
    if (url.pathname === '/api/foreground-runs') return json(request.method() === 'GET' ? { runs: [] } : { id: request.postDataJSON().id, revision: 1, status: 'running' })
    if (url.pathname.startsWith('/api/foreground-runs/')) return json({ revision: 2, status: 'running' })
    if (url.pathname === '/api/tasks') return json({ tasks: [] })
    if (url.pathname === '/api/skills') return json({ skills: [] })
    if (url.pathname === '/api/mcp/servers') return json({ servers: [] })
    if (['/api/papers', '/api/projects'].includes(url.pathname)) return json([])
    if (url.pathname === '/api/settings') return json({ approval_mode: 'auto', reasoning_effort: 'medium', memory_recall: true })
    if (url.pathname.startsWith('/api/goal')) return json({ current: null, goals: [] })
    return json({})
  })
  async function until(fn, label) {
    const end = Date.now() + 10000
    while (!await fn()) { assert.ok(Date.now() < end, label); await page.waitForTimeout(40) }
  }
  const answer = page.locator('.markdown-answer').first()
  const copies = answer.getByRole('button', { name: '复制代码', exact: true })
  const composer = page.locator('textarea').first()
  try {
    await page.goto(base + '/chat?conv=c_render')
    await answer.locator('.hljs-keyword').first().waitFor()
    await until(async () => await copies.count() === 2, 'code controls mounted')
    assert.equal(await answer.locator('.katex').count(), 3)
    assert.equal(await answer.locator('script,img').count(), 0)
    assert.equal(await page.evaluate(() => window.injected), undefined)
    assert.equal(await answer.locator('.markdown-citation').count(), 1)
    await composer.fill('保留未发送的草稿')
    await copies.first().click()
    await until(async () => await copies.first().getAttribute('title') === '已复制', 'copy success')
    assert.equal(await page.evaluate(() => window.copiedText.at(-1)), code)
    await answer.getByRole('button', { name: '复制表格', exact: true }).click()
    assert.equal(await page.evaluate(() => window.copiedText.at(-1)), '方法\t准确率\t备注\r\n基线\t0.82\t"含逗号,与""引号"""\r\n改进方法\t0.91\t来源\r\n=1+1\t-1.5\tx^2')
    const downloadEvent = page.waitForEvent('download')
    await answer.getByRole('button', { name: '导出 CSV', exact: true }).click()
    const download = await downloadEvent, chunks = []
    for await (const chunk of await download.createReadStream()) chunks.push(chunk)
    const csv = Buffer.concat(chunks).toString('utf8')
    assert.equal(download.suggestedFilename(), '表格-1.csv')
    assert.ok(csv.startsWith('\ufeff方法,准确率,备注\r\n'))
    assert.match(csv, /'=1\+1,-1\.5,x\^2/)
    assert.match(csv, /"含逗号,与""引号"""/)
    assert.doesNotMatch(csv, /<span|https:/)

    await page.evaluate(() => { window.clipboardMode = 'legacy' })
    await composer.focus()
    await composer.evaluate(el => el.setSelectionRange(2, 5))
    await copies.last().evaluate(el => el.click())
    await until(async () => await copies.last().getAttribute('title') === '已复制', 'legacy copy')
    assert.equal(await page.evaluate(() => window.copiedText.at(-1)), '<img src=x onerror=alert(1)>\n')
    assert.equal(await composer.evaluate(el => document.activeElement === el), true)
    assert.deepEqual(await composer.evaluate(el => [el.selectionStart, el.selectionEnd]), [2, 5])
    assert.equal(await composer.inputValue(), '保留未发送的草稿')
    await page.evaluate(() => { window.clipboardMode = 'denied' })
    await copies.last().click()
    await page.getByText('复制失败，请重试或手动选择内容复制', { exact: true }).waitFor()
    assert.equal(await copies.last().getAttribute('title'), '复制代码')
    await page.evaluate(() => { window.clipboardMode = 'modern' })
    await copies.last().focus()
    await page.keyboard.press('Enter')
    await until(async () => await copies.last().getAttribute('title') === '已复制', 'keyboard retry')
    await page.getByText('复制失败，请重试或手动选择内容复制', { exact: true }).waitFor({ state: 'hidden' })

    for (const [width, height, name] of [[1440, 900, 'desktop'], [1100, 700, 'small-desktop']]) {
      await page.setViewportSize({ width, height })
      await page.locator('.chat-scroll').evaluate(el => { el.scrollTop = 0 })
      await page.evaluate(() => document.fonts.ready)
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'desktop width fits')
      assert.ok(await page.evaluate(() => document.fonts.check('16px KaTeX_Main')), 'local math fonts loaded')
      for (const button of await answer.locator('.answer-block-action').all()) {
        const bounds = await button.boundingBox()
        assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width, 'action fits answer')
      }
      await page.screenshot({ path: path.join(output, `answer-tools-${name}.png`), animations: 'disabled' })
    }

    await composer.fill('流式回答验证')
    await composer.press('Enter')
    await until(async () => await page.evaluate(() => window.answerStreams.length) === 1, 'stream started')
    const live = page.locator('.markdown-answer').last()
    await page.evaluate(() => window.feedAnswer([{ kind: 'answer_delta', text: '```python\nprint("old")\n' }]))
    await live.getByRole('button', { name: '复制代码', exact: true }).waitFor()
    await page.evaluate(() => { window.clipboardMode = 'pending' })
    await live.getByRole('button', { name: '复制代码', exact: true }).click()
    await until(async () => await page.evaluate(() => !!window.releaseClipboard), 'clipboard held')
    assert.equal(await live.getByRole('button', { name: '复制代码', exact: true }).isDisabled(), true)
    await page.evaluate(() => window.feedAnswer([{ kind: 'answer_delta', text: 'print("new")\n```\n\n| A |\n| --- |\n| B |\n' }]))
    await live.getByRole('button', { name: '复制表格', exact: true }).waitFor()
    await page.evaluate(() => { window.releaseClipboard(); window.clipboardMode = 'modern' })
    await until(async () => !await live.getByRole('button', { name: '复制代码', exact: true }).isDisabled(), 'clipboard released')
    assert.equal(await live.getByRole('button', { name: '复制代码', exact: true }).getAttribute('title'), '复制代码', 'old copy does not mark newer content copied')
    await live.getByRole('button', { name: '复制代码', exact: true }).click()
    assert.equal(await page.evaluate(() => window.copiedText.at(-1)), 'print("old")\nprint("new")\n')
    await page.evaluate(() => window.feedAnswer([{ kind: 'answer', answer: '```json\n{"final":true}\n```\n\n$' + 'a+'.repeat(250) + 'b$', evidence: [], qa_id: 'qa_final' }, { kind: 'done' }], true))
    await until(async () => await live.locator('code').textContent() === '{"final":true}\n', 'final answer replaces streamed blocks')
    await live.getByRole('button', { name: '复制代码', exact: true }).click()
    assert.equal(await page.evaluate(() => window.copiedText.at(-1)), '{"final":true}\n')
    for (const [width, height] of [[1440, 900], [1100, 700]]) {
      await page.setViewportSize({ width, height })
      assert.ok(await live.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'long inline formula stays within answer')
    }
    await page.getByRole('button', { name: '新建会话', exact: true }).click()
    await until(async () => await page.locator('.answer-block-action').count() === 0, 'controls clean up after conversation switch')
    assert.deepEqual(errors, [])
    console.log('PASS: highlighted/nested code, dollar/bracket math/fonts, raw copying, TSV/CSV, clipboard fallback/failure/retry/focus, keyboard, stream replacement/stale-copy guard, cleanup, 1440x900 and 1100x700')
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'answer-tools-failure.png') })
    throw error
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
