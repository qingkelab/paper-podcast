<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  addAlbumEpisodes,
  createAlbum,
  errorMessage,
  listAlbums,
  removeAlbumEpisode,
} from '../api'
import type { Album } from '../api'
import AccountPrompt from './AccountPrompt.vue'
import { useSessionStore } from '../stores/session'

/**
 * 「加入专辑 / 移出专辑」的选择器（契约 §2.6）。
 *
 * 播客库与详情页共用这一个弹窗：单集同一时刻只能属于一张专辑
 * （后端是 `episodes.album_id` 一个字段，加到新专辑会自动把它从旧专辑挪走），
 * 所以这里做成**单选**：选哪张就归哪张，选「不加入」就移出当前专辑。
 *
 * 空专辑列表是常态（刚上线时一张都没有），所以弹窗里直接能新建专辑 ——
 * 让人先去另一个页面建好再回来，是白白把一件事拆成两件。
 */
const props = withDefaults(
  defineProps<{
    open: boolean
    episodeId: string
    episodeTitle?: string
    /** 这一集当前所属的专辑 id（null = 不在任何专辑里） */
    albumId?: string | null
  }>(),
  { episodeTitle: '', albumId: null },
)

const emit = defineEmits<{
  (event: 'close'): void
  /** 归属变化了：带上新的 album_id（null = 已移出） */
  (event: 'changed', albumId: string | null): void
}>()

const session = useSessionStore()
const albums = ref<Album[]>([])
const loading = ref(true)
/** 选中的专辑；空字符串表示「不加入任何专辑」 */
const selected = ref<string>('')
const creating = ref(false)
const newTitle = ref('')
const busy = ref(false)
const error = ref<string | null>(null)

const current = computed(() => props.albumId ?? '')

/** 当前归属的那张专辑（可能在列表里已经不存在了，那就只显示 id） */
const currentTitle = computed(() => {
  if (!props.albumId) return null
  return albums.value.find((album) => album.id === props.albumId)?.title ?? null
})

const dirty = computed(() => selected.value !== current.value)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    albums.value = await listAlbums()
    selected.value = props.albumId ?? ''
  } catch (cause) {
    error.value = errorMessage(cause, '读取专辑列表失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    creating.value = false
    newTitle.value = ''
    error.value = null
    void load()
  },
)

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && !busy.value) emit('close')
}

async function createAndSelect(): Promise<void> {
  const title = newTitle.value.trim()
  if (!title) {
    error.value = '专辑名称不能为空'
    return
  }
  busy.value = true
  error.value = null
  try {
    const album = await createAlbum({ title })
    albums.value = [album, ...albums.value]
    selected.value = album.id
    creating.value = false
    newTitle.value = ''
  } catch (cause) {
    error.value = errorMessage(cause, '新建专辑失败')
  } finally {
    busy.value = false
  }
}

async function confirm(): Promise<void> {
  if (!dirty.value) {
    emit('close')
    return
  }
  busy.value = true
  error.value = null
  try {
    if (selected.value) {
      // 加到目标专辑（后端是「改 album_id」，所以顺带就把它从旧专辑挪走了）
      await addAlbumEpisodes(selected.value, [props.episodeId])
      emit('changed', selected.value)
    } else if (props.albumId) {
      await removeAlbumEpisode(props.albumId, props.episodeId)
      emit('changed', null)
    } else {
      emit('changed', null)
    }
    emit('close')
  } catch (cause) {
    if (session.isAccountRequired(cause)) {
      session.requireAccount('把播客加入专辑')
      emit('close')
    } else {
      error.value = errorMessage(cause, '更新专辑归属失败')
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="加入专辑"
      tabindex="-1"
      @keydown="onKeydown"
      @click.self="!busy && emit('close')"
    >
      <div class="modal modal--wide">
        <h3 class="modal__title">加入专辑</h3>
        <p class="modal__text">
          <template v-if="episodeTitle">《{{ episodeTitle }}》</template>
          当前：<strong>{{ currentTitle ?? (albumId ? albumId : '不在任何专辑里') }}</strong>
          <template v-if="albums.length"> · 一集同时只能属于一张专辑</template>
        </p>

        <AccountPrompt />

        <div v-if="loading" class="stack" style="margin-bottom: 18px">
          <div class="skeleton" style="height: 38px" />
          <div class="skeleton" style="height: 38px" />
        </div>

        <template v-else>
          <div v-if="!albums.length" class="empty" style="padding: 18px 0">
            <p class="empty__title">还没有任何专辑</p>
            <p style="margin-bottom: 14px">专辑是给播客分组的合集，比如「Transformer 系列」。</p>
          </div>

          <div v-else class="album-picker" role="radiogroup" aria-label="选择专辑">
            <label class="album-picker__item" :class="{ 'is-active': selected === '' }">
              <input v-model="selected" type="radio" value="" />
              <span>不加入任何专辑</span>
            </label>
            <label
              v-for="album in albums"
              :key="album.id"
              class="album-picker__item"
              :class="{ 'is-active': selected === album.id }"
            >
              <input v-model="selected" type="radio" :value="album.id" />
              <span>{{ album.title }}</span>
              <span class="album-picker__count">{{ album.episode_count }} 集</span>
            </label>
          </div>

          <div v-if="creating" class="row" style="margin-top: 14px; gap: 10px">
            <input
              v-model="newTitle"
              class="input"
              type="text"
              maxlength="60"
              placeholder="新专辑名称，例如：Transformer 系列"
              @keydown.enter.prevent="createAndSelect"
            />
            <button type="button" class="btn btn--sm btn--primary" :disabled="busy" @click="createAndSelect">
              创建
            </button>
            <button type="button" class="btn btn--sm btn--ghost" :disabled="busy" @click="creating = false">
              取消
            </button>
          </div>
          <button
            v-else
            type="button"
            class="btn btn--sm btn--ghost"
            style="margin-top: 14px"
            @click="creating = true"
          >
            ＋ 新建专辑
          </button>

          <div v-if="error" class="alert alert--error" style="margin-top: 16px">
            <span class="alert__icon" aria-hidden="true">!</span>
            <span class="alert__body">{{ error }}</span>
          </div>
        </template>

        <div class="modal__actions" style="margin-top: 22px">
          <button type="button" class="btn btn--ghost" :disabled="busy" @click="emit('close')">取消</button>
          <button type="button" class="btn btn--primary" :disabled="busy || loading" @click="confirm">
            <span v-if="busy" class="spinner" aria-hidden="true" />
            {{ dirty ? '保存' : '完成' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
