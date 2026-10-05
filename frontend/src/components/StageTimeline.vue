<script setup lang="ts">
import { computed } from 'vue'
import type { EpisodeStatus } from '../api'
import { STAGES } from '../utils/stages'

const props = defineProps<{
  status: EpisodeStatus
  progress: number
}>()

const failed = computed(() => props.status === 'failed')

const reachedIndex = computed(() => {
  const index = STAGES.findIndex((stage) => stage.status === props.status)
  if (index >= 0) return index
  // failed：按进度反推中断在哪一阶段
  let current = 0
  STAGES.forEach((stage, i) => {
    if (props.progress >= stage.progress) current = i
  })
  return current
})

function stateOf(index: number): string {
  if (failed.value) {
    if (index < reachedIndex.value) return 'is-done'
    if (index === reachedIndex.value) return 'is-failed'
    return ''
  }
  if (index < reachedIndex.value) return 'is-done'
  if (index === reachedIndex.value) return 'is-active'
  return ''
}
</script>

<template>
  <ol class="timeline">
    <li
      v-for="(stage, index) in STAGES"
      :key="stage.status"
      class="timeline__item"
      :class="stateOf(index)"
    >
      <span class="timeline__dot" aria-hidden="true">
        <template v-if="stateOf(index) === 'is-done'">✓</template>
      </span>
      <span class="timeline__body">
        <span class="timeline__label">{{ stage.label }}</span>
        <span class="timeline__meta">
          {{ stage.progress }}%<template v-if="failed && index === reachedIndex"> · 中断</template>
        </span>
      </span>
    </li>
  </ol>
</template>
