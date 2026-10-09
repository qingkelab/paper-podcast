<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Figure, FigureRotateDirection } from '../api'
import ConfirmDialog from './ConfirmDialog.vue'

/**
 * 论文原图的灯箱：点击遮罩、按 ESC、点右上角关闭都会关闭。
 * 多张图时支持 ← / → 切换。图片加载失败时退化成一张带说明的占位卡，不留破图。
 *
 * 这里同时是**人工校正配图**的主场：放大看图才能判断「这张图正不正」，
 * 所以逆时针 / 顺时针 / 删除三个操作都放在灯箱里。操作即时保存（没有「保存」按钮），
 * 由父组件调接口、把返回的 Episode 就地刷回来，灯箱里的图和尺寸跟着变，
 * 方便连续转几下直到看着满意。
 *
 * 组件本身不发请求：它只管把意图 emit 出去，并且把「进行中 / 失败」如实画出来。
 * 是否人工改过后端没有记录，所以「已人工校正」只认父组件传进来的本次会话集合。
 */
const props = withDefaults(
  defineProps<{
    figures: Figure[]
    /** null = 关闭 */
    openIndex: number | null
    title?: string
    /** 正在进行的写操作（旋转/删除）；非 null 时所有按钮禁用，防止连点发出重复请求 */
    pending?: FigureRotateDirection | 'delete' | null
    /** 上一次操作的失败原因（保留到下一次操作开始） */
    error?: string | null
    /** 本次会话内被人工校正过的配图 id（前端本地记录，后端没有这个字段） */
    correctedIds?: string[]
    /**
     * 只读（公开分享页）：隐藏旋转/删除那一栏。
     * 公开页拿的是 `/api/share/{token}/…`，本来就没有写接口，摆按钮只会让人白点。
     */
    readonly?: boolean
  }>(),
  {
    title: '',
    pending: null,
    error: null,
    correctedIds: () => [],
    readonly: false,
  },
)

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'navigate', delta: number): void
  (event: 'rotate', direction: FigureRotateDirection): void
  (event: 'delete'): void
}>()

const failed = ref(false)
const closeButton = ref<HTMLButtonElement | null>(null)
/** 删除的二次确认（误删一张图没法撤回，必须问一次） */
const confirming = ref(false)

const isOpen = computed(() => props.openIndex !== null && props.figures.length > 0)
const busy = computed(() => props.pending !== null)
const current = computed<Figure | null>(() => {
  const index = props.openIndex
  if (index === null) return null
  return props.figures[index] ?? null
})
const hasMultiple = computed(() => props.figures.length > 1)
const isCorrected = computed(() => {
  const figure = current.value
  return figure ? props.correctedIds.includes(figure.id) : false
})
/** 转完宽高会对调，直接显示出来，用眼判断方向的同时也能看到尺寸真的变了 */
const sizeText = computed(() => {
  const figure = current.value
  if (!figure?.width || !figure?.height) return ''
  return `${figure.width}×${figure.height}`
})
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

// 成功删除后父组件会关掉灯箱；失败时父组件把 error 传回来。
// 两种情况都不该让确认框继续挂着（否则用户看不到失败原因）。
watch([isOpen, () => props.error], ([open, error]) => {
  if (!open || error) confirming.value = false
})

/**
 * 键盘处理挂在**捕获阶段**，这是必须的。
 *
 * 二次确认对话框（ConfirmDialog）的 ESC 处理挂在它自己的遮罩上（冒泡阶段）。
 * 如果灯箱用冒泡阶段的 window 监听，事件顺序会变成：
 * 遮罩先处理 ESC（emit cancel → confirming 变 false，对话框关闭），
 * 然后才轮到 window 上的灯箱处理 —— 此时 confirming 已经是 false，
 * 于是灯箱也跟着关了。实测就是这个表现：按一次 ESC 把对话框和灯箱一起关掉。
 * 换成捕获阶段，灯箱先看到「正在确认」，直接让路。
 */
function onKeydown(event: KeyboardEvent): void {
  if (!isOpen.value) return
  // 二次确认对话框自己处理 ESC，这里不要抢（否则一按 ESC 连灯箱一起关了）
  if (confirming.value) return
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

function askDelete(): void {
  if (busy.value) return
  confirming.value = true
}

function confirmDelete(): void {
  if (busy.value) return
  emit('delete')
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
  // 捕获阶段，理由见 onKeydown 上方的注释
  window.addEventListener('keydown', onKeydown, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown, true)
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
            <span v-if="isCorrected" class="badge badge--corrected">已人工校正</span>
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
          <div v-if="!readonly" class="lightbox__tools">
            <span class="lightbox__tools-label">方向不对？</span>
            <button
              type="button"
              class="btn btn--sm"
              :disabled="busy"
              aria-label="逆时针旋转 90 度"
              @click="emit('rotate', 'ccw')"
            >
              <span v-if="pending === 'ccw'" class="spinner" aria-hidden="true" />
              <span v-else aria-hidden="true">↺</span>
              逆时针
            </button>
            <button
              type="button"
              class="btn btn--sm"
              :disabled="busy"
              aria-label="顺时针旋转 90 度"
              @click="emit('rotate', 'cw')"
            >
              <span v-if="pending === 'cw'" class="spinner" aria-hidden="true" />
              <span v-else aria-hidden="true">↻</span>
              顺时针
            </button>
            <span class="spacer" />
            <button
              type="button"
              class="btn btn--sm btn--danger"
              :disabled="busy"
              @click="askDelete"
            >
              <span v-if="pending === 'delete'" class="spinner" aria-hidden="true" />
              删除这张
            </button>
          </div>

          <p v-if="error" class="lightbox__error" role="alert">
            <span aria-hidden="true">!</span>
            {{ error }}
          </p>

          <p class="lightbox__caption">{{ current.caption }}</p>

          <div class="lightbox__foot-meta">
            <span class="section__hint">
              {{ readonly ? '由作者分享的公开配图' : '旋转即时保存，90° 无损 · 转多了转回来即可' }}
            </span>
            <span class="spacer" />
            <span v-if="sizeText" class="file-pill__size">{{ sizeText }}</span>
            <span class="section__hint">点击遮罩、按 Esc 或点右上角 ✕ 关闭</span>
          </div>
        </footer>
      </div>
    </div>

    <ConfirmDialog
      :open="confirming"
      elevated
      danger
      title="删除这张配图？"
      :text="`将从这一集移除「${current?.label ?? ''}」并删除该图片文件，源 PDF 不受影响。删掉后就找不回来了。`"
      confirm-text="删除"
      :busy="pending === 'delete'"
      @cancel="confirming = false"
      @confirm="confirmDelete"
    />
  </Teleport>
</template>
