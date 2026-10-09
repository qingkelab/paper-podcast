<script setup lang="ts">
import { computed } from 'vue'
import { xIntentUrl } from '../utils/shareX'

/**
 * 「分享到 X」按钮。
 *
 * 用 `<a target="_blank">` 而不是 `window.open()`：这样能中键新标签打开、
 * 能右键复制链接、也不会被弹窗拦截器盯上 —— 这几件事在小按钮上都是免费的可用性。
 *
 * **我们不替用户发帖**：这个链接只打开 X 的发布框，内容预填好，发不发由他决定。
 * 因此这里既不需要 API 密钥，也不需要任何授权。
 */
const props = withDefaults(
  defineProps<{
    /** 已经拼好的推文正文（见 utils/shareX.ts，长度按 X 的加权字数算过） */
    text: string
    label?: string
    /** primary = 主按钮；ghost = 次要；icon = 只有图标（列表行里用） */
    variant?: 'primary' | 'ghost' | 'icon'
    size?: 'sm' | 'md'
    /** 禁用时说明原因 —— 只说「不可用」的灰按钮等于让人猜 */
    disabled?: boolean
    disabledHint?: string
  }>(),
  { label: '分享到 X', variant: 'ghost', size: 'md', disabled: false, disabledHint: '' },
)

const emit = defineEmits<{ (event: 'open'): void }>()

const href = computed(() => xIntentUrl(props.text))

const classes = computed(() => {
  if (props.variant === 'icon') return ['share-x', 'share-x--icon']
  return [
    'btn',
    props.variant === 'primary' ? 'btn--primary' : 'btn--ghost',
    props.size === 'sm' ? 'btn--sm' : '',
    'share-x',
  ]
})

const title = computed(() => {
  if (props.disabled) return props.disabledHint || '暂时无法分享到 X'
  return `分享到 X（在新标签页打开预填好的发布框）`
})
</script>

<template>
  <button
    v-if="disabled"
    type="button"
    :class="classes"
    class="is-disabled"
    disabled
    :title="title"
    :aria-label="title"
  >
    <svg class="share-x__glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"
      />
    </svg>
    <span v-if="variant !== 'icon'">{{ label }}</span>
  </button>

  <a
    v-else
    :class="classes"
    :href="href"
    target="_blank"
    rel="noopener noreferrer"
    :title="title"
    :aria-label="variant === 'icon' ? `${label}（在新标签页打开）` : undefined"
    @click="emit('open')"
  >
    <svg class="share-x__glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"
      />
    </svg>
    <span v-if="variant !== 'icon'">{{ label }}</span>
  </a>
</template>
