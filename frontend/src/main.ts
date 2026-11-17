import { createApp } from 'vue'
// 仅保留全量样式（组件在各文件按需 import，JS 可 tree-shaking，避免全量注册撑大首屏）
import 'element-plus/dist/index.css'
import App from './App.vue'
import { ElMessage } from 'element-plus'
import './theme/fonts.css'
import './theme/tokens.css'
import './styles.css'
import { initializeDesktopState } from './desktopState'
import { applyReadingTextScale } from './readingZoom'
import { initializeLocalSession } from './localSession'

applyReadingTextScale()
void initializeLocalSession(message => { ElMessage.error(message) }).then(() => initializeDesktopState().catch(() => undefined)).then(async () => {
  const { default: router } = await import('./router')
  createApp(App).use(router).mount('#app')
}).catch(() => {
  const root = document.querySelector('#app')
  if (!root) return
  const heading = document.createElement('h1')
  heading.textContent = '研序暂时无法连接本次窗口'
  const message = document.createElement('p')
  message.textContent = '请重新连接；若仍失败，请关闭并重新启动研序。原有资料保存在本地。'
  const retry = document.createElement('button')
  retry.textContent = '重新连接'
  retry.onclick = () => window.location.reload()
  root.replaceChildren(heading, message, retry)
})
