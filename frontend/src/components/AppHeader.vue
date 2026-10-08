<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useMetaStore } from '../stores/meta'

const meta = useMetaStore()

const links = [
  { to: '/', label: '导入论文' },
  { to: '/library', label: '播客库' },
  { to: '/about', label: '项目介绍' },
  { to: '/settings', label: '设置' },
]
</script>

<template>
  <header class="app-header">
    <div class="app-header__inner">
      <RouterLink to="/" class="brand">
        <span class="brand__mark" aria-hidden="true">播</span>
        <span class="brand__text">
          <span class="brand__name">论文解读播客</span>
          <span class="brand__tagline">Paper → Podcast</span>
        </span>
      </RouterLink>

      <nav class="app-nav" aria-label="主导航">
        <RouterLink
          v-for="link in links"
          :key="link.to"
          :to="link.to"
          class="nav-link"
          exact-active-class="is-active"
        >
          {{ link.label }}
        </RouterLink>
      </nav>

      <span v-if="meta.showMockBadge" class="badge--mock" :title="meta.mockDetail">
        <span aria-hidden="true">◈</span> Mock 模式
      </span>
      <RouterLink
        v-else-if="meta.error"
        to="/settings"
        class="badge badge--failed"
        title="点击前往设置页查看后端状态"
      >
        后端未连接
      </RouterLink>
    </div>
  </header>
</template>
