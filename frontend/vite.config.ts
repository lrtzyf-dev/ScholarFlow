import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端默认 8765（8000 在部分 Windows 上被保留端口策略拦截）
export default defineConfig({
  plugins: [vue()],
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
})
