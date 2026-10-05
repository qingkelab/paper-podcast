<script setup lang="ts">
import { computed } from 'vue'
import type { Analysis } from '../api'

const props = defineProps<{
  analysis: Analysis | null
}>()

interface TextCard {
  kind: 'text'
  title: string
  body: string
  wide: boolean
}

interface ListCard {
  kind: 'list'
  title: string
  items: string[]
  wide: boolean
}

type Card = TextCard | ListCard

const CARDS: Array<{ key: keyof Analysis; title: string; kind: 'text' | 'list'; wide: boolean }> = [
  { key: 'background', title: '研究背景', kind: 'text', wide: true },
  { key: 'innovations', title: '创新点', kind: 'list', wide: false },
  { key: 'method', title: '研究方法', kind: 'text', wide: true },
  { key: 'experiments', title: '实验结果', kind: 'text', wide: false },
  { key: 'conclusion', title: '核心结论', kind: 'text', wide: false },
  { key: 'limitations', title: '存在不足', kind: 'list', wide: true },
  { key: 'value', title: '行业价值', kind: 'text', wide: false },
  { key: 'future', title: '未来方向', kind: 'list', wide: false },
]

const cards = computed<Card[]>(() => {
  const analysis = props.analysis
  if (!analysis) return []
  return CARDS.flatMap<Card>((definition) => {
    const value = analysis[definition.key]
    if (definition.kind === 'list') {
      const items = Array.isArray(value) ? value.filter((item) => item && item.trim()) : []
      return items.length
        ? [{ kind: 'list' as const, title: definition.title, items, wide: definition.wide }]
        : []
    }
    const text = typeof value === 'string' ? value.trim() : ''
    return text ? [{ kind: 'text' as const, title: definition.title, body: text, wide: definition.wide }] : []
  })
})

function paragraphs(body: string): string[] {
  return body
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
}
</script>

<template>
  <div v-if="!cards.length" class="empty">
    <p class="empty__title">解读还没有生成</p>
    <p style="margin: 0">任务完成后即可在这里看到结构化的论文解读。</p>
  </div>

  <div v-else class="analysis-grid">
    <section
      v-for="(card, index) in cards"
      :key="card.title"
      class="analysis-card"
      :class="{ 'analysis-card--wide': card.wide }"
    >
      <header class="analysis-card__head">
        <span class="analysis-card__index">{{ String(index + 1).padStart(2, '0') }}</span>
        <h3 class="analysis-card__title">{{ card.title }}</h3>
      </header>

      <div v-if="card.kind === 'text'" class="analysis-card__body">
        <p v-for="(line, lineIndex) in paragraphs(card.body)" :key="lineIndex">{{ line }}</p>
      </div>

      <ul v-else class="analysis-list">
        <li v-for="(item, itemIndex) in card.items" :key="itemIndex" class="analysis-list__item">
          <span class="analysis-list__bullet" aria-hidden="true">{{ itemIndex + 1 }}</span>
          <span>{{ item }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>
