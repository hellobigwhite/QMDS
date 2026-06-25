<script lang="ts" setup>
import {
  Document,
  Menu as IconMenu,
  Coin,
  DocumentAdd
} from '@element-plus/icons-vue'

// const isCollapse = ref(true)
function handleOpen(key: string, keyPath: string[]) {
  // eslint-disable-next-line no-console
  console.log(key, keyPath)
}
function handleClose(key: string, keyPath: string[]) {
  // eslint-disable-next-line no-console
  console.log(key, keyPath)
}
</script>

<template>
  <el-menu
    router
    default-active="1"
    class="el-menu-vertical-demo"
    @open="handleOpen"
    @close="handleClose"
  >
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
        标签管理
      </template>
    </el-menu-item>
    

  </el-menu>
</template>
