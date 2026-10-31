import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  { path: '/library', name: 'library', component: () => import('./views/LibraryView.vue'), meta: { title: '文献库' } },
  { path: '/reader', redirect: '/library' },
  { path: '/reader/:id', name: 'reader', component: () => import('./views/ReaderView.vue'), meta: { title: '版式阅读' } },
  { path: '/chat', name: 'chat', component: () => import('./views/ChatView.vue'), meta: { title: '问答工作台' } },
  { path: '/graph', name: 'graph', component: () => import('./views/GraphView.vue'), meta: { title: '引用图谱' } },
  { path: '/tasks', name: 'tasks', component: () => import('./views/TasksView.vue'), meta: { title: '后台任务' } },
  { path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue'), meta: { title: '模型与设置' } },
  { path: '/about', name: 'about', component: () => import('./views/AboutView.vue'), meta: { title: '关于' } },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
