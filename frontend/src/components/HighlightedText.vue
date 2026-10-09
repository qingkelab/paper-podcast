<script setup lang="ts">
import { computed } from 'vue'
import { splitByKeywords } from '../utils/highlight'

/**
 * 把一段正文按关键词切成片段渲染（契约 §2.9）。
 *
 * **刻意不提供 `v-html`**：这里渲染的是模型生成的原文，字符串拼 `<mark>` 就等着
 * 正文里的 `<` 变成标签。切成 `[{ text, hit }]` 交给 Vue 渲染，文本永远只是文本节点。
 */
const props = withDefaults(
  defineProps<{
    text: string | null | undefined
    keywords?: readonly string[] | null
    /** 关掉时是纯文本（详情页那个开关） */
    enabled?: boolean
  }>(),
  { keywords: null, enabled: true },
)

const segments = computed(() =>
  props.enabled
    ? splitByKeywords(props.text, props.keywords)
    : [{ text: props.text ?? '', hit: false }],
)
</script>

<template>
  <template v-for="(segment, index) in segments" :key="index">
    <mark v-if="segment.hit" class="hl">{{ segment.text }}</mark>
    <template v-else>{{ segment.text }}</template>
  </template>
</template>
