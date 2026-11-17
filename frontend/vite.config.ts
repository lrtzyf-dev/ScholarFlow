import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { releaseBuildReceipt } from './releaseBuild'

const release = JSON.parse(readFileSync(fileURLToPath(new URL('../release.json', import.meta.url)), 'utf8'))
if (release.schema_version !== 1 || release.product_id !== 'scholarflow') throw new Error('Unsupported release definition')

// 后端默认 8765（8000 在部分 Windows 上被保留端口策略拦截）
export default defineConfig({
  define: { __SCHOLARFLOW_RELEASE__: JSON.stringify({ version: release.version, channel: release.channel, build_number: release.build_number }) },
  plugins: [vue(), releaseBuildReceipt(fileURLToPath(new URL('..', import.meta.url)))],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 仅对纯叶子型大依赖做独立分包：按需加载、独立缓存。
        // 不分组 element-plus 的 barrel 入口（会破坏 tree-shaking），
        // 让 rollup 自动处理共享 helper，避免重型库被误提到首屏。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          const rest = id
            .split('node_modules')
            .pop()!
            .replace(/^[\\/]/, '')
          const segs = rest.split(/[\\/]/)
          const pkg = segs[0].startsWith('@') ? `${segs[0]}/${segs[1] ?? ''}` : segs[0]
          if (pkg === 'echarts' || pkg === 'zrender') return 'vendor-echarts'
          if (pkg === 'cytoscape' || pkg === 'cytoscape-cose-bilkent' || pkg === 'cytoscape-fcose')
            return 'vendor-cytoscape'
          if (pkg === 'dayjs') return 'vendor-dayjs'
          if (pkg === 'pdfjs-dist') return 'vendor-pdfjs'
          if (pkg.startsWith('@xterm')) return 'vendor-xterm'
          if (pkg === 'katex') return 'vendor-katex'
          if (pkg === 'markdown-it' || pkg === 'highlight.js' || pkg.startsWith('@mdit'))
            return 'vendor-markdown'
          return undefined
        },
      },
    },
  },
})
