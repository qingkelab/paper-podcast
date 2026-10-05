import type { EpisodeStatus, LevelValue, SourceType } from '../api/types'

/** 阶段时间线（对齐契约 §1 状态机） */
export interface StageDef {
  status: EpisodeStatus
  label: string
  progress: number
}

export const STAGES: StageDef[] = [
  { status: 'queued', label: '排队中', progress: 0 },
  { status: 'parsing', label: '正在解析论文', progress: 10 },
  { status: 'analyzing', label: '正在深度解读', progress: 35 },
  { status: 'scripting', label: '正在生成播客脚本', progress: 55 },
  { status: 'synthesizing', label: '正在合成播客音频', progress: 75 },
  { status: 'completed', label: '已完成', progress: 100 },
]

export function stageIndexOf(status: EpisodeStatus): number {
  const index = STAGES.findIndex((stage) => stage.status === status)
  return index < 0 ? 0 : index
}

export const STATUS_LABELS: Record<EpisodeStatus, string> = {
  queued: '排队中',
  parsing: '解析论文',
  analyzing: '深度解读',
  scripting: '生成脚本',
  synthesizing: '合成音频',
  completed: '已完成',
  failed: '失败',
}

export const SOURCE_LABELS: Record<SourceType, string> = {
  pdf: 'PDF 上传',
  url: '链接导入',
  text: '文本粘贴',
}

export const LEVEL_LABELS: Record<LevelValue, string> = {
  intro: '入门',
  advanced: '进阶',
  expert: '专业',
}

/** 进度页/列表页可筛选的状态（按流程顺序） */
export const FILTERABLE_STATUSES: EpisodeStatus[] = [
  'queued',
  'parsing',
  'analyzing',
  'scripting',
  'synthesizing',
  'completed',
  'failed',
]
