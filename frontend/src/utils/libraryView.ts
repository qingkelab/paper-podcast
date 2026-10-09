/**
 * 播客库的展示偏好（视图模式 / 排序）。
 *
 * 为什么存 localStorage：这是「我看东西的习惯」，不是数据本身。
 * 存后端要多一张表 + 一个接口，而它换台设备重新选一次也没什么损失；
 * 不存的话每次进页面都要重新点一遍「列表视图」，很快就不想用了。
 */
import type { EpisodeSort } from '../api/types'

export type LibraryViewMode = 'grid' | 'list'

const VIEW_KEY = 'paper-podcast:library-view:v1'
const SORT_KEY = 'paper-podcast:library-sort:v1'

export const LIBRARY_VIEWS: ReadonlyArray<{ value: LibraryViewMode; label: string }> = [
  { value: 'grid', label: '卡片' },
  { value: 'list', label: '列表' },
]

/** 排序选项的文案：要说清「按什么排」，而不是只给「时间倒序」这种要猜的 */
export const SORT_OPTIONS: ReadonlyArray<{ value: EpisodeSort; label: string }> = [
  { value: 'created_desc', label: '最新生成' },
  { value: 'updated_desc', label: '最近更新' },
  { value: 'created_asc', label: '最早生成' },
  { value: 'title_asc', label: '标题 A→Z' },
  { value: 'duration_desc', label: '时长最长' },
]

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    // 隐私模式下 localStorage 会直接抛异常。读不到就当没设置过 ——
    // 偏好记不住是小事，整个播客库打不开才是大事。
    return null
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* 同上：写不进去就算了 */
  }
}

export function readLibraryView(): LibraryViewMode {
  return read(VIEW_KEY) === 'list' ? 'list' : 'grid'
}

export function saveLibraryView(view: LibraryViewMode): void {
  write(VIEW_KEY, view)
}

export function readLibrarySort(): EpisodeSort {
  const raw = read(SORT_KEY)
  const known = SORT_OPTIONS.some((option) => option.value === raw)
  // 认不出的值（比如回退到旧版本、或手改过 localStorage）一律回到默认排序，
  // 绝不把任意字符串当 sort 发给后端 —— 那会是一个 400。
  return known ? (raw as EpisodeSort) : 'created_desc'
}

export function saveLibrarySort(sort: EpisodeSort): void {
  write(SORT_KEY, sort)
}
