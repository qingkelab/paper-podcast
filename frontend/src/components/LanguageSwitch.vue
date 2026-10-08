<script setup lang="ts">
/**
 * 语言版本切换器（中文 / English）。
 *
 * 首页用它两处：**首屏视频上**和**成品展示区标题旁**。两处是**各自独立**的状态 ——
 * 首屏那个只换首屏那段视频，展示区那个换整块（视频/音频/脚本/解读/信息图）。
 * 所以这里刻意做成受控组件：状态在父组件手里，两处互不影响。
 *
 * 样式复用详情页那套 `.lang-switch`；`overlay` 变体叠在视频上时用更实的底色，
 * 否则白底的论文画面会把按钮衬得看不清。
 */
import type { EpisodeLanguage } from '../api'
import { languageLabel } from '../utils/language'

withDefaults(
  defineProps<{
    languages: EpisodeLanguage[]
    modelValue: EpisodeLanguage | null
    /** 是否显示左侧「语言版本」这行小字 */
    label?: string
    /** 叠在视频/图片上 */
    overlay?: boolean
    /** 按钮提示文案；不给就用通用文案。切换范围不同时应当传（首屏只换视频） */
    hint?: (language: EpisodeLanguage) => string
  }>(),
  { label: '', overlay: false, hint: undefined },
)

const emit = defineEmits<{ 'update:modelValue': [EpisodeLanguage] }>()
</script>

<template>
  <div class="lang-switch-group" :class="{ 'lang-switch-group--overlay': overlay }">
    <span v-if="label" class="lang-switch__label">{{ label }}</span>
    <div class="lang-switch" role="group" aria-label="切换语言版本">
      <button
        v-for="language in languages"
        :key="language"
        type="button"
        class="lang-switch__btn"
        :class="{ 'is-active': language === modelValue }"
        :aria-pressed="language === modelValue"
        :title="hint ? hint(language) : `切换到${languageLabel(language)}版`"
        @click="emit('update:modelValue', language)"
      >
        {{ languageLabel(language) }}
      </button>
    </div>
  </div>
</template>
