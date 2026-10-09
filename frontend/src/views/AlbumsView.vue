<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { createAlbum, errorMessage, listAlbums } from '../api'
import type { Album } from '../api'
import AccountPrompt from '../components/AccountPrompt.vue'
import { useSessionStore } from '../stores/session'
import { formatRelative } from '../utils/format'

/**
 * 个人专辑列表（契约 §2.6）。
 *
 * 专辑是「把几期播客归到一起」的分组，不是容器：删专辑不会删单集。
 * 空状态必须给引导 —— 一张专辑都没有时页面不能是白的。
 */
const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const items = ref<Album[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)

const creating = ref(false)
const title = ref('')
const description = ref('')
const busy = ref(false)

const canCreate = computed(() => !busy.value && title.value.trim().length > 0)

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    items.value = await listAlbums()
  } catch (cause) {
    error.value = errorMessage(cause, '读取专辑列表失败')
  } finally {
    loading.value = false
  }
}

/** 需要账号的操作：开放模式下先给中文提示，不发那个注定 401 的请求 */
function openCreateForm(): void {
  if (session.isOpenMode && !session.isLoggedIn) {
    session.requireAccount('新建专辑')
    return
  }
  creating.value = true
}

async function submit(): Promise<void> {
  if (!canCreate.value) return
  busy.value = true
  error.value = null
  try {
    const album = await createAlbum({
      title: title.value.trim(),
      description: description.value.trim() || null,
    })
    notice.value = `已创建专辑《${album.title}》，可以去播客库里把单集加进去。`
    title.value = ''
    description.value = ''
    creating.value = false
    await load()
  } catch (cause) {
    // 开放模式下后端会返回 401「需要登录」：换成中文提示，不把 detail 原样抛给用户
    if (session.isAccountRequired(cause)) session.requireAccount('新建专辑')
    else error.value = errorMessage(cause, '新建专辑失败')
    creating.value = false
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  // 删除专辑后会带着 ?deleted=专辑名 跳回来：把结果说清楚，
  // 并顺手把参数从地址栏去掉（刷新一次又冒出来一条「已删除」很莫名其妙）
  const deleted = route.query.deleted
  const title = Array.isArray(deleted) ? deleted[0] : deleted
  if (typeof title === 'string' && title) {
    notice.value = `已删除专辑《${title}》。里面的单集都还在播客库里，只是不再属于任何专辑。`
    void router.replace({ name: 'albums' })
  }
  void load()
})
</script>

<template>
  <div class="container page">
    <header class="row row--between" style="align-items: flex-end; margin-bottom: 26px">
      <div>
        <p class="eyebrow">Albums · 个人专辑</p>
        <h1 class="page-title" style="margin-bottom: 6px">我的专辑</h1>
        <p class="page-subtitle" style="margin: 0">
          把几期播客归到一起做成主题合集，例如「Transformer 系列」。共 {{ items.length }} 张专辑。
        </p>
      </div>
      <div class="row">
        <RouterLink to="/library" class="btn btn--ghost">播客库</RouterLink>
        <button type="button" class="btn btn--primary" @click="creating ? (creating = false) : openCreateForm()">
          {{ creating ? '收起' : '＋ 新建专辑' }}
        </button>
      </div>
    </header>

    <AccountPrompt />

    <div v-if="creating" class="card card--pad" style="margin-bottom: 24px">
      <div class="form-grid">
        <div class="field">
          <label class="field__label" for="album-title">专辑名称</label>
          <input
            id="album-title"
            v-model="title"
            class="input"
            type="text"
            maxlength="60"
            placeholder="例如：Transformer 系列"
            @keydown.enter.prevent="submit"
          />
        </div>
        <div class="field">
          <label class="field__label" for="album-desc">说明（可选）</label>
          <input
            id="album-desc"
            v-model="description"
            class="input"
            type="text"
            maxlength="200"
            placeholder="例如：从 Attention 到后续跟进工作"
          />
        </div>
      </div>
      <div class="row" style="justify-content: flex-end; margin-top: 18px">
        <button type="button" class="btn btn--primary" :disabled="!canCreate" @click="submit">
          <span v-if="busy" class="spinner" aria-hidden="true" />
          {{ busy ? '创建中…' : '创建专辑' }}
        </button>
      </div>
    </div>

    <div v-if="notice" class="alert alert--info" style="margin-bottom: 20px">
      <span class="alert__icon" aria-hidden="true">i</span>
      <span class="alert__body">{{ notice }}</span>
    </div>

    <div v-if="error" class="alert alert--error" style="margin-bottom: 20px">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">
        {{ error }}
        <button type="button" class="btn btn--sm btn--ghost" style="margin-left: 10px" @click="load">
          重试
        </button>
      </span>
    </div>

    <div v-if="loading" class="episode-grid">
      <div v-for="index in 3" :key="index" class="episode-card">
        <div class="skeleton" style="width: 60%; height: 20px; margin-bottom: 12px" />
        <div class="skeleton" style="width: 90%" />
      </div>
    </div>

    <div v-else-if="!items.length" class="empty">
      <p class="empty__title">还没有专辑</p>
      <p style="margin-bottom: 18px">
        专辑把几期播客收在一起，方便按主题连着听。先建一张，再到播客库里把单集加进去。
      </p>
      <div class="row" style="justify-content: center">
        <button type="button" class="btn btn--primary" @click="openCreateForm">＋ 新建第一张专辑</button>
        <RouterLink to="/library" class="btn btn--ghost">先去播客库</RouterLink>
      </div>
    </div>

    <div v-else class="album-grid">
      <RouterLink v-for="album in items" :key="album.id" :to="{ name: 'album', params: { id: album.id } }" class="album-card card">
        <div class="album-card__cover" :class="{ 'is-empty': !album.cover_url }">
          <img v-if="album.cover_url" :src="album.cover_url" alt="" loading="lazy" decoding="async" />
          <span v-else class="album-card__glyph" aria-hidden="true">◫</span>
        </div>
        <div class="album-card__body">
          <h2 class="album-card__title">{{ album.title }}</h2>
          <p v-if="album.description" class="album-card__desc">{{ album.description }}</p>
          <div class="episode-card__meta">
            <span>{{ album.episode_count }} 集</span>
            <span>更新于 {{ formatRelative(album.updated_at) }}</span>
          </div>
        </div>
      </RouterLink>
    </div>
  </div>
</template>
