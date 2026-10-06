<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Figure } from '../api'

/**
 * 论文原图画廊。只负责把网格画出来、把「第几张」抛给页面（灯箱由页面统一管理，
 * 这样灯箱能 Teleport 到 body，不受折叠区影响）。
 *
 * 注意：这个组件会被放进收起的 <details> 里（有视频时）。收起状态下的元素尺寸是 0，
 * 所以这里不做任何依赖尺寸的计算——图片用 aspect-ratio 撑开 .figure-card__frame，
 * 展开后浏览器会自己重新布局并加载图片。
 *
 * 配图方向是程序自动判定的，不可能总对（判「图正不正」最终只能靠人眼），
 * 所以卡片右上角给一个快捷「↻」；真正的校正主场在灯箱里（放大才看得准）。
 * 快捷按钮刻意做成卡片的**兄弟节点**而不是嵌在卡片按钮里 —— 按钮套按钮是非法 HTML，
 * 浏览器和屏幕阅读器的行为都不可预期。
 */
const props = withDefaults(
  defineProps<{
    figures: Figure[]
    /** 正在写操作的配图 id（旋转/删除中）：该卡的快捷旋转按钮显示 loading 并禁用 */
    busyId?: string | null
    /** 本次会话内被人工校正过的配图 id（前端本地记录，后端没有这个字段） */
    correctedIds?: string[]
  }>(),
  {
    busyId: null,
    correctedIds: () => [],
  },
)

const emit = defineEmits<{
  (event: 'open', index: number): void
  (event: 'rotate', figure: Figure): void
}>()

/** 加载失败的图：退化成占位卡，不留破图 */
const broken = ref<string[]>([])

/** 组件会被复用（/episode/a → /episode/b 命中同一条路由），换了数据就要清掉失败标记 */
watch(
  () => props.figures,
  () => {
    broken.value = []
  },
)

function isBroken(figure: Figure): boolean {
  return broken.value.includes(figure.id)
}

function markBroken(figureId: string): void {
  if (!broken.value.includes(figureId)) broken.value = [...broken.value, figureId]
}

function isCorrected(figure: Figure): boolean {
  return props.correctedIds.includes(figure.id)
}
</script>

<template>
  <div class="figure-panel">
    <!--
      这句提示是功能的一部分：自动判定方向会出错，但用户不知道能改，这个功能就白做了。
      放在标题与网格之间，第一眼就能看到。
    -->
    <p class="figure-note">
      <span class="figure-note__icon" aria-hidden="true">↻</span>
      配图方向由程序按图内文字方向自动判定，可能出错 ——
      点开任意一张可以手动旋转纠正，多余或没用的图也能删掉。
    </p>

    <div class="figure-grid">
      <div v-for="(figure, index) in figures" :key="figure.id" class="figure-cell">
        <button type="button" class="figure-card" @click="emit('open', index)">
          <span class="figure-card__frame">
            <img
              v-if="!isBroken(figure)"
              :src="figure.url"
              :alt="figure.caption"
              loading="lazy"
              decoding="async"
              @error="markBroken(figure.id)"
            />
            <span v-else class="figure-card__broken">
              <span class="figure-card__broken-glyph" aria-hidden="true">◫</span>
              图片暂时加载不出来
            </span>
          </span>
          <span class="figure-card__body">
            <span class="figure-card__tags">
              <span class="figure-card__label">{{ figure.label }}</span>
              <span v-if="isCorrected(figure)" class="figure-card__flag">已校正</span>
            </span>
            <span class="figure-card__caption" :title="figure.caption">{{ figure.caption }}</span>
          </span>
        </button>

        <button
          type="button"
          class="figure-card__quick"
          :disabled="busyId !== null"
          :aria-label="`顺时针旋转 ${figure.label} 90 度`"
          title="顺时针旋转 90°（点开大图还能逆时针转或删除）"
          @click="emit('rotate', figure)"
        >
          <span v-if="busyId === figure.id" class="spinner" aria-hidden="true" />
          <span v-else aria-hidden="true">↻</span>
        </button>
      </div>
    </div>
  </div>
</template>
