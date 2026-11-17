import { createHash } from 'node:crypto'
import { existsSync, lstatSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import type { Plugin } from 'vite'

function inputs(root: string) {
  const paths: string[] = []
  function walk(path: string) {
    const stat = lstatSync(path)
    if (stat.isSymbolicLink()) throw new Error('Release inputs cannot be symbolic links')
    if (stat.isDirectory()) {
      for (const name of readdirSync(path).sort()) {
        if (!name.startsWith('.') && !['__pycache__', 'node_modules', 'data'].includes(name)) walk(join(path, name))
      }
    } else if (stat.isFile()) paths.push(path)
  }
  for (const name of ['frontend/src', 'frontend/public']) {
    const path = join(root, name)
    if (existsSync(path)) walk(path)
  }
  for (const name of ['release.json', 'frontend/package.json', 'frontend/package-lock.json', 'frontend/vite.config.ts',
    'frontend/releaseBuild.ts', 'frontend/tsconfig.json', 'frontend/tsconfig.node.json', 'frontend/index.html']) {
    const path = join(root, name)
    if (existsSync(path)) walk(path)
  }
  return paths.map(path => {
    const bytes = readFileSync(path)
    return { path: relative(root, path).replaceAll('\\', '/'), size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }
  }).sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
}

export function releaseBuildReceipt(root: string): Plugin {
  let before: ReturnType<typeof inputs> = [], outDir = ''
  return {
    name: 'scholarflow-release-receipt',
    apply: 'build',
    configResolved(config) { outDir = resolve(config.root, config.build.outDir) },
    buildStart() { before = inputs(root) },
    writeBundle() {
      if (JSON.stringify(before) !== JSON.stringify(inputs(root))) throw new Error('Source changed during frontend build; rebuild required')
      writeFileSync(join(outDir, 'release-build.json'), JSON.stringify({ format: 'scholarflow-frontend-inputs', version: 1, files: before }))
    },
  }
}
