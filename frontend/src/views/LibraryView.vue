<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  deleteEpisode,
  errorMessage,
  listAlbums,
  listEpisodes,
  listPublicEpisodes,
  retryEpisode,
} from '../api'
import type { Album, EpisodeSummary, PublicEpisodeItem } from '../api'
import AccountPrompt from '../components/AccountPrompt.vue'
import AlbumPickerDialog from '../components/AlbumPickerDialog.vue'
import EpisodeCard from '../components/EpisodeCard.vue'
import EpisodeRow from '../components/EpisodeRow.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { useSessionStore } from '../stores/session'
import {
  LIBRARY_VIEWS,
  SORT_OPTIONS,
  readLibrarySort,
  readLibraryView,
  saveLibrarySort,
  saveLibraryView,
  type LibraryViewMode,
} from '../utils/libraryView'

const PAGE_SIZE = 12
const AUTO_REFRESH_MS = 5000

/**
 * 状态筛选用**胶囊**而不是下拉框。
 *
 * 下拉框里排着 7 个状态，但用户实际只会问三个问题：还在跑的是哪些、
 * 哪些已经好了、哪些挂了。「生成中」是后端支持的伪状态（契约 §2.5），
 * 一次请求就能拿到，不必点五次。
 */
type StatusFilter = 'all' | 'running' | 'completed' | 'failed'

const STATUS_FILTERS: ReadonlyArray<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'running', label: '生成中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
]

const session = useSessionStore()

/**
 * **访客模式**：没登录也能看播客库（契约 §2.8）。
 *
 * 为什么加这条：这个站点本质上是展示用的 —— 想让人看见，就得先让人看见。
 * 原来「先登录再看别人的作品」是反过来的；首页的产品介绍页早就免登录了，
 * 播客库却卡在门口，逻辑上说不通。
 *
 * 访客走 `/api/library`（只含公开且已完成的），登录后仍然走 `/api/episodes` 看**自己的**
 * 全部单集（含私有的）。两条路的形状不一样，所以在访客模式下把公开条目
 * 映射成卡片要用的 `EpisodeSummary`，并给卡片一个 `/share/:token` 的跳转目标 ——
 * 公开侧没有内部 id，访客也没有 cookie，跳到 `/episode/:id` 只会拿到 401。
 */
const isGuest = computed(() => !session.isLoggedIn)

/** 每个条目的跳转目标：访客去公开分享页，作者去自己的单集页 */
const linkTargets = ref<Record<string, { name: string; params: Record<string, string> }>>({})

function linkToFor(episode: EpisodeSummary) {
  return linkTargets.value[episode.id]
}

function guestSummary(item: PublicEpisodeItem): EpisodeSummary {
  // 逐字段对齐卡片真正用到的那几个；公开侧没有的（source_ref / paper_meta / status 之外
  // 的状态机字段）一律不给值，别用假数据把卡片填满 —— 那看起来像真有这些信息。
  return {
    id: item.token,
    title: item.title,
    status: 'completed',
    source_type: 'url',
    visibility: 'public',
    share_token: item.token,
    created_at: item.created_at,
    language: item.language,
    languages: item.languages,
    audio_duration_sec: item.duration_sec,
    cover_url: item.cover_url,
    cover_width: null,
    cover_height: null,
    video: item.has_video ? {} : null,
    // `options` 是内部参数，公开库故意不给 —— 这里给一个空的，
    // 卡片上「难度」那一格因此留空（`levelText` 会返回空串），不编造数据
    options: {},
  } as unknown as EpisodeSummary
}

async function fetchPublicPage(offset: number, limit: number): Promise<void> {
  const result = await listPublicEpisodes({
    offset,
    limit,
    q: keyword.value.trim() || undefined,
    sort: sort.value,
  })
  const mapped = result.items.map(guestSummary)
  if (offset === 0) items.value = mapped
  else items.value = [...items.value, ...mapped]
  total.value = result.total
  const targets: Record<string, { name: string; params: Record<string, string> }> = {}
  result.items.forEach((item: PublicEpisodeItem) => {
    targets[item.token] = { name: 'share', params: { token: item.token } }
  })
  linkTargets.value = offset === 0 ? targets : { ...linkTargets.value, ...targets }
}

// 列表接口返回的是 EpisodeSummary（不含 analysis / script / figures / illustration）
const items = ref<EpisodeSummary[]>([])
const total = ref(0)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const keyword = ref('')
const status = ref<StatusFilter>('all')
const sort = ref(readLibrarySort())
const view = ref<LibraryViewMode>(readLibraryView())
const confirmTarget = ref<EpisodeSummary | null>(null)
const deleting = ref(false)
const refreshing = ref(false)

/** 专辑（契约 §2.6）：列表上显示「属于哪张专辑」，并提供一个加入/移出的入口 */
const albums = ref<Album[]>([])
const pickerTarget = ref<EpisodeSummary | null>(null)

const albumTitles = computed(() => {
  const map = new Map<string, string>()
  albums.value.forEach((album) => map.set(album.id, album.title))
  return map
})

function albumTitleFor(episode: EpisodeSummary): string | null {
  const id = episode.album_id
  if (!id) return null
  return albumTitles.value.get(id) ?? '未知专辑'
}

async function loadAlbums(): Promise<void> {
  try {
    albums.value = await listAlbums()
  } catch {
    // 专辑读不到不该把播客库整页搞坏：卡片上少一个角标而已
    albums.value = []
  }
}

/**
 * 点卡片上的「加入专辑」：开放模式下先给中文提示 + 登录入口，
 * 不打开一个点了也做不成的弹窗（那是把人引到死路上）
 */
function openAlbumPicker(episode: EpisodeSummary): void {
  if (session.isOpenMode && !session.isLoggedIn) {
    session.requireAccount('把播客加入专辑')
    return
  }
  pickerTarget.value = episode
}

/** 弹窗里改完归属：就地更新这一条，不必整页重拉 */
function onAlbumChanged(albumId: string | null): void {
  const target = pickerTarget.value
  if (!target) return
  const index = items.value.findIndex((episode) => episode.id === target.id)
  if (index >= 0) {
    const next = items.value.slice()
    next[index] = { ...next[index]!, album_id: albumId }
    items.value = next
  }
  notice.value = albumId
    ? `已把《${target.title}》加入专辑`
    : `已把《${target.title}》移出专辑`
  void loadAlbums()
}

let refreshTimer: number | undefined
let debounceTimer: number | undefined
let requestSeq = 0

const hasMore = computed(() => items.value.length < total.value)
const hasActiveTask = computed(() =>
  items.value.some((episode) => episode.status !== 'completed' && episode.status !== 'failed'),
)
const hasFilter = computed(() => Boolean(keyword.value.trim()) || status.value !== 'all')

function params(offset: number, limit: number) {
  return {
    offset,
    limit,
    status: status.value === 'all' ? undefined : status.value,
    q: keyword.value.trim() || undefined,
    sort: sort.value,
  }
}

async function fetchPage(offset: number, limit: number, silent = false): Promise<void> {
  // 用序号丢弃过期响应：静默刷新与用户操作可以并发，只有最新一次的返回会被采用
  const seq = ++requestSeq
  if (!silent) {
    error.value = null
    if (offset === 0) loading.value = true
    else loadingMore.value = true
  }
  try {
    if (isGuest.value) {
      await fetchPublicPage(offset, limit)
      if (seq !== requestSeq) return
    } else {
      const result = await listEpisodes(params(offset, limit))
      if (seq !== requestSeq) return
      if (offset === 0) items.value = result.items
      else items.value = [...items.value, ...result.items]
      total.value = result.total
    }
    error.value = null
  } catch (cause) {
    if (seq !== requestSeq) return
    error.value = errorMessage(cause, '读取播客库失败')
  } finally {
    if (seq === requestSeq) {
      loading.value = false
      loadingMore.value = false
      refreshing.value = false
      syncAutoRefresh()
    }
  }
}

function currentLimit(): number {
  return Math.max(items.value.length, PAGE_SIZE)
}

async function reload(): Promise<void> {
  await fetchPage(0, currentLimit())
}

async function loadMore(): Promise<void> {
  await fetchPage(items.value.length, PAGE_SIZE)
}

async function manualRefresh(): Promise<void> {
  refreshing.value = true
  await reload()
}

function syncAutoRefresh(): void {
  if (!isGuest.value && hasActiveTask.value && refreshTimer === undefined) {
    refreshTimer = window.setInterval(() => {
      if (document.hidden) return
      void fetchPage(0, currentLimit(), true)
    }, AUTO_REFRESH_MS)
  } else if ((isGuest.value || !hasActiveTask.value) && refreshTimer !== undefined) {
    window.clearInterval(refreshTimer)
    refreshTimer = undefined
  }
}

function onConfirmDelete(event: EpisodeSummary): void {
  confirmTarget.value = event
}

async function confirmDelete(): Promise<void> {
  const target = confirmTarget.value
  if (!target) return
  deleting.value = true
  try {
    await deleteEpisode(target.id)
    confirmTarget.value = null
    notice.value = `已删除《${target.title}》`
    await reload()
  } catch (cause) {
    error.value = errorMessage(cause, '删除失败')
    confirmTarget.value = null
  } finally {
    deleting.value = false
  }
}

async function onRetry(episode: EpisodeSummary): Promise<void> {
  notice.value = null
  try {
    await retryEpisode(episode.id)
    notice.value = `已把《${episode.title}》重新排队`
    await reload()
  } catch (cause) {
    error.value = errorMessage(cause, '重新生成失败')
  }
}

function clearFilters(): void {
  keyword.value = ''
  status.value = 'all'
}

function pickView(next: LibraryViewMode): void {
  view.value = next
  saveLibraryView(next)
}

/** 筛选/搜索/排序任一变化都回到第一页 —— 停在第二页看新条件的结果会让人以为丢了数据 */
watch([keyword, status, sort], () => {
  if (debounceTimer !== undefined) window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(() => {
    void fetchPage(0, PAGE_SIZE)
  }, 320)
})

watch(sort, (value) => saveLibrarySort(value))

onMounted(() => {
  void session.ensureInit()
  void fetchPage(0, PAGE_SIZE)
  // 访客没有专辑，也不需要轮询自己的任务
  if (!isGuest.value) void loadAlbums()
})

// 登录态可能来得比首次渲染晚（`/auth/me` 要一次往返）：变了就重新取一遍，
// 否则「刚登录仍看到公开库」会让人以为自己的单集丢了
watch(isGuest, () => {
  void fetchPage(0, PAGE_SIZE)
  if (!isGuest.value) void loadAlbums()
})

onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
  if (debounceTimer !== undefined) window.clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="container page">
    <header class="library-head">
      <div>
        <p class="eyebrow">Library · 播客库</p>
        <h1 class="page-title">已生成的播客</h1>
        <p class="page-subtitle">
          共 {{ total }} 条<template v-if="items.length">，当前显示 {{ items.length }} 条</template>
        </p>
      </div>
      <div class="library-head__actions">
        <button type="button" class="btn btn--ghost" :disabled="refreshing" @click="manualRefresh">
          <span v-if="refreshing" class="spinner" aria-hidden="true" />
          {{ refreshing ? '刷新中…' : '刷新' }}
        </button>
        <template v-if="isGuest">
          <!-- 访客：这两个入口都需要账号，给了也点不通，不如直接说清楚 -->
          <RouterLink to="/login" class="btn btn--ghost">登录</RouterLink>
          <RouterLink to="/create" class="btn btn--primary">导入新论文</RouterLink>
        </template>
        <template v-else>
          <RouterLink to="/albums" class="btn btn--ghost">我的专辑</RouterLink>
          <RouterLink to="/create" class="btn btn--primary">导入新论文</RouterLink>
        </template>
      </div>
    </header>

    <!-- 工具栏：搜索 / 状态 / 排序 / 视图。四个都是「看的方式」，
         所以收在一条里，不再占一张大卡片 -->
    <div class="toolbar">
      <div class="toolbar__search">
        <span class="toolbar__search-icon" aria-hidden="true">⌕</span>
        <input
          id="search"
          v-model="keyword"
          class="input"
          type="search"
          placeholder="按标题搜索，例如：Attention / LoRA"
          aria-label="按标题搜索"
        />
      </div>

      <div v-if="!isGuest" class="chips" role="group" aria-label="按状态筛选">
        <button
          v-for="item in STATUS_FILTERS"
          :key="item.value"
          type="button"
          class="chip"
          :class="{ 'is-active': status === item.value }"
          :aria-pressed="status === item.value"
          @click="status = item.value"
        >
          {{ item.label }}
        </button>
      </div>

      <label class="toolbar__select">
        <span class="toolbar__select-label">排序</span>
        <select v-model="sort" class="select select--sm" aria-label="排序方式">
          <option v-for="option in SORT_OPTIONS" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </label>

      <div class="chips" role="group" aria-label="切换视图">
        <button
          v-for="option in LIBRARY_VIEWS"
          :key="option.value"
          type="button"
          class="chip"
          :class="{ 'is-active': view === option.value }"
          :aria-pressed="view === option.value"
          @click="pickView(option.value)"
        >
          {{ option.label }}
        </button>
      </div>
    </div>

    <p v-if="isGuest" class="section__hint" style="margin: -8px 0 20px">
      这是<strong>公开库</strong>：只列作者分享出来、并且已经生成完成的单集。
      登录后可以看到自己的全部单集（包括还没公开的）。
    </p>

    <p v-if="hasActiveTask" class="section__hint" style="margin: -8px 0 20px">
      有任务正在生成，页面每 5 秒会自动刷新一次状态。
    </p>

    <AccountPrompt />

    <div v-if="notice" class="alert alert--info" style="margin-bottom: 20px">
      <span class="alert__icon" aria-hidden="true">i</span>
      <span class="alert__body">{{ notice }}</span>
    </div>

    <div v-if="error" class="alert alert--error" style="margin-bottom: 20px">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">
        {{ error }}
        <button type="button" class="btn btn--sm btn--ghost" style="margin-left: 10px" @click="reload">
          重试
        </button>
      </span>
    </div>

    <div v-if="loading" class="episode-grid">
      <div v-for="index in 3" :key="index" class="episode-card">
        <div class="episode-card__top">
          <div class="skeleton skeleton--thumb" />
          <div style="min-width: 0; flex: 1">
            <div class="skeleton" style="width: 34%; height: 14px; margin-bottom: 12px" />
            <div class="skeleton" style="width: 88%; height: 20px; margin-bottom: 10px" />
            <div class="skeleton" style="width: 62%" />
          </div>
        </div>
        <div class="skeleton" style="width: 78%" />
      </div>
    </div>

    <div v-else-if="!items.length" class="empty">
      <template v-if="hasFilter">
        <p class="empty__title">没有符合条件的播客</p>
        <p style="margin-bottom: 18px">换个关键词，或把状态筛选改回「全部」。</p>
        <button type="button" class="btn btn--ghost" @click="clearFilters">清除筛选</button>
      </template>
      <template v-else-if="isGuest">
        <p class="empty__title">还没有公开的播客</p>
        <p style="margin-bottom: 18px">作者分享出来的成品会出现在这里。</p>
        <RouterLink to="/" class="btn btn--primary">回首页看看</RouterLink>
      </template>
      <template v-else>
        <p class="empty__title">播客库还是空的</p>
        <p style="margin-bottom: 18px">导入第一篇论文，几分钟后这里就会出现一条双人播客。</p>
        <RouterLink to="/create" class="btn btn--primary">去导入论文</RouterLink>
      </template>
    </div>

    <template v-else>
      <div v-if="view === 'grid'" class="episode-grid">
        <EpisodeCard
          v-for="(episode, index) in items"
          :key="episode.id"
          :episode="episode"
          :priority="index < 3"
          :album-title="isGuest ? null : albumTitleFor(episode)"
          :link-to="linkToFor(episode)"
          :show-album-action="!isGuest"
          :can-manage="!isGuest"
          @delete="onConfirmDelete"
          @retry="onRetry"
          @album="openAlbumPicker"
        />
      </div>

      <div v-else class="episode-list">
        <EpisodeRow
          v-for="episode in items"
          :key="episode.id"
          :episode="episode"
          :album-title="isGuest ? null : albumTitleFor(episode)"
          :link-to="linkToFor(episode)"
          :show-album-action="!isGuest"
          :can-manage="!isGuest"
          @delete="onConfirmDelete"
          @retry="onRetry"
          @album="openAlbumPicker"
        />
      </div>

      <div class="row" style="justify-content: center; margin-top: 30px">
        <button
          v-if="hasMore"
          type="button"
          class="btn"
          :disabled="loadingMore"
          @click="loadMore"
        >
          <span v-if="loadingMore" class="spinner" aria-hidden="true" />
          {{ loadingMore ? '加载中…' : `加载更多（还有 ${total - items.length} 条）` }}
        </button>
        <span v-else class="section__hint">已经到底了 · 共 {{ total }} 条</span>
      </div>
    </template>

    <AlbumPickerDialog
      :open="!isGuest && pickerTarget !== null"
      :episode-id="pickerTarget?.id ?? ''"
      :episode-title="pickerTarget?.title ?? ''"
      :album-id="pickerTarget?.album_id ?? null"
      @close="pickerTarget = null"
      @changed="onAlbumChanged"
    />

    <ConfirmDialog
      :open="!isGuest && confirmTarget !== null"
      title="删除这条播客？"
      :text="`《${confirmTarget?.title ?? ''}》的脚本、解读、音频与视频都会被一起删掉，且无法恢复。`"
      confirm-text="删除"
      danger
      :busy="deleting"
      @confirm="confirmDelete"
      @cancel="confirmTarget = null"
    />
  </div>
</template>
