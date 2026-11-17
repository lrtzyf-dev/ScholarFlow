import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  {
    path: '/library',
    name: 'library',
    component: () => import('./views/LibraryView.vue'),
    meta: { title: '文献库' },
  },
  { path: '/reader', redirect: '/library' },
  {
    path: '/sources/:revisionId',
    name: 'source-reader',
    component: () => import('./views/SourceReaderView.vue'),
    meta: { title: '资料原文' },
  },
  {
    path: '/reader/:id',
    name: 'reader',
    component: () => import('./views/ReaderView.vue'),
    meta: { title: '版式阅读' },
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('./views/ChatView.vue'),
    meta: { title: '问答工作台' },
  },
  {
    path: '/graph',
    name: 'graph',
    component: () => import('./views/GraphView.vue'),
    meta: { title: '引用图谱' },
  },
  {
    path: '/compare',
    name: 'compare',
    component: () => import('./views/CompareView.vue'),
    meta: { title: '跨文献对比' },
  },
  {
    path: '/data',
    name: 'data',
    component: () => import('./views/DataView.vue'),
    meta: { title: '数据图表' },
  },
  { path: '/artifacts', name: 'artifacts', component: () => import('./views/ArtifactsView.vue'), meta: { title: '研究成果' } },
  { path: '/artifacts/:id', name: 'artifact', component: () => import('./views/ArtifactsView.vue'), meta: { title: '编辑研究成果' } },
  {
    path: '/translate',
    name: 'translate',
    component: () => import('./views/TranslateView.vue'),
    meta: { title: '中译英' },
  },
  {
    path: '/quality',
    name: 'quality',
    component: () => import('./views/QualityView.vue'),
    meta: { title: '引用含金量' },
  },
  {
    path: '/future',
    name: 'future',
    component: () => import('./views/FutureWorkView.vue'),
    meta: { title: '未来方向' },
  },
  {
    path: '/track',
    name: 'track',
    component: () => import('./views/TrackView.vue'),
    meta: { title: '追踪推荐' },
  },
  {
    path: '/framework',
    name: 'framework',
    component: () => import('./views/FrameworkView.vue'),
    meta: { title: '论文框架' },
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('./views/TasksView.vue'),
    meta: { title: '后台任务' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('./views/SettingsView.vue'),
    meta: { title: '模型与设置' },
  },
  {
    path: '/about',
    name: 'about',
    component: () => import('./views/AboutView.vue'),
    meta: { title: '关于' },
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
