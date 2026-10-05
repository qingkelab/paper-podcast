<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Figure } from '../api'

/**
 * 论文原图的灯箱：点击遮罩、按 ESC、点右上角关闭都会关闭。
 * 多张图时支持 ← / → 切换。图片加载失败时退化成一张带说明的占位卡，不留破图。
 */
const props = defineProps<{
  figures: Figure[]
  /** null = 关闭 */
  openIndex: number | null
  title?: string
}>()

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'navigate', delta: number): void
}>()

const failed = ref(false)
const closeButton = ref<HTMLButtonElement | null>(null)

const isOpen = computed(() => props.openIndex !== null && props.figures.length > 0)
const current = computed<Figure | null>(() => {
  const index = props.openIndex
  if (index === null) return null
  return props.figures[index] ?? null
})
const hasMultiple = computed(() => props.figures.length > 1)
const positionText = computed(() => {
  if (props.openIndex === null) return ''
  return `${props.openIndex + 1} / ${props.figures.length}`
})

watch(
  () => props.openIndex,
  () => {
    failed.value = false
  },
)

function onKeydown(event: KeyboardEvent): void {
  if (!isOpen.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    emit('navigate', 1)
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault()
    emit('navigate', -1)
  }
}

function setScrollLock(locked: boolean): void {
  document.body.style.overflow = locked ? 'hidden' : ''
}

watch(
  isOpen,
  (open) => {
    setScrollLock(open)
    if (open) {
      // 打开后把焦点移到关闭按钮，键盘用户可以直接按 Esc / Tab
      void Promise.resolve().then(() => closeButton.value?.focus())
    }
  },
  { immediate: true },
)

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  setScrollLock(false)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="isOpen && current" class="lightbox" role="dialog" aria-modal="true" @click.self="emit('close')">
      <div class="lightbox__panel">
        <header class="lightbox__head">
          <div class="lightbox__head-text">
            <span class="badge badge--accent">{{ current.label }}</span>
            <span v-if="title" class="lightbox__paper" :title="title">{{ title }}</span>
            <span v-if="current.page" class="section__hint">第 {{ current.page }} 页</span>
          </div>
          <div class="row" style="gap: 6px">
            <span v-if="hasMultiple" class="lightbox__position">{{ positionText }}</span>
            <button
              v-if="hasMultiple"
              type="button"
              class="icon-btn"
              aria-label="上一张"
              @click="emit('navigate', -1)"
            >
              ←
            </button>
            <button
              v-if="hasMultiple"
              type="button"
              class="icon-btn"
              aria-label="下一张"
              @click="emit('navigate', 1)"
            >
              →
            </button>
            <button
              ref="closeButton"
              type="button"
              class="icon-btn"
              aria-label="关闭（Esc）"
              @click="emit('close')"
            >
              ✕
            </button>
          </div>
        </header>

        <div class="lightbox__stage">
          <img
            v-if="!failed && current.url"
            :src="current.url"
            :alt="current.caption"
            decoding="async"
            @error="failed = true"
          />
          <div v-else class="lightbox__broken">
            <p class="empty__title" style="margin-bottom: 6px">这张图无法加载</p>
            <p class="section__hint" style="margin: 0">
              资源可能已被清理（{{ current.id }}）。其余图片不受影响。
            </p>
          </div>
        </div>

        <footer class="lightbox__foot">
          <p class="lightbox__caption">{{ current.caption }}</p>
          <span class="section__hint">点击遮罩、按 Esc 或点右上角 ✕ 关闭</span>
        </footer>
      </div>
    </div>
  </Teleport>
</template>
