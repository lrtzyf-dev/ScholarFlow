import axios from 'axios'
import type { Conversation } from './api'
const http = axios.create({ baseURL: '/api' })
export interface Project { id: string; name: string; root_path: string; available: boolean; error: string }
export interface ProjectEntry { name: string; path: string; kind: 'file' | 'directory' }
export interface FilePage { path: string; text: string; offset: number; next_offset: number | null; total_chars: number; sha256: string }
export interface DirectoryPage { path: string; entries: ProjectEntry[]; next_offset: number | null; limited: boolean }
export const listProjects = (): Promise<Project[]> => http.get('/projects').then(r => r.data)
export const listProjectConversations = (id: string, cursor?: string): Promise<{ conversations: Conversation[]; next_cursor: string | null }> => http.get(`/projects/${id}/conversations`, { params: { cursor } }).then(r => r.data)
export const addProject = (path: string) : Promise<Project> => http.post('/projects', { path }).then(r => r.data)
export const getChatProject = (id: string): Promise<{ project: Project | null; workspace_revision: number }> => http.get(`/conversations/${encodeURIComponent(id)}/project`).then(r => r.data)
export const bindChatProject = (id: string, project_id: string): Promise<{ project: Project }> => http.put(`/conversations/${encodeURIComponent(id)}/project`, { project_id }).then(r => r.data)
export const listProjectFiles = (id: string, path = '', offset = 0, q = ''): Promise<DirectoryPage> => http.get(`/projects/${id}/files`, { params: { path, offset, q } }).then(r => r.data)
export const readProjectFile = (id: string, path: string, offset = 0, sha256?: string): Promise<FilePage> => http.get(`/projects/${id}/file`, { params: { path, offset, sha256 } }).then(r => r.data)
export const initProjectRules = (id: string) => http.post(`/projects/${id}/init`).then(r => r.data)
export const getProjectRules = (id: string): Promise<{ rules: { path: string; text: string }[] }> => http.get(`/projects/${id}/rules`).then(r => r.data)
export function projectError(error: any) { return typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : error?.message || '项目操作失败' }
