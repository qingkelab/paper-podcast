<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { IS_MOCK } from '../api'

const configuredUrl = import.meta.env.VITE_QINGKE_JIEDU_URL?.trim()
const serviceUrl = computed(() =>
  configuredUrl ||
  (IS_MOCK
    ? 'https://qingkelab.github.io/qingke-jiedu/web/'
    : 'http://127.0.0.1:4781/'),
)
const embedUrl = computed(() => {
  const url = new URL(serviceUrl.value)
  url.searchParams.set('embed', '1')
  return url.toString()
})
const loaded = ref(false)
const frame = ref<HTMLIFrameElement | null>(null)
const frameHeight = ref(900)

function handleFrameMessage(event: MessageEvent) {
  if (event.source !== frame.value?.contentWindow || event.origin !== new URL(serviceUrl.value).origin) return
  if (event.data?.type !== 'qingke-embed:resize' || typeof event.data.height !== 'number') return
  frameHeight.value = Math.min(12000, Math.max(760, Math.ceil(event.data.height)))
}

onMounted(() => window.addEventListener('message', handleFrameMessage))
onBeforeUnmount(() => window.removeEventListener('message', handleFrameMessage))
</script>

<template>
  <section class="quick-page">
    <header class="quick-page__head">
      <div>
        <p class="eyebrow">QUICK EXPLAINER</p>
        <h1 class="page-title">快速图文解读</h1>
        <p class="page-subtitle">
          把论文或网页快速整理成图文和解读文案。需要完整双人播客时，使用「开始生成」。
        </p>
      </div>
      <a class="btn btn--ghost" :href="serviceUrl" target="_blank" rel="noreferrer">
        单独打开
      </a>
    </header>

    <p class="quick-page__note">
      快速图文会复用主应用 .env 中的模型密钥；也可在 integrations/qingke-jiedu/.env 单独配置。
      单纯转图不需要模型密钥。
    </p>
    <div class="quick-frame-wrap">
      <div v-if="!loaded" class="quick-frame-loading" role="status">
        正在连接快速图文服务…
      </div>
      <iframe
        ref="frame"
        class="quick-frame"
        :src="embedUrl"
        :style="{ height: `${frameHeight}px` }"
        title="青稞解读快速图文工具"
        allow="clipboard-read; clipboard-write; fullscreen; downloads"
        @load="loaded = true"
      />
    </div>
  </section>
</template>

<style scoped>
.quick-page {
  width: min(1440px, 100%);
  margin: 0 auto;
  padding: 32px 28px 0;
}

.quick-page__head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
}

.quick-page__head .page-title {
  margin: 0 0 8px;
}

.quick-page__note {
  margin: 0 0 14px;
  color: var(--text-muted);
  font-size: 13px;
}

.quick-frame-wrap {
  position: relative;
  min-height: 760px;
  overflow: hidden;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  background: var(--surface);
}

.quick-frame {
  display: block;
  width: 100%;
  min-height: 760px;
  border: 0;
  background: transparent;
}

.quick-frame-loading {
  position: absolute;
  z-index: 1;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--text-muted);
  background: var(--surface);
  pointer-events: none;
  animation: quick-loading-out 0s 1.2s forwards;
}

@keyframes quick-loading-out {
  to { opacity: 0; visibility: hidden; }
}

@media (max-width: 640px) {
  .quick-page { padding: 24px 16px 0; }
  .quick-page__head { align-items: start; }
  .quick-frame-wrap, .quick-frame { min-height: 680px; }
}
</style>
