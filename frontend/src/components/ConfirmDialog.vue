<script setup lang="ts">
import { ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title?: string
    text?: string
    confirmText?: string
    cancelText?: string
    danger?: boolean
    busy?: boolean
    /**
     * 浮到灯箱之上（灯箱 z-index 70，普通遮罩 60）。
     * 在灯箱里做二次确认时必须打开，否则对话框会藏在灯箱下面看不见也点不到。
     */
    elevated?: boolean
  }>(),
  {
    title: '确认操作',
    text: '',
    confirmText: '确认',
    cancelText: '取消',
    danger: false,
    busy: false,
    elevated: false,
  },
)

const emit = defineEmits<{
  (event: 'confirm'): void
  (event: 'cancel'): void
}>()

const confirmButton = ref<HTMLButtonElement | null>(null)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    window.setTimeout(() => confirmButton.value?.focus(), 30)
  },
)

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && !props.busy) emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="modal-backdrop"
      :class="{ 'modal-backdrop--elevated': elevated }"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      tabindex="-1"
      @keydown="onKeydown"
      @click.self="!busy && emit('cancel')"
    >
      <div class="modal">
        <h3 class="modal__title">{{ title }}</h3>
        <p class="modal__text">{{ text }}</p>
        <div class="modal__actions">
          <button type="button" class="btn btn--ghost" :disabled="busy" @click="emit('cancel')">
            {{ cancelText }}
          </button>
          <button
            ref="confirmButton"
            type="button"
            class="btn"
            :class="danger ? 'btn--danger' : 'btn--primary'"
            :disabled="busy"
            @click="emit('confirm')"
          >
            <span v-if="busy" class="spinner" aria-hidden="true" />
            {{ confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
