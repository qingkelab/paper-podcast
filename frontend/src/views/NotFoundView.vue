<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="container page">
    <div class="card card--pad">
      <div class="empty">
        <p class="empty__title">这个页面不存在</p>
        <p style="margin-bottom: 18px">检查一下链接，或者从下面两个入口继续。</p>
        <div class="row" style="justify-content: center">
          <RouterLink to="/" class="btn btn--primary">导入论文</RouterLink>
          <RouterLink to="/library" class="btn btn--ghost">播客库</RouterLink>
        </div>
      </div>
    </div>
  </div>
</template>
