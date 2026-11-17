import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, writeFile, readFile, access } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { build } from 'vite'
import { releaseBuildReceipt } from '../releaseBuild.ts'

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'scholarflow-build-receipt-'))
  await mkdir(join(root, 'frontend/src'), { recursive: true })
  await writeFile(join(root, 'frontend/index.html'), '<script type="module" src="/src/main.ts"></script>')
  await writeFile(join(root, 'frontend/src/main.ts'), 'document.body.textContent="fixture"')
  await writeFile(join(root, 'release.json'), '{"version":"0.1.0"}')
  return root
}
const config = (root, plugins = []) => ({ root: join(root, 'frontend'), configFile: false,
  logLevel: 'silent', plugins: [releaseBuildReceipt(root), ...plugins], build: { minify: false } })

test('successful bundle records exact frontend inputs', async () => {
  const root = await fixture()
  await build(config(root))
  const receipt = JSON.parse(await readFile(join(root, 'frontend/dist/release-build.json'), 'utf8'))
  assert.equal(receipt.format, 'scholarflow-frontend-inputs')
  assert.deepEqual(receipt.files.map(file => file.path), ['frontend/index.html', 'frontend/src/main.ts', 'release.json'])
  assert(receipt.files.every(file => /^[a-f0-9]{64}$/.test(file.sha256)))
})

test('failed bundle does not create a success receipt', async () => {
  const root = await fixture()
  await assert.rejects(build(config(root, [{ name: 'deliberate-failure', buildStart() { throw new Error('deliberate build failure') } }])))
  await assert.rejects(access(join(root, 'frontend/dist/release-build.json')))
})

test('changing input during bundle refuses a mixed receipt', async () => {
  const root = await fixture()
  await assert.rejects(build(config(root, [{ name: 'change-input', async generateBundle() {
    await writeFile(join(root, 'frontend/src/main.ts'), 'document.body.textContent="changed"')
  } }])), /Source changed during frontend build/)
  await assert.rejects(access(join(root, 'frontend/dist/release-build.json')))
})
