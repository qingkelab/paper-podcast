<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useSessionStore } from '../stores/session'

/**
 * 「这个功能需要账号」的提示条。
 *
 * 场景：**开放模式**（后端库里还没有任何账号）下点「新建专辑」「开启分享」这类操作。
 * 开放模式只是「不强制登录」，不是「全部功能都能用」—— 专辑和分享都需要一个归属者。
 * 这种时候：
 * - **不自动跳 `/login`**（开放模式本来就不该跳登录页，跳过去会让人以为被挡在门外）；
 * - **不把后端的 `{"detail": "需要登录"}` 原样弹给用户**（那是给开发看的报错）；
 * - 就地给一句中文说明 + 一个明确的去 /login 的入口。
 *
 * 提示状态放在会话 store 里：专辑、详情页、弹窗三处都要用同一条提示，
 * 各写各的迟早会出现两种说法。
 */
const session = useSessionStore()
</script>

<template>
  <div v-if="session.accountPrompt" class="alert alert--warning" style="margin-bottom: 18px" role="status">
    <span class="alert__icon" aria-hidden="true">!</span>
    <span class="alert__body">
      <span class="alert__title">这个功能需要账号</span>
      {{ session.accountPrompt }}
      <span class="row alert__actions">
        <RouterLink :to="{ name: 'login' }" class="btn btn--sm btn--primary">
          去创建账号 / 登录
        </RouterLink>
        <button type="button" class="btn btn--sm btn--ghost" @click="session.clearAccountPrompt()">
          知道了
        </button>
      </span>
    </span>
  </div>
</template>
