<script setup lang="ts">
import { computed } from 'vue'
import type { Episode } from '../api'

/**
 * 论文元信息（标题 / 作者 / 年份 / 会议 / arXiv / 关键词 / 摘要）。
 *
 * 抽成组件是因为详情页有两种展示形态：
 *   - 有视频解读时，元信息要紧跟在标题下方（视频是最主要的产物，不能被压在后面）；
 *   - 没有视频时，元信息保持在原文画廊之后的位置（行为与改造前完全一致）。
 * 两种形态用的是同一份标记，避免两处各写一遍后走样。
 */
const props = defineProps<{
  episode: Episode
}>()

const paper = computed(() => props.episode.paper_meta ?? null)

const arxivUrl = computed(() =>
  paper.value?.arxiv_id ? `https://arxiv.org/abs/${paper.value.arxiv_id}` : null,
)

const metaItems = computed(() => {
  const value = paper.value
  if (!value) return []
  const items: Array<{ label: string; value: string }> = []
  if (value.authors?.length) items.push({ label: '作者', value: value.authors.join('、') })
  if (value.year) items.push({ label: '年份', value: String(value.year) })
  if (value.venue) items.push({ label: '会议 / 期刊', value: value.venue })
  if (value.arxiv_id) items.push({ label: 'arXiv', value: value.arxiv_id })
  return items
})
</script>

<template>
  <section v-if="paper" class="section">
    <div class="section__head">
      <h2 class="section__title">论文元信息</h2>
      <span class="section__hint">{{ episode.source_ref ?? '—' }}</span>
    </div>
    <div class="card card--pad">
      <div class="meta-grid">
        <div class="meta-item">
          <div class="meta-item__label">论文标题</div>
          <div class="meta-item__value">{{ paper.title ?? episode.title }}</div>
        </div>
        <div v-for="item in metaItems" :key="item.label" class="meta-item">
          <div class="meta-item__label">{{ item.label }}</div>
          <div class="meta-item__value">
            <a v-if="item.label === 'arXiv' && arxivUrl" :href="arxivUrl" target="_blank" rel="noopener">
              {{ item.value }} ↗
            </a>
            <template v-else>{{ item.value }}</template>
          </div>
        </div>
      </div>

      <template v-if="paper.keywords?.length">
        <hr class="divider" />
        <div class="tag-list">
          <span v-for="keyword in paper.keywords" :key="keyword" class="tag">{{ keyword }}</span>
        </div>
      </template>

      <template v-if="paper.abstract">
        <hr class="divider" />
        <div class="meta-item__label">摘要</div>
        <p class="analysis-card__body" style="margin: 6px 0 0">{{ paper.abstract }}</p>
      </template>
    </div>
  </section>
</template>
