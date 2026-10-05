<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterView } from 'vue-router'
import AppHeader from './components/AppHeader.vue'
import { IS_MOCK, api } from './api'
import { useMetaStore } from './stores/meta'

const meta = useMetaStore()
const year = new Date().getFullYear()

onMounted(() => {
  void meta.load()
})
</script>

<template>
  <div class="app-shell">
    <AppHeader />

    <main class="app-main">
      <RouterView v-slot="{ Component }">
        <component :is="Component" class="rise" />
      </RouterView>
    </main>

    <footer class="app-footer">
      <div class="app-footer__inner">
        <span>论文解读 AI 播客 · 前端演示 © {{ year }}</span>
        <span>
          数据来源：{{ IS_MOCK ? '前端内置 Mock' : `真实后端（api.mode = ${api.mode}）` }}
          <template v-if="meta.version"> · 版本 {{ meta.version }}</template>
        </span>
      </div>
    </footer>
  </div>
</template>
