<script lang="ts" setup>
import { repository } from '~/../package.json'

import { toggleDark } from '~/composables'
import {
  Document,
  Menu as IconMenu,
  Warning,
  Setting,
  CreditCard,
  MapLocation,
  Coin,
  Grid,
  Sugar,
  GoodsFilled,
  DocumentAdd
} from '@element-plus/icons-vue'
</script>

<template>
  <el-menu class="el-menu-demo" mode="horizontal" :ellipsis="false" router>
    <el-menu-item index="/">
      <div class="flex items-center justify-center gap-2">
        <div class="text-xl" i-ep-element-plus />
        <span>个人数据源管理系统</span>
      </div>
    </el-menu-item>

  
    <el-menu-item index="/nav/1">
      <el-icon>
        <DocumentAdd />
      </el-icon>
      <template #title>
        批量添加URL
      </template>
    </el-menu-item>
    <el-menu-item index="/nav/2">
      <el-icon>
        <Coin />
      </el-icon>
      <template #title>
        数据源管理
      </template>
    </el-menu-item>
    <el-menu-item index="/nav/3">
      <el-icon>
        <Document />
      </el-icon>
      <template #title>
        产品标签管理
      </template>
    </el-menu-item>
    
    <el-menu-item h="full" @click="toggleDark()">
      <button
        class="w-full cursor-pointer border-none bg-transparent"
        style="height: var(--ep-menu-item-height)"
      >
        <i inline-flex i="dark:ep-moon ep-sunny" />
      </button>
    </el-menu-item>

  </el-menu>
</template>

<style lang="scss">
.el-menu-demo {
  &.ep-menu--horizontal > .ep-menu-item:nth-child(1) {
    margin-right: auto;
  }
}
</style>
