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
 */
const props = defineProps<{
  figures: Figure[]
}>()

const emit = defineEmits<{
  (event: 'open', index: number): void
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
</script>

<template>
  <div class="figure-grid">
    <button
      v-for="(figure, index) in figures"
      :key="figure.id"
      type="button"
      class="figure-card"
      @click="emit('open', index)"
    >
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
        <span class="figure-card__label">{{ figure.label }}</span>
        <span class="figure-card__caption" :title="figure.caption">{{ figure.caption }}</span>
      </span>
    </button>
  </div>
</template>
