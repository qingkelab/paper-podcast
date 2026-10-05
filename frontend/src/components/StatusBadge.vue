<script setup lang="ts">
import { computed } from 'vue'
import type { EpisodeStatus } from '../api'
import { STATUS_LABELS } from '../utils/stages'

const props = defineProps<{
  status: EpisodeStatus
  label?: string
}>()

const variant = computed(() => {
  if (props.status === 'completed') return 'badge--completed'
  if (props.status === 'failed') return 'badge--failed'
  if (props.status === 'queued') return 'badge--neutral'
  return 'badge--running'
})

const text = computed(() => props.label ?? STATUS_LABELS[props.status] ?? props.status)
</script>

<template>
  <span class="badge" :class="variant">
    <span class="badge__dot" aria-hidden="true" />
    {{ text }}
  </span>
</template>
