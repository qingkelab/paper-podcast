/**
 * 列表展示用的派生文案（播客库的卡片视图与列表视图共用）。
 *
 * 为什么单独抽一个文件：卡片和列表行要显示同一批信息（时长、来源、
 * 论文出处、语言版本、难度），两处各写一遍必然漂移 —— 出现「卡片上是
 * arXiv:2609.38169、列表里是一条长 URL」这种不一致，改的人还会以为改全了。
 */
import type { EpisodeSummary } from '../api'
import { LEVEL_LABELS, SOURCE_LABELS } from './stages'
import { formatBytes, formatDuration } from './format'
import { languageLabel, sanitizeLanguages } from './language'

/** 时长：合成完用真实时长，没合成完就退回「申请的是几分钟」 */
export function durationText(episode: EpisodeSummary): string {
  if (episode.audio_duration_sec) return formatDuration(episode.audio_duration_sec)
  return `${episode.options.duration_min} 分钟`
}

/**
 * 这一集「是从哪来的」，一句话说完，**不暴露服务器路径**。
 *
 * 踩过的坑：PDF 上传时后端把 `source_ref` 写成落盘后的绝对路径
 * （`.../data/uploads/{id}.pdf`），列表里原样铺出来就是一条
 * `/Users/someone/...` 的服务器路径 —— 对用户毫无信息量，还平白
 * 泄漏目录结构。所以这里只认得出「人看得懂的东西」，认不出就不显示。
 */
export function sourceRefText(episode: EpisodeSummary): string | null {
  const ref = (episode.source_ref ?? '').trim()
  if (episode.source_type === 'text') return '粘贴的文本'
  if (!ref) return null

  if (episode.source_type === 'pdf') {
    // 后端存的是磁盘路径，只在末尾带一点可读信息时取文件名（不含目录）
    const base = ref.split(/[\\/]/).pop() ?? ''
    const stem = base.replace(/\.pdf$/i, '').trim()
    // 文件名等于 episode id（后端就是这么命名的）时它对人没有任何意义
    if (!stem || stem.toLowerCase() === episode.id.toLowerCase()) return '本地 PDF'
    return stem
  }

  // 链接：优先给 arXiv 号，比一整条 URL 好认也好念
  const arxiv = ref.match(/arxiv\.org\/(?:abs|pdf)\/([0-9]{4}\.[0-9]{4,5})(v\d+)?/i)
  if (arxiv) return `arXiv:${arxiv[1]}${arxiv[2] ?? ''}`
  try {
    return new URL(ref).hostname.replace(/^www\./, '')
  } catch {
    return null
  }
}

/** 「作者 · 年份 · 会议/期刊」，能拼多少算多少；全都取不到就返回 null */
export function paperLine(episode: EpisodeSummary): string | null {
  const meta = episode.paper_meta
  const parts: string[] = []
  const authors = (meta?.authors ?? []).filter((name) => Boolean(name && name.trim()))
  if (authors.length === 1) parts.push(authors[0]!.trim())
  else if (authors.length > 1) parts.push(`${authors[0]!.trim()} 等`)

  const venue = meta?.venue?.trim()
  if (venue) parts.push(venue.length > 24 ? `${venue.slice(0, 24)}…` : venue)
  // 会议名里常自带年份（「NeurIPS 2017」），再补一个 year 就成了「NeurIPS 2017 · 2017」
  if (meta?.year && !(venue ?? '').includes(String(meta.year))) parts.push(String(meta.year))

  const ref = sourceRefText(episode)
  if (ref) parts.push(ref)
  return parts.length ? parts.join(' · ') : null
}

/**
 * 关键词，最多几个（列表上只当「这一集讲什么」的提示）。
 *
 * 每个词也做了截断：论文关键词里有「Delta 规则线性注意力」这种长短语，
 * 原样铺出来会把芯片挤到第二行，卡片凭空高 35px（实测），
 * 而省掉的那几个字在详情页看得全。
 */
export function keywordsOf(episode: EpisodeSummary, max = 2, maxChars = 18): string[] {
  const list = episode.paper_meta?.keywords ?? []
  return list
    .map((word) => {
      const clean = word.trim().replace(/\s+/g, ' ')
      return clean.length > maxChars ? `${clean.slice(0, maxChars)}…` : clean
    })
    .filter(Boolean)
    .slice(0, max)
}

/** 这一集产出了哪些语言版本：`['中文', 'English']`；老数据缺字段时按主语言兜底 */
export function languageLabels(episode: EpisodeSummary): string[] {
  const declared = sanitizeLanguages(episode.languages)
  const fallback = declared.length ? declared : sanitizeLanguages([episode.language])
  return fallback.map((code) => languageLabel(code))
}

export function levelText(episode: EpisodeSummary): string {
  return LEVEL_LABELS[episode.options.level] ?? episode.options.level
}

/**
 * 卡片/列表行共用的一行元信息：来源 · 难度 · 时长 · 体积。
 * 体积只在有音频时出现（进行中/失败时显示「—」是纯噪音）。
 */
export function metaParts(episode: EpisodeSummary): string[] {
  const parts = [SOURCE_LABELS[episode.source_type] ?? episode.source_type, levelText(episode)]
  parts.push(durationText(episode))
  if (episode.audio_bytes) parts.push(formatBytes(episode.audio_bytes))
  return parts
}

/** 已经对外分享（契约 §2.7）：列表上给个角标，免得忘了哪一期是公开的 */
export function isShared(episode: EpisodeSummary): boolean {
  return (episode.visibility ?? 'private') === 'public' && Boolean(episode.share_token)
}

/** 还在跑（非终态） */
export function isRunning(episode: EpisodeSummary): boolean {
  return episode.status !== 'completed' && episode.status !== 'failed'
}
