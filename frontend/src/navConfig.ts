import type { Component } from 'vue'
import {
  MessageSquareText,
  LibraryBig,
  BookOpenText,
  Network,
  GitCompare,
  Share2,
  BadgeCheck,
  Lightbulb,
  PenLine,
  ChartColumn,
  Languages,
  Settings2,
  ListTodo,
  Info,
} from 'lucide-vue-next'

export interface NavView {
  /** 路由 name，与 router.ts 保持一致 */
  name: string
  label: string
  icon: Component
  /** 点击跳转路径；reader 为特殊项，由外壳单独处理 */
  to?: string
  /** 命中激活的路径前缀 */
  match: string[]
  /** 特殊标记：reader 无固定路由 */
  special?: 'reader'
}

export interface NavZone {
  id: string
  label: string
  icon: Component
  views: NavView[]
}

/** 双层导航配置：一级 5 分区，二级各视图。新增功能只在此处追加，一级永不增长。 */
export const zones: NavZone[] = [
  {
    id: 'chat',
    label: '问答',
    icon: MessageSquareText,
    views: [
      { name: 'chat', label: '问答工作台', icon: MessageSquareText, to: '/chat', match: ['/chat'] },
    ],
  },
  {
    id: 'library',
    label: '文献',
    icon: LibraryBig,
    views: [
      { name: 'library', label: '文献库', icon: LibraryBig, to: '/library', match: ['/library'] },
      {
        name: 'reader',
        label: '版式阅读',
        icon: BookOpenText,
        special: 'reader',
        match: ['/reader'],
      },
      { name: 'track', label: '追踪推荐', icon: Lightbulb, to: '/track', match: ['/track'] },
    ],
  },
  {
    id: 'analysis',
    label: '分析',
    icon: Network,
    views: [
      {
        name: 'compare',
        label: '跨文献对比',
        icon: GitCompare,
        to: '/compare',
        match: ['/compare'],
      },
      { name: 'graph', label: '引用图谱', icon: Share2, to: '/graph', match: ['/graph'] },
      {
        name: 'quality',
        label: '引用含金量',
        icon: BadgeCheck,
        to: '/quality',
        match: ['/quality'],
      },
      { name: 'future', label: '未来方向', icon: Lightbulb, to: '/future', match: ['/future'] },
    ],
  },
  {
    id: 'writing',
    label: '写作',
    icon: PenLine,
    views: [
      {
        name: 'framework',
        label: '论文框架',
        icon: PenLine,
        to: '/framework',
        match: ['/framework'],
      },
      { name: 'data', label: '数据图表', icon: ChartColumn, to: '/data', match: ['/data'] },
      { name: 'artifacts', label: '研究成果', icon: PenLine, to: '/artifacts', match: ['/artifacts'] },
      {
        name: 'translate',
        label: '中译英',
        icon: Languages,
        to: '/translate',
        match: ['/translate'],
      },
    ],
  },
  {
    id: 'system',
    label: '系统',
    icon: Settings2,
    views: [
      { name: 'tasks', label: '运行与待办', icon: ListTodo, to: '/tasks', match: ['/tasks'] },
      {
        name: 'settings',
        label: '模型与设置',
        icon: Settings2,
        to: '/settings',
        match: ['/settings'],
      },
      { name: 'about', label: '关于', icon: Info, to: '/about', match: ['/about'] },
    ],
  },
]

/** 根据当前路径找到激活视图 */
export function findActiveView(path: string): NavView | undefined {
  for (const zone of zones) {
    for (const view of zone.views) {
      if (view.match.some((prefix) => path.startsWith(prefix))) return view
    }
  }
  return undefined
}

/** 根据当前路径找到激活分区 */
export function findActiveZone(path: string): NavZone {
  const view = findActiveView(path)
  return zones.find((zone) => zone.views.some((v) => v.name === view?.name)) || zones[0]
}

/** 扁平化全部视图，供命令面板检索 */
export const allViews: { zone: NavZone; view: NavView }[] = zones.flatMap((zone) =>
  zone.views.map((view) => ({ zone, view }))
)
