import axios from 'axios'
const http = axios.create({ baseURL: '/api', timeout: 20000 })
export interface SearchOptions {
  query: string; path: string; regex: boolean; case_sensitive: boolean; whole_word: boolean
  include: string; exclude: string; hidden: boolean; ignored: boolean
}
export interface SearchHit {
  path: string; line: number; column: number; offset: number; length: number; sha256: string
  parts: { text: string; match: boolean }[]; snippet_limited: boolean
}
export interface SearchPage {
  matches: SearchHit[]; total: number; files: number; snapshot: string; next_offset: number | null
  limited: boolean; warnings: string[]
}
export const emptySearchOptions = (): SearchOptions => ({ query: '', path: '', regex: false, case_sensitive: false, whole_word: false, include: '', exclude: '', hidden: false, ignored: false })
export const searchProject = (project: string, options: SearchOptions, offset = 0, snapshot?: string, signal?: AbortSignal): Promise<SearchPage> =>
  http.post(`/projects/${project}/search`, { ...options, offset, snapshot }, { signal }).then(r => r.data)
