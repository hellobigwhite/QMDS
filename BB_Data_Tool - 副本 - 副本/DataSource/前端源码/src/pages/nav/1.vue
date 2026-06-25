<template>
  <div class="container">
    <el-card class="card">
      <el-form ref="form" label-width="120px">
        <el-form-item label="数据源列表：">
          <el-input
            v-model="urls"
            type="textarea"
            :rows="30"
            placeholder="请输入数据源列表，每行一个 URL，自动去重。添加过程由于需要发送请求识别是否是Shopify并且获取对应站点信息。请耐心等待，不要关闭页面！"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSubmit">提交</el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script lang="ts" setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import axios from 'axios'

const urls = ref('')

// 定义获取 meta.json 的函数，带有重试机制（最多 2 次）和超时时间 10 秒
async function fetchMeta(metaUrl: string, maxAttempts = 2): Promise<any> {
  let attempt = 0
  while (attempt < maxAttempts) {
    try {
      const response = await axios.get(metaUrl, { timeout: 10000 })
      return response.data
    } catch (error) {
      attempt++
      if (attempt >= maxAttempts) {
        throw error
      }
    }
  }
}

const handleSubmit = async () => {
  // 对输入的 URL 去重并过滤空行
  const urlSet = new Set(
    urls.value.split('\n').map(url => url.trim()).filter(url => url)
  )
  const urlArray = Array.from(urlSet)

  if (!urlArray.length) {
    ElMessage({
      showClose: true,
      message: '请输入至少一个 URL',
      type: 'error'
    })
    return
  }

  let successCount = 0
  let failureCount = 0

  // 逐条处理每个 URL
  for (const originalUrl of urlArray) {
    // 检查是否带协议头，不带则默认添加 https://
    let fixedUrl = originalUrl
    if (!/^https?:\/\//i.test(fixedUrl)) {
      fixedUrl = 'https://' + fixedUrl
    }
    // 去掉结尾的 /
    fixedUrl = fixedUrl.replace(/\/+$/, '')

    // 默认数据结构
    let dataSource = {
      url: fixedUrl,
      site_name: '',         // 默认站点名称
      site_title: '',        // 根据需求设置默认值
      site_describe: '',
      site_language: '',
      site_currency: '',
      site_techstack: '',
      ai_analysis_summary: '',
      product_volume: 0,
      data_volume: 0,
      analysis_result: '',
      remark: '',
      status: '新增',         // 默认状态
      custom_field_1: '',
      custom_field_2: '',
      tags: ''               // 标签为空字符串
    }

    // 拼接 /meta.json
    const metaUrl = fixedUrl + '/meta.json'
    try {
      const meta = await fetchMeta(metaUrl, 2)
      // 获取 meta.json 成功，则设置技术栈为 "shopify" 并提取相关字段
      dataSource.site_techstack = 'Shopify'
      dataSource.site_name = meta.name || ''
      dataSource.site_currency = meta.currency || ''
      dataSource.site_describe = meta.description || ''
      dataSource.product_volume = meta.published_products_count || 0
      dataSource.status = '新增'
    } catch (error) {
      // 获取 meta.json 失败时，保留默认数据
    }

    // 提交当前数据源
    try {
      const response = await axios.post('http://127.0.0.1:8050/data_sources/', dataSource)
      if (response.status === 200 || response.status === 201) {
        successCount++
        ElMessage({
          showClose: true,
          message: `成功添加数据源：${dataSource.url}`,
          type: 'success'
        })
      } else {
        failureCount++
        ElMessage({
          showClose: true,
          message: `添加数据源失败：${dataSource.url}`,
          type: 'error'
        })
      }
    } catch (error) {
      failureCount++
      ElMessage({
        showClose: true,
        message: `添加数据源失败：${dataSource.url}`,
        type: 'error'
      })
    }
  }

  // 所有 URL 处理完成后，显示汇总信息（永久显示，需用户手动关闭）
  ElMessage({
    showClose: true,
    message: `总计：成功 ${successCount} 条，失败 ${failureCount} 条。`,
    duration: 0,
    type: 'info'
  })
}
</script>

<style scoped>
.container {
  display: flex;
  justify-content: center;
  align-items: center;
  margin: 20px;
}
.card {
  width: 100%;
  max-width: 100%;
}
</style>
