<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  addAlbumEpisodes,
  deleteAlbum,
  errorMessage,
  getAlbum,
  isApiError,
  listEpisodes,
  removeAlbumEpisode,
  updateAlbum,
} from '../api'
import type { AlbumDetail, EpisodeSummary } from '../api'
import AccountPrompt from '../components/AccountPrompt.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import EpisodeCard from '../components/EpisodeCard.vue'
import { useSessionStore } from '../stores/session'

/**
 * 专辑详情（契约 §2.6）：改名/说明、加单集、移出单集、删专辑。
 *
 * **删专辑不删单集** —— 后端只把 `album_id` 置空，所以确认文案必须说清这一点，
 * 否则没人敢按那个红色按钮。
 */
const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const id = computed(() => String(route.params.id ?? ''))
const album = ref<AlbumDetail | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const notFound = ref(false)
const notice = ref<string | null>(null)

const editing = ref(false)
const form = ref({ title: '', description: '' })
const saving = ref(false)
const busyEpisodeId = ref<string | null>(null)

const confirmDelete = ref(false)
const deleting = ref(false)

/** 加单集：列出自己所有单集，勾选后批量加入 */
const pickerOpen = ref(false)
const allEpisodes = ref<EpisodeSummary[]>([])
const pickerLoading = ref(false)
const pickerError = ref<string | null>(null)
const picked = ref<string[]>([])

const episodes = computed(() => album.value?.episodes ?? [])
const inAlbumIds = computed(() => new Set(episodes.value.map((episode) => episode.id)))
const candidates = computed(() => allEpisodes.value.filter((episode) => !inAlbumIds.value.has(episode.id)))
const canSave = computed(() => !saving.value && form.value.title.trim().length > 0)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const value = await getAlbum(id.value)
    album.value = value
    form.value = { title: value.title, description: value.description ?? '' }
    notFound.value = false
  } catch (cause) {
    // 别人的专辑也返回 404（契约：越权不返回 403，避免泄漏「这个 id 存在」）
    if (isApiError(cause) && cause.status === 404) {
      notFound.value = true
      album.value = null
    }
    error.value = errorMessage(cause, '读取专辑失败')
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!canSave.value || !album.value) return
  saving.value = true
  error.value = null
  try {
    const updated = await updateAlbum(album.value.id, {
      title: form.value.title.trim(),
      description: form.value.description.trim(),
    })
    notice.value = `已保存为《${updated.title}》`
    editing.value = false
    await load()
  } catch (cause) {
    if (session.isAccountRequired(cause)) session.requireAccount('修改专辑')
    else error.value = errorMessage(cause, '保存失败')
  } finally {
    saving.value = false
  }
}

async function removeEpisode(episode: EpisodeSummary): Promise<void> {
  if (!album.value || busyEpisodeId.value) return
  busyEpisodeId.value = episode.id
  error.value = null
  try {
    const detail = await removeAlbumEpisode(album.value.id, episode.id)
    album.value = detail
    notice.value = `已把《${episode.title}》移出专辑（单集本身还在播客库里）`
  } catch (cause) {
    if (session.isAccountRequired(cause)) session.requireAccount('移出专辑')
    else error.value = errorMessage(cause, '移出专辑失败')
  } finally {
    busyEpisodeId.value = null
  }
}

async function openPicker(): Promise<void> {
  pickerOpen.value = true
  pickerLoading.value = true
  pickerError.value = null
  picked.value = []
  try {
    const result = await listEpisodes({ limit: 100 })
    allEpisodes.value = result.items
  } catch (cause) {
    pickerError.value = errorMessage(cause, '读取播客库失败')
  } finally {
    pickerLoading.value = false
  }
}

function togglePick(episodeId: string): void {
  picked.value = picked.value.includes(episodeId)
    ? picked.value.filter((item) => item !== episodeId)
    : [...picked.value, episodeId]
}

async function confirmPicker(): Promise<void> {
  if (!album.value || !picked.value.length) return
  saving.value = true
  pickerError.value = null
  try {
    const detail = await addAlbumEpisodes(album.value.id, picked.value)
    album.value = detail
    notice.value = `已加入 ${picked.value.length} 集`
    pickerOpen.value = false
    picked.value = []
  } catch (cause) {
    if (session.isAccountRequired(cause)) session.requireAccount('加入专辑')
    else pickerError.value = errorMessage(cause, '加入专辑失败')
  } finally {
    saving.value = false
  }
}

async function doDelete(): Promise<void> {
  if (!album.value) return
  deleting.value = true
  error.value = null
  try {
    const title = album.value.title
    await deleteAlbum(album.value.id)
    // 用 query 把提示带到列表页，避免「删完跳走什么都没说」
    await router.replace({ name: 'albums', query: { deleted: title } })
  } catch (cause) {
    if (session.isAccountRequired(cause)) session.requireAccount('删除专辑')
    else error.value = errorMessage(cause, '删除专辑失败')
    confirmDelete.value = false
  } finally {
    deleting.value = false
  }
}

watch(id, () => {
  album.value = null
  notice.value = null
  editing.value = false
  pickerOpen.value = false
  void load()
})

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="container page">
    <div v-if="loading && !album" class="card card--pad">
      <div class="skeleton" style="width: 45%; height: 26px; margin-bottom: 16px" />
      <div class="skeleton" style="width: 70%" />
    </div>

    <div v-else-if="notFound" class="card card--pad">
      <div class="empty">
        <p class="empty__title">找不到这张专辑</p>
        <p style="margin-bottom: 18px">{{ error }}</p>
        <RouterLink :to="{ name: 'albums' }" class="btn btn--primary">回到专辑列表</RouterLink>
      </div>
    </div>

    <template v-else-if="album">
      <header class="page__head">
        <p class="eyebrow">
          <RouterLink :to="{ name: 'albums' }" style="color: inherit">专辑</RouterLink> · {{ album.id }}
        </p>
        <div class="row row--between" style="align-items: flex-start">
          <div style="min-width: 0; max-width: 70ch">
            <h1 class="page-title" style="margin-bottom: 8px">{{ album.title }}</h1>
            <p class="page-subtitle" style="margin: 0">
              {{ album.episode_count }} 集<template v-if="album.description"> · {{ album.description }}</template>
            </p>
          </div>
          <div class="page__actions">
            <button type="button" class="btn btn--ghost" @click="editing = !editing">
              {{ editing ? '收起' : '改名 / 改说明' }}
            </button>
            <button type="button" class="btn" @click="openPicker">＋ 加入单集</button>
            <button type="button" class="btn btn--danger" @click="confirmDelete = true">删除专辑</button>
          </div>
        </div>
      </header>

      <div v-if="editing" class="card card--pad" style="margin-bottom: 24px">
        <div class="form-grid">
          <div class="field">
            <label class="field__label" for="album-edit-title">专辑名称</label>
            <input id="album-edit-title" v-model="form.title" class="input" type="text" maxlength="60" />
          </div>
          <div class="field">
            <label class="field__label" for="album-edit-desc">说明</label>
            <input id="album-edit-desc" v-model="form.description" class="input" type="text" maxlength="200" />
          </div>
        </div>
        <div class="row" style="justify-content: flex-end; margin-top: 18px">
          <button type="button" class="btn btn--primary" :disabled="!canSave" @click="save">
            <span v-if="saving" class="spinner" aria-hidden="true" />
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>

      <AccountPrompt />

      <div v-if="notice" class="alert alert--info" style="margin-bottom: 20px">
        <span class="alert__icon" aria-hidden="true">i</span>
        <span class="alert__body">{{ notice }}</span>
      </div>

      <div v-if="error" class="alert alert--error" style="margin-bottom: 20px">
        <span class="alert__icon" aria-hidden="true">!</span>
        <span class="alert__body">{{ error }}</span>
      </div>

      <div v-if="!episodes.length" class="empty">
        <p class="empty__title">这张专辑还是空的</p>
        <p style="margin-bottom: 18px">专辑只是分组：加进来的单集仍然留在播客库里。</p>
        <div class="row" style="justify-content: center">
          <button type="button" class="btn btn--primary" @click="openPicker">＋ 加入单集</button>
          <RouterLink to="/library" class="btn btn--ghost">去播客库</RouterLink>
        </div>
      </div>

      <div v-else class="episode-grid">
        <div v-for="episode in episodes" :key="episode.id" class="album-episode">
          <EpisodeCard :episode="episode" :show-album-action="false" />
          <div class="album-episode__bar">
            <span class="section__hint">属于本专辑</span>
            <span class="spacer" />
            <button
              type="button"
              class="btn btn--sm btn--ghost"
              :disabled="busyEpisodeId === episode.id"
              @click="removeEpisode(episode)"
            >
              <span v-if="busyEpisodeId === episode.id" class="spinner" aria-hidden="true" />
              {{ busyEpisodeId === episode.id ? '移出中…' : '移出专辑' }}
            </button>
          </div>
        </div>
      </div>
    </template>

    <!-- 加单集：勾选式，一次可以加多集（契约 §2.6 的批量加入） -->
    <Teleport to="body">
      <div
        v-if="pickerOpen"
        class="modal-backdrop"
        role="dialog"
        aria-modal="true"
        aria-label="加入单集"
        @click.self="pickerOpen = false"
      >
        <div class="modal modal--wide">
          <h3 class="modal__title">加入单集</h3>
          <p class="modal__text">
            勾选要放进《{{ album?.title ?? '' }}》的单集。已经在里面、或者属于别的专辑的单集不会列出来。
          </p>

          <div v-if="pickerLoading" class="stack">
            <div class="skeleton" style="height: 44px" />
            <div class="skeleton" style="height: 44px" />
          </div>
          <div v-else-if="!candidates.length" class="empty" style="padding: 14px 0">
            <p class="empty__title">没有可加入的单集</p>
            <p style="margin: 0">你的所有单集都已经在这张专辑里了。</p>
          </div>
          <div v-else class="album-picker album-picker--multi">
            <label
              v-for="episode in candidates"
              :key="episode.id"
              class="album-picker__item"
              :class="{ 'is-active': picked.includes(episode.id) }"
            >
              <input
                type="checkbox"
                :checked="picked.includes(episode.id)"
                @change="togglePick(episode.id)"
              />
              <span>{{ episode.title }}</span>
              <span class="album-picker__count">{{ episode.stage_label }}</span>
            </label>
          </div>

          <div v-if="pickerError" class="alert alert--error" style="margin-top: 14px">
            <span class="alert__icon" aria-hidden="true">!</span>
            <span class="alert__body">{{ pickerError }}</span>
          </div>

          <div class="modal__actions" style="margin-top: 22px">
            <button type="button" class="btn btn--ghost" @click="pickerOpen = false">取消</button>
            <button
              type="button"
              class="btn btn--primary"
              :disabled="!picked.length || saving"
              @click="confirmPicker"
            >
              <span v-if="saving" class="spinner" aria-hidden="true" />
              加入 {{ picked.length || '' }} 集
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <ConfirmDialog
      :open="confirmDelete"
      title="删除这张专辑？"
      :text="`只删除专辑本身，《${album?.title ?? ''}》里的 ${album?.episode_count ?? 0} 集播客不受影响，仍然在播客库里。`"
      confirm-text="删除专辑"
      danger
      :busy="deleting"
      @confirm="doDelete"
      @cancel="confirmDelete = false"
    />
  </div>
</template>
