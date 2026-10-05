<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { formatDuration } from '../utils/format'

const props = withDefaults(
  defineProps<{
    /** 音频地址；null 表示还没有音频 */
    src: string | null
    /** Episode.audio_duration_sec：真实时长拿不到时的兜底显示 */
    fallbackDuration?: number | null
    title?: string
  }>(),
  { fallbackDuration: null, title: '' },
)

const RATES = [0.75, 1, 1.25, 1.5]

const audioEl = ref<HTMLAudioElement | null>(null)
const playing = ref(false)
const buffering = ref(false)
const currentTime = ref(0)
const mediaDuration = ref(0)
/** 拖动过程中不为 null：显示拖动位置，松手才真正 seek */
const scrubTime = ref<number | null>(null)
const volume = ref(1)
const muted = ref(false)
const rate = ref(1)
const error = ref<string | null>(null)

const duration = computed(() => {
  if (Number.isFinite(mediaDuration.value) && mediaDuration.value > 0) return mediaDuration.value
  const fallback = props.fallbackDuration
  if (typeof fallback === 'number' && Number.isFinite(fallback) && fallback > 0) return fallback
  return 0
})

const displayTime = computed(() => scrubTime.value ?? currentTime.value)
const percent = computed(() =>
  duration.value > 0 ? Math.min(100, Math.max(0, (displayTime.value / duration.value) * 100)) : 0,
)
const ready = computed(() => Boolean(props.src) && !error.value)

// ---------------------------------------------------------------- 时长获取
// 服务端用 Range/分块响应时，loadedmetadata 阶段的 duration 可能是 Infinity 或 NaN。
// 标准解法：先跳到远超末尾的位置逼浏览器算出总时长，拿到后再跳回开头。
function onLoadedMetadata(): void {
  const el = audioEl.value
  if (!el) return
  const value = el.duration
  if (Number.isFinite(value) && value > 0) {
    mediaDuration.value = value
    return
  }
  void resolveStreamDuration(el)
}

async function resolveStreamDuration(el: HTMLAudioElement): Promise<void> {
  const stop = window.setTimeout(() => el.removeEventListener('durationchange', onDurationChange), 4000)
  function onDurationChange(): void {
    if (!Number.isFinite(el.duration) || el.duration <= 0) return
    mediaDuration.value = el.duration
    el.removeEventListener('durationchange', onDurationChange)
    window.clearTimeout(stop)
    if (el.currentTime > el.duration) el.currentTime = 0
  }
  el.addEventListener('durationchange', onDurationChange)
  try {
    el.currentTime = 1e101
  } catch {
    // 某些浏览器直接抛错，忽略即可（时长会退回 fallbackDuration）
  }
  await Promise.resolve()
}

function onDurationChange(): void {
  const el = audioEl.value
  if (!el) return
  if (Number.isFinite(el.duration) && el.duration > 0) mediaDuration.value = el.duration
}

// ---------------------------------------------------------------- 播放控制
function toggle(): void {
  const el = audioEl.value
  if (!el || !ready.value) return
  if (playing.value) {
    el.pause()
    return
  }
  void el
    .play()
    .catch(() => {
      error.value = '自动播放被浏览器拦截，请再点一次播放按钮'
    })
}

function onScrubInput(event: Event): void {
  const value = Number((event.target as HTMLInputElement).value)
  if (Number.isFinite(value)) scrubTime.value = value
}

function commitSeek(): void {
  const el = audioEl.value
  const target = scrubTime.value
  scrubTime.value = null
  if (!el || target === null) return
  try {
    el.currentTime = target
    currentTime.value = target
  } catch {
    error.value = '该音频不支持跳转'
  }
}

function onTimeUpdate(): void {
  const el = audioEl.value
  if (!el) return
  if (scrubTime.value === null) currentTime.value = el.currentTime
}

function onEnded(): void {
  playing.value = false
  currentTime.value = 0
  const el = audioEl.value
  if (el) el.currentTime = 0
}

function setRate(value: number): void {
  rate.value = value
  const el = audioEl.value
  if (el) el.playbackRate = value
}

function toggleMute(): void {
  muted.value = !muted.value
  const el = audioEl.value
  if (el) el.muted = muted.value
}

function onError(): void {
  error.value = '音频加载失败：地址不可用，或浏览器不支持该音频格式'
}

function updateMediaSession(): void {
  if (!props.title) return
  const session = (navigator as unknown as { mediaSession?: { metadata: unknown } }).mediaSession
  const Meta = (globalThis as unknown as {
    MediaMetadata?: new (init: { title: string; artist?: string }) => unknown
  }).MediaMetadata
  if (!session || !Meta) return
  try {
    session.metadata = new Meta({ title: props.title, artist: '论文解读 AI 播客' })
  } catch {
    // 不支持就算了，不影响播放
  }
}

// ---------------------------------------------------------------- 同步
watch(
  () => props.src,
  () => {
    const el = audioEl.value
    playing.value = false
    currentTime.value = 0
    mediaDuration.value = 0
    scrubTime.value = null
    error.value = null
    if (el) {
      el.load()
      el.playbackRate = rate.value
      el.volume = volume.value
      el.muted = muted.value
    }
    updateMediaSession()
  },
  { immediate: true },
)

watch(volume, (value) => {
  const el = audioEl.value
  if (el) {
    el.volume = value
    if (value > 0 && el.muted) {
      el.muted = false
      muted.value = false
    }
  }
})

watch(rate, (value) => {
  const el = audioEl.value
  if (el) el.playbackRate = value
})

watch(
  () => props.title,
  () => updateMediaSession(),
)

onBeforeUnmount(() => {
  audioEl.value?.pause()
})

defineExpose({ toggle })
</script>

<template>
  <section class="player" aria-label="播客播放器">
    <div class="player__top">
      <button
        type="button"
        class="player__play"
        :disabled="!ready"
        :aria-label="playing ? '暂停' : '播放'"
        :title="playing ? '暂停' : '播放'"
        @click="toggle"
      >
        <span v-if="buffering" class="spinner" aria-hidden="true" />
        <span v-else aria-hidden="true">{{ playing ? '❚❚' : '▶' }}</span>
      </button>

      <div class="player__timeline">
        <div class="player__times">
          <span>{{ formatDuration(displayTime) }}</span>
          <span v-if="!ready && !error" style="color: var(--text-muted)">音频准备中…</span>
          <span>{{ duration > 0 ? formatDuration(duration) : '--:--' }}</span>
        </div>

        <div class="player__scrub">
          <div class="player__track" aria-hidden="true">
            <div class="player__fill" :style="{ width: `${percent}%` }" />
          </div>
          <input
            class="player__range"
            type="range"
            min="0"
            :max="duration > 0 ? duration : 100"
            step="0.05"
            :value="displayTime"
            :disabled="!ready"
            aria-label="播放进度"
            @input="onScrubInput"
            @change="commitSeek"
          />
        </div>
      </div>
    </div>

    <div class="player__bottom">
      <div class="control-group">
        <span class="control-group__label">倍速</span>
        <div class="rate-group" role="group" aria-label="播放倍速">
          <button
            v-for="item in RATES"
            :key="item"
            type="button"
            class="rate-btn"
            :class="{ 'is-active': rate === item }"
            :aria-pressed="rate === item"
            @click="setRate(item)"
          >
            {{ item }}x
          </button>
        </div>
      </div>

      <div class="control-group">
        <span class="control-group__label">音量</span>
        <div class="volume">
          <button
            type="button"
            class="icon-btn"
            :aria-label="muted ? '取消静音' : '静音'"
            :title="muted ? '取消静音' : '静音'"
            @click="toggleMute"
          >
            {{ muted || volume === 0 ? '🔇' : '🔊' }}
          </button>
          <input
            v-model.number="volume"
            class="volume__range"
            type="range"
            min="0"
            max="1"
            step="0.01"
            aria-label="音量"
          />
          <span class="file-pill__size">{{ Math.round((muted ? 0 : volume) * 100) }}%</span>
        </div>
      </div>

      <span class="spacer" />
      <span v-if="muted" class="badge badge--neutral">已静音</span>
    </div>

    <p v-if="error" class="alert alert--error" style="margin: 16px 0 0">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">{{ error }}</span>
    </p>

    <audio
      ref="audioEl"
      class="audio-hidden"
      :src="src ?? undefined"
      preload="metadata"
      @loadedmetadata="onLoadedMetadata"
      @durationchange="onDurationChange"
      @timeupdate="onTimeUpdate"
      @play="playing = true"
      @pause="playing = false"
      @waiting="buffering = true"
      @playing="buffering = false"
      @canplay="buffering = false"
      @ended="onEnded"
      @error="onError"
    />
  </section>
</template>
