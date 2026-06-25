<template>
  <div style="margin-bottom: 10px;">

    <el-card class="filter-card">
      <el-row :gutter="10" style="margin-bottom: 5px;">
        <el-col :span="2">
          <el-select v-model="selectedCategory" placeholder="选择分类" size="small">
            <el-option v-for="category in categories" :key="category.id" :label="category.categoryName"
              :value="category.id" />
          </el-select>
        </el-col>
        <el-col :span="2">
          <el-select v-model="selectedTechStack" placeholder="选择技术栈" size="small">
            <el-option v-for="techStack in techStacks" :key="techStack.id" :label="techStack.techStack"
              :value="techStack.id" />
          </el-select>
        </el-col>
        <el-col :span="2">
          <el-select v-model="selectedCurrency" placeholder="选择币种" size="small">
            <el-option v-for="currency in currencys" :key="currency.id" :label="currency.name" :value="currency.id" />
          </el-select>
        </el-col>
        <el-col :span="2">
          <el-select v-model="selectedStatus" placeholder="选择状态" size="small">
            <el-option
              v-for="status in ['新增', '采集失败', '已采集', '已清洗', '已处理重复标题', '已体检', '已删除不合格变体', '已转换币种', '已过滤价格', '已过滤违禁词', '预选', '已拆分数据']"
              :key="status" :label="status" :value="status" />
          </el-select>
        </el-col>
        <el-col :span="6">
          <el-text class="mx-1">数据量范围：</el-text>
          <el-input-number v-model="minDataVolume" placeholder="最小值" size="small" />
          <span style="margin: 0 5px;">-</span>
          <el-input-number v-model="maxDataVolume" placeholder="最大值" size="small" />
        </el-col>
        <el-col :span="3">
          <el-input v-model="search" placeholder="搜索URL,备注,标题..." size="small" />
        </el-col>
        <el-col :span="1">
          <el-button type="success" plain @click="fetchData" size="small">筛选搜索</el-button>
        </el-col>
        <el-col :span="1">
          <el-button type="success" plain @click="resetFilters" size="small">清除选项</el-button>
        </el-col>
        <el-col :span="1">
          <el-button type="primary" plain @click="selectAll" size="small">选择全部</el-button>
        </el-col>
        <el-col :span="1">
          <el-button type="primary" plain @click="deselectAll" size="small">取消选择</el-button>
        </el-col>
        <el-col :span="1">
          <el-button @click="handleAdd" size="small">新增数据</el-button>
        </el-col>
        <el-col :span="1">
          <!-- 触发抽屉显示的按钮 -->
          <el-button type="danger" @click="showDrawer" size="small">批量修改</el-button>
        </el-col>
        <el-col :span="1">
          <!-- 触发抽屉显示的按钮 -->
          <el-button type="danger" @click="showNewDrawer" size="small">特殊服务</el-button>
        </el-col>
      </el-row>
    </el-card>


    <!-- 数据源数据表组件 -->
    <el-table :data="tableData" style="width: 100%" @selection-change="handleSelectionChange" ref="multipleTableRef">
      <!-- 选择列 -->
      <el-table-column type="selection" width="70"></el-table-column>
      <!-- 数据列 -->
      <el-table-column label="ID" prop="id" width="70" />
      <el-table-column label="创建时间" prop="createdDate" width="110" />
      <el-table-column label="URL" prop="url" width="300">
        <template #default="scope">
          <el-link :href="scope.row.url" type="primary" target="_blank"
            :class="{ 'active-link': scope.$index === activeIndex }" @click="handleLinkClick(scope.$index)">
            {{ scope.row.url }}
          </el-link>
        </template>
      </el-table-column>
      <el-table-column label="分类" prop="categoryName" width="100">
        <template #default="scope">
          <el-tag>{{ scope.row.categoryName }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="技术栈" prop="techStackName" width="100">
        <template #default="scope">
          <el-tag>{{ scope.row.techStackName }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="站名" prop="name" width="100" show-overflow-tooltip sortable />
      <el-table-column label="变体处理" prop="variants" width="120" show-overflow-tooltip sortable />
      <el-table-column label="填充分类" prop="customCategory" width="120" show-overflow-tooltip sortable />
      <el-table-column label="币种" prop="currencyName" width="100" />
      <el-table-column label="数据量" prop="dataVolume" width="100" sortable />
      <el-table-column label="备注" prop="remark" width="160" show-overflow-tooltip />
      <el-table-column label="状态" prop="status" width="150">
        <template #default="scope">
          <el-tag>{{ scope.row.status }}</el-tag>
        </template>
      </el-table-column>

      <!-- 子表格展开列 -->
      <el-table-column type="expand" label="其他信息" width="100">
        <template #default="scope">
          <!-- 子表格 -->
          <el-table :data="[scope.row]" style="width: 100%">
            <el-table-column v-if="scope.row.title" label="标题" prop="title" show-overflow-tooltip />
            <el-table-column v-if="scope.row.describe" label="描述" prop="describe" show-overflow-tooltip />
            <el-table-column v-if="scope.row.language" label="语种" prop="language" show-overflow-tooltip />
            <el-table-column v-if="scope.row.ntdTranslate" label="翻译" prop="ntdTranslate" show-overflow-tooltip />
          </el-table>
        </template>
      </el-table-column>

      <!-- 操作列 -->
      <el-table-column fixed="right" label="操作" align="center" width="200">
        <template #default="scope">
          <el-button size="small" @click="handleEdit(scope.$index, scope.row)">编辑</el-button>
          <el-button size="small" @click="handleDelete(scope.$index, scope.row)">删除</el-button>
          <el-button size="small" type="success" @click="openDatasourceFolder(scope.row.id)">目录</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>


  <!-- 数据表分页组件 -->
  <div style="display: flex; justify-content: center; margin-top: 20px;">
    <el-pagination background :page-size="pageSize" :current-page="currentPage" :total="totalRecords"
      @current-change="handlePageChange" layout="prev, pager, next, jumper, ->, total" />
  </div>

  <!-- 编辑数据弹窗 -->
  <el-dialog v-model="dialogVisible" title="编辑数据源" @close="handleDialogClose">
    <el-form :model="editForm" ref="form" label-width="120px">
      <el-row :gutter="20">
        <el-col :span="12">
          <!-- URL -->
          <el-form-item label="URL">
            <el-input v-model="editForm.url" />
          </el-form-item>

          <!-- 分类 -->
          <el-form-item label="分类">
            <el-select v-model="editForm.categoryId" placeholder="请选择分类">
              <el-option v-for="category in categories" :key="category.id" :label="category.categoryName"
                :value="category.id" />
            </el-select>
          </el-form-item>

          <!-- 技术栈 -->
          <el-form-item label="技术栈">
            <el-select v-model="editForm.techStackId" placeholder="请选择技术栈">
              <el-option v-for="techStack in techStacks" :key="techStack.id" :label="techStack.techStack"
                :value="techStack.id" />
            </el-select>
          </el-form-item>

          <!-- 标题 -->
          <el-form-item label="标题">
            <el-input v-model="editForm.title" placeholder="请输入标题" />
          </el-form-item>

          <!-- 描述 -->
          <el-form-item label="描述">
            <el-input type="textarea" v-model="editForm.describe" placeholder="请输入描述" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <!-- 站名 -->
          <el-form-item label="站名">
            <el-input v-model="editForm.name" placeholder="请输入站名" />
          </el-form-item>

          <!-- 币种 -->
          <el-form-item label="币种">
            <el-select v-model="editForm.currencyId" placeholder="请选择币种">
              <el-option v-for="currency in currencys" :key="currency.id" :label="currency.name" :value="currency.id" />
            </el-select>
          </el-form-item>

          <!-- 填充空分类 -->
          <el-form-item label="空分类填充">
            <el-input v-model="editForm.customCategory" placeholder="请输入英文,用来填充空的Categories" />
          </el-form-item>

          <!-- 翻译 -->
          <el-form-item label="翻译">
            <el-input type="textarea" v-model="editForm.ntdTranslate" placeholder="请输入翻译内容" />
          </el-form-item>

          <!-- 语种 -->
          <el-form-item label="语种">
            <el-input v-model="editForm.language" placeholder="请输入语种" />
          </el-form-item>

          <!-- 变体处理方式 -->
          <el-form-item label="变体处理">
            <el-select v-model="editForm.variants" placeholder="请选择变体处理方式">
              <el-option v-for="option in variantOptions" :key="option.value" :label="option.label"
                :value="option.value" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <!-- 数据量 -->
      <el-form-item label="数据量">
        <el-input v-model="editForm.dataVolume" placeholder="请输入数据量" />
      </el-form-item>

      <!-- 备注 -->
      <el-form-item label="备注">
        <el-input type="textarea" v-model="editForm.remark" placeholder="请输入备注" />
      </el-form-item>

      <!-- 状态 -->
      <el-form-item label="状态">
        <el-select v-model="editForm.status" placeholder="请选择状态">
          <el-option label="新增" value="新增"></el-option>
          <el-option label="采集失败" value="采集失败" />
          <el-option label="已采集" value="已采集"></el-option>
          <el-option label="已清洗" value="已清洗"></el-option>
          <el-option label="已处理重复标题" value="已处理重复标题"></el-option>
          <el-option label="已体检" value="已体检"></el-option>
          <el-option label="已删除不合格变体" value="已删除不合格变体"></el-option>
          <el-option label="已转换币种" value="已转换币种"></el-option>
          <el-option label="已过滤价格" value="已过滤价格"></el-option>
          <el-option label="已过滤违禁词" value="已过滤违禁词"></el-option>
          <el-option label="预选" value="预选"></el-option>
          <el-option label="已拆分数据" value="已拆分数据"></el-option>
        </el-select>
      </el-form-item>
    </el-form>

    <span slot="footer" class="dialog-footer">
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSave">保存</el-button>
    </span>
  </el-dialog>

  <!-- 批量修改分类弹窗 -->
  <el-dialog v-model="batchUpdateCategoryDialogVisible" title="批量更新分类" @close="handleBatchUpdateCategoryDialogClose">
    <el-form :model="batchUpdateCategoryForm" label-width="120px">
      <el-form-item label="分类">
        <el-select v-model="batchUpdateCategoryForm.categoryId" placeholder="请选择分类">
          <el-option v-for="category in categories" :key="category.id" :label="category.categoryName"
            :value="category.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchUpdateCategoryDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchUpdateCategory">确定</el-button>
    </span>
  </el-dialog>

  <!-- 批量修改技术栈弹窗 -->
  <el-dialog v-model="batchUpdateTechStackDialogVisible" title="批量更新技术栈" @close="handleBatchUpdateTechStackDialogClose">
    <el-form :model="batchUpdateTechStackForm" label-width="120px">
      <el-form-item label="技术栈">
        <el-select v-model="batchUpdateTechStackForm.techStackId" placeholder="请选择技术栈">
          <el-option v-for="techStack in techStacks" :key="techStack.id" :label="techStack.techStack"
            :value="techStack.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchUpdateTechStackDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchUpdateTechStack">确定</el-button>
    </span>
  </el-dialog>


  <!-- 批量修改币种弹窗 -->
  <el-dialog v-model="batchUpdateCurrencyDialogVisible" title="批量更新币种" @close="handleBatchUpdateCurrencyDialogClose">
    <el-form :model="batchUpdateCurrencyForm" label-width="120px">
      <el-form-item label="币种">
        <el-select v-model="batchUpdateCurrencyForm.currencyId" placeholder="请选择币种">
          <el-option v-for="currency in currencys" :key="currency.id" :label="currency.name" :value="currency.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchUpdateCurrencyDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchUpdateCurrency">确定</el-button>
    </span>
  </el-dialog>


  <!-- 批量修改填充分类弹窗 -->
  <el-dialog v-model="batchUpdateCustomCategoryDialogVisible" title="批量更新自定义分类"
    @close="handleBatchUpdateCustomCategoryDialogClose">
    <el-form :model="batchUpdateCustomCategoryForm" label-width="120px">
      <el-form-item label="自定义分类">
        <el-input v-model="batchUpdateCustomCategoryForm.customCategory" placeholder="请输入自定义分类" />
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchUpdateCustomCategoryDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchUpdateCustomCategory">确定</el-button>
    </span>
  </el-dialog>


  <!-- 批量修改变体处理方式弹窗 -->
  <el-dialog v-model="batchUpdateVariantsDialogVisible" title="批量更新变体处理方式"
    @close="handleBatchUpdateVariantsDialogClose">
    <el-form :model="batchUpdateVariantsForm" label-width="120px">
      <el-form-item label="变体处理方式">
        <el-select v-model="batchUpdateVariantsForm.variants" placeholder="请选择变体处理方式">
          <el-option v-for="option in variantOptions" :key="option.value" :label="option.label" :value="option.value" />
          <!-- 根据实际情况添加更多选项 -->
        </el-select>
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchUpdateVariantsDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchUpdateVariants">确定</el-button>
    </span>
  </el-dialog>

  <!-- 批量修改状态弹窗 -->
  <el-dialog v-model="batchUpdateStatusDialogVisible" title="批量更新状态" @close="handleBatchUpdateStatusDialogClose">
    <el-form :model="batchUpdateStatusForm" label-width="120px">
      <el-form-item label="状态">
        <el-select v-model="batchUpdateStatusForm.status" placeholder="请选择状态">
          <el-option label="新增" value="新增"></el-option>
          <el-option label="采集失败" value="采集失败" />
          <el-option label="已采集" value="已采集"></el-option>
          <el-option label="已清洗" value="已清洗"></el-option>
          <el-option label="已处理重复标题" value="已处理重复标题"></el-option>
          <el-option label="已体检" value="已体检"></el-option>
          <el-option label="已删除不合格变体" value="已删除不合格变体"></el-option>
          <el-option label="已转换币种" value="已转换币种"></el-option>
          <el-option label="已过滤价格" value="已过滤价格"></el-option>
          <el-option label="已过滤违禁词" value="已过滤违禁词"></el-option>
          <el-option label="预选" value="预选"></el-option>
          <el-option label="已拆分数据" value="已拆分数据"></el-option>
        </el-select>
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchUpdateStatusDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchUpdateStatus">确定</el-button>
    </span>
  </el-dialog>


  <!---------------------------------------------------->

  <!-- 输入价格范围的弹窗 -->
  <el-dialog v-model="filterPriceDialogVisible" title="过滤价格" @close="handleFilterPriceDialogClose">
    <el-form :model="filterPriceForm" label-width="100px">
      <!-- 最小价格 -->
      <el-form-item label="最小价格">
        <el-input-number v-model="filterPriceForm.minimumPrice" :min="0.01" placeholder="请输入最小价格" />
      </el-form-item>

      <!-- 最大价格 -->
      <el-form-item label="最大价格">
        <el-input-number v-model="filterPriceForm.maximumPrice" :min="0.01" placeholder="请输入最大价格" />
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="filterPriceDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSaveFilterPrice">确定</el-button>
    </span>
  </el-dialog>

  <!-- 输入拆分数量的弹窗 -->
  <el-dialog v-model="splitDialogVisible" title="数据拆分" @close="handleSplitDialogClose">
    <el-form :model="splitForm" label-width="100px">
      <!-- 显示选中数据源的 dataVolume -->
      <el-form-item label="数据量">
        <el-input v-model="splitForm.dataVolume" :disabled="true" placeholder="数据量" />
        <span v-if="selectedRows.length > 1" class="el-input__suffix">（选中多个数据源时，只显示第一个数据源的数据量）</span>
      </el-form-item>

      <el-form-item label="拆分数量">
        <el-input-number v-model="splitForm.splitSize" :min="1" placeholder="请输入每份数量" />
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="splitDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSaveSplit">确定</el-button>
    </span>
  </el-dialog>

  <!-- 功能组件抽屉 -->
  <el-drawer v-model="drawerVisible" :direction="direction" size="15%">
    <template #header>
      <h4>批量修改功能</h4>
    </template>
    <template #default>
      <div class="drawer-content">
        <!-- 按钮容器 -->
        <div class="drawer-buttons-container">
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openLinksBatchDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量打开网址</el-button>
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openBatchUpdateStatusDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量修改状态</el-button>
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openBatchUpdateCategoryDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量修改分类</el-button>
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openBatchUpdateTechStackDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量修改技术栈</el-button>
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openBatchUpdateCurrencyDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量修改币种</el-button>
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openBatchUpdateCustomCategoryDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量修改填充空分类</el-button>
          <el-button type="info" color="#b8860b" :icon="Edit" @click="openBatchUpdateVariantsDialog"
            :disabled="selectedRows.length === 0" size="large" :dark="isDark" plain
            class="drawer-button">批量修改变体处理方式</el-button>
        </div>
      </div>
    </template>
  </el-drawer>

  <!-- 新功能组件抽屉 -->
  <el-drawer v-model="newDrawerVisible" :direction="newDirection" size="15%">
    <template #header>
      <h4>特殊服务功能</h4>
    </template>
    <template #default>
      <div class="new-drawer-content">
        <!-- 按钮容器 -->
        <div class="new-drawer-buttons-container">
          <el-button type="primary" color="#ff5959" @click="handleOneClickCollect" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking">
            一键SP采集
          </el-button>
          <el-button type="primary" color="#fc5185" @click="handleDataTdn" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            一键获取TDN
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleDataCleaning" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            1. 一键数据清洗
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleDataRepeatTitle" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            2. 一键处理重复标题
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleDataExamine" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            3. 一键体检
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleDataDeleteVariants"
            :disabled="selectedRows.length === 0" size="large" :icon="Smoking" :dark="isDark" plain>
            4. 一键删除不合格变体
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleDataCurrency" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            5. 一键转换USD
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleFilterPrice" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            6. 一键过滤价格
          </el-button>
          <el-button type="primary" color="#b8860b" @click="handleDatakeywords" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            7. 一键过滤违禁词
          </el-button>
          <el-button type="primary" color="#2cb978" @click="allHandleDataCleaning" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            一条龙清洗处理数据
          </el-button>
          <el-button type="primary" color="#fc5185" @click="handleSplit" :disabled="selectedRows.length === 0"
            size="large" :icon="Smoking" :dark="isDark" plain>
            一键拆分数据
          </el-button>
        </div>
      </div>
    </template>
  </el-drawer>

</template>

<script lang="ts" setup>
import { computed, ref, onMounted } from 'vue'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { TableInstance } from 'element-plus'
import { isDark } from '~/composables/dark'
import { Delete, Edit, Search, Share, Upload, Smoking } from '@element-plus/icons-vue'
import type { DrawerProps } from 'element-plus'


// API 地址常量
const baseUrl = 'http://127.0.0.1:5000/datasources'
const categoriesUrl = 'http://127.0.0.1:5000/categories'
const techStacksUrl = 'http://127.0.0.1:5000/tech_stacks'
const currencyUrl = 'http://127.0.0.1:5000/api/currencies'

// 数据源信息接口
interface DataSource {
  id: number;
  createdDate: string;
  url: string;
  categoryName: string;
  techStackName: string;
  currencyName: string;
  categoryId: number;
  techStackId: number;
  currencyId: number;
  name: string;
  dataVolume: string;
  status: string;
  customCategory: string;
  remark: string;
  title: string;
  describe: string;
  ntdTranslate: string;
  variants: string;
  language: string;
}

// 分类、技术栈和币种接口
interface Category { id: number; categoryName: string; }
interface TechStack { id: number; techStack: string; }
interface Currency { id: number; name: string; }

// 组件数据
const categories = ref<Category[]>([])
const techStacks = ref<TechStack[]>([])
const currencys = ref<Currency[]>([])
const tableData = ref<DataSource[]>([])
const selectedRows = ref<DataSource[]>([])
const totalRecords = ref(0)
const pageSize = ref(50) // 分页输出数量
const currentPage = ref(1)

// 筛选条件
const selectedCategory = ref<number | undefined>(undefined)  // 使用 undefined 而非 null
const selectedTechStack = ref<number | undefined>(undefined)
const selectedCurrency = ref<number | undefined>(undefined)
const selectedStatus = ref<string | undefined>(undefined)
const minDataVolume = ref<number | undefined>(undefined);
const maxDataVolume = ref<number | undefined>(undefined);

// 搜索
const search = ref('')


// 获取数据源、分类、技术栈和币种信息
const fetchData = async () => {
  try {
    const params: any = {
      page: currentPage.value,
      per_page: pageSize.value,
      search: search.value  // 添加搜索关键词参数
    };
    if (selectedCategory.value) params.category_id = selectedCategory.value;
    if (selectedTechStack.value) params.tech_stack_id = selectedTechStack.value;
    if (selectedCurrency.value) params.currency_id = selectedCurrency.value;
    if (selectedStatus.value) params.status = selectedStatus.value;
    if (minDataVolume.value !== undefined) params.min_data_volume = minDataVolume.value;
    if (maxDataVolume.value !== undefined) params.max_data_volume = maxDataVolume.value;

    const response = await axios.get(baseUrl, { params });
    tableData.value = response.data.datasources;
    totalRecords.value = response.data.total;
  } catch (error) {
    ElMessage.error('获取数据失败');
  }
};

// 获取分类信息
const fetchCategories = async () => {
  try {
    const response = await axios.get(categoriesUrl)
    categories.value = response.data
  } catch (error) {
    ElMessage.error('获取分类数据失败')
  }
}

// 获取技术栈信息
const fetchTechStacks = async () => {
  try {
    const response = await axios.get(techStacksUrl)
    techStacks.value = response.data
  } catch (error) {
    ElMessage.error('获取技术栈数据失败')
  }
}

// 获取币种信息
const fetchCurrencys = async () => {
  try {
    const response = await axios.get(currencyUrl)
    currencys.value = response.data
  } catch (error) {
    ElMessage.error('获取币种数据失败')
  }
}

// 处理表格选择项变化
const selectedDataVolumes = ref<{ [id: number]: string }>({})

const handleSelectionChange = (selection: DataSource[]) => {
  selectedRows.value = selection
  selectedDataVolumes.value = selection.reduce((acc, row) => {
    acc[row.id] = row.dataVolume
    return acc
  }, {} as { [id: number]: string })
}

// 分页操作
const handlePageChange = (page: number) => {
  currentPage.value = page
  fetchData()
}

// ----------------------
// 全选和取消全选逻辑

// 存储表格实例的引用
const multipleTableRef = ref<TableInstance>()

// 全选操作
const selectAll = () => {
  multipleTableRef.value!.toggleAllSelection()
}

// 取消选择操作
const deselectAll = () => {
  multipleTableRef.value!.clearSelection()
}

// ----------------------
// 重置搜索和筛选
const resetFilters = () => {
  selectedCategory.value = undefined;
  selectedTechStack.value = undefined;
  selectedCurrency.value = undefined;
  selectedStatus.value = undefined;
  minDataVolume.value = undefined;
  maxDataVolume.value = undefined;
  search.value = '';
  currentPage.value = 1; // 重置当前页为第一页
  fetchData(); // 重新获取数据
};

// ----------------------
// url点击变色的逻辑
const activeIndex = ref<number | null>(null); // 用于记录当前被点击的链接的索引，可以是 number 或 null

const handleLinkClick = (index: number) => {
  activeIndex.value = index; // 更新当前被点击的链接的索引
};




// ------------------
// 编辑保存数据逻辑
const dialogVisible = ref(false)

const variantOptions = ref([
  { value: '处理变体', label: '处理变体' },
  { value: '不处理变体', label: '不处理变体' },
  { value: '自动检测变体列', label: '自动检测变体列' }
]);

// 编辑数据
const editForm = ref<DataSource>({
  id: 0,
  createdDate: '',
  url: '',
  categoryName: '',
  techStackName: '',
  currencyName: '',
  categoryId: 0,
  techStackId: 0,
  currencyId: 0,
  name: '',           // 添加站名字段
  dataVolume: '',     // 添加数据量字段
  status: '',         // 添加状态字段

  customCategory: '', // 添加自定义填充空分类字段
  remark: '',         // 添加备注字段
  title: '',          // 添加标题字段
  describe: '',       // 添加描述字段
  ntdTranslate: '',   // 添加翻译字段
  variants: '',       // 添加变体处理方式字段
  language: '',       // 添加语种字段
});


// 新增数据
const handleAdd = () => {
  editForm.value = {
    id: 0,
    createdDate: '',
    url: '',
    categoryName: '',
    techStackName: '',
    currencyName: '',
    categoryId: 1,
    techStackId: 1,
    currencyId: 1,
    status: '',          // 添加状态字段
    customCategory: '',  // 添加自定义填充分类字段
    remark: '',          // 添加备注字段
    name: '',            // 添加名称字段
    dataVolume: '',      // 添加数据量字段
    title: '',           // 添加标题字段
    describe: '',        // 添加描述字段
    ntdTranslate: '',    // 添加翻译字段
    variants: '',        // 添加变体处理方式字段
    language: '',     // 添加语种字段
  }
  dialogVisible.value = true;
}

// 重置数据
const handleDialogClose = () => {
  editForm.value = {
    id: 0,
    createdDate: '',
    url: '',
    categoryName: '',
    techStackName: '',
    currencyName: '',
    categoryId: 1,
    techStackId: 1,
    currencyId: 1,
    status: '',          // 重置状态字段
    customCategory: '',  // 重置自定义填充分类字段
    remark: '',          // 重置备注字段
    name: '',            // 重置名称字段
    dataVolume: '',      // 重置数据量字段
    title: '',           // 重置标题字段
    describe: '',        // 重置描述字段
    ntdTranslate: '',    // 重置翻译字段
    variants: '',        // 重置变体处理方式字段
    language: '',        // 重置语种字段
  }
}


const handleEdit = async (index: number, row: DataSource) => {
  try {
    // 先将数据源信息赋值给编辑表单
    editForm.value = { ...row }

    // 使用数据源的 ID 请求详细信息
    const response = await axios.get(`${baseUrl}/${row.id}`)

    // 赋值返回的分类和技术栈的 ID
    editForm.value.categoryId = response.data.categoryId  // 用ID替换名称
    editForm.value.techStackId = response.data.techStackId  // 用ID替换名称
    editForm.value.currencyId = response.data.currencyId  // 用ID替换名称

    // 赋值新增的字段
    editForm.value.status = response.data.status  // 自定义状态
    editForm.value.currencyId = response.data.currencyId  // 币种
    editForm.value.customCategory = response.data.customCategory  // 自定义填充空分类
    editForm.value.remark = response.data.remark  // 备注字段

    // 显示弹窗
    dialogVisible.value = true
  } catch (error) {
    ElMessage.error('获取详细信息失败')
  }
}

const handleSave = async () => {
  try {
    if (editForm.value.id === 0) {
      await axios.post(baseUrl, {
        createdDate: new Date().toISOString().split('T')[0],
        url: editForm.value.url,
        categoryId: editForm.value.categoryId,
        techStackId: editForm.value.techStackId,
        currencyId: editForm.value.currencyId, // 添加币种字段
        name: editForm.value.name, // 添加站名字段
        dataVolume: editForm.value.dataVolume, // 添加数据量字段
        status: editForm.value.status, // 添加状态字段
        customCategory: editForm.value.customCategory, // 自定义填充空分类字段
        remark: editForm.value.remark, // 添加备注字段
        title: editForm.value.title, // 添加标题字段
        describe: editForm.value.describe, // 添加描述字段
        ntdTranslate: editForm.value.ntdTranslate, // 添加翻译字段
        variants: editForm.value.variants, // 添加变体处理方式字段
        language: editForm.value.language, // 添加语种字段
      })
      ElMessage.success('新增数据源成功')
    } else {
      await axios.put(`${baseUrl}/${editForm.value.id}`, {
        url: editForm.value.url,
        categoryId: editForm.value.categoryId,
        techStackId: editForm.value.techStackId,
        currencyId: editForm.value.currencyId, // 添加币种字段
        name: editForm.value.name, // 添加站名字段
        dataVolume: editForm.value.dataVolume, // 添加数据量字段
        status: editForm.value.status, // 添加状态字段
        customCategory: editForm.value.customCategory, // 自定义填充空分类字段
        remark: editForm.value.remark, // 添加备注字段
        title: editForm.value.title, // 添加标题字段
        describe: editForm.value.describe, // 添加描述字段
        ntdTranslate: editForm.value.ntdTranslate, // 添加翻译字段
        variants: editForm.value.variants, // 添加变体处理方式字段
        language: editForm.value.language, // 添加语种字段
      })
      ElMessage.success('更新数据源成功')
    }
    dialogVisible.value = false
    fetchData()
  } catch (error) {
    ElMessage.error('保存失败')
  }
}

// ------------------
// 删除数据逻辑
const handleDelete = async (index: number, row: DataSource) => {
  try {
    await ElMessageBox.confirm('确定要删除这条数据吗？', '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await axios.delete(`${baseUrl}/${row.id}`)
    ElMessage.success('删除成功')
    fetchData()
  } catch (error) {
    ElMessage.error('删除失败')
  }
}

// ------------------
// 批量修改分类
const batchUpdateCategoryDialogVisible = ref(false);  // 控制批量更新分类弹窗显示
const batchUpdateCategoryForm = ref({
  categoryId: '',  // 用户选择的分类ID
});

// 打开批量更新分类弹窗
const openBatchUpdateCategoryDialog = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }
  batchUpdateCategoryDialogVisible.value = true;
};

// 关闭批量更新分类弹窗
const handleBatchUpdateCategoryDialogClose = () => {
  batchUpdateCategoryDialogVisible.value = false;
};

// 执行批量更新分类
const handleBatchUpdateCategory = async () => {
  const ids = selectedRows.value.map(row => row.id);  // 获取选中的数据源ID列表
  const newCategoryId = batchUpdateCategoryForm.value.categoryId;  // 获取新的分类ID

  try {
    const response = await axios.put('http://127.0.0.1:5000/datasources/batch-update-category', {
      ids,
      categoryId: newCategoryId,
    });
    ElMessage.success(response.data.message);
    batchUpdateCategoryDialogVisible.value = false;
    // fetchData();  // 刷新数据
  } catch (error) {
    ElMessage.error('批量更新分类失败');
  }
};

// --------------------
// 批量更新技术栈
const batchUpdateTechStackDialogVisible = ref(false);  // 控制批量更新技术栈弹窗显示
const batchUpdateTechStackForm = ref({
  techStackId: '',  // 用户选择的技术栈ID
});

// 打开批量更新技术栈弹窗
const openBatchUpdateTechStackDialog = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }
  batchUpdateTechStackDialogVisible.value = true;
};

// 关闭批量更新技术栈弹窗
const handleBatchUpdateTechStackDialogClose = () => {
  batchUpdateTechStackDialogVisible.value = false;
};

// 执行批量更新技术栈
const handleBatchUpdateTechStack = async () => {
  const ids = selectedRows.value.map(row => row.id);  // 获取选中的数据源ID列表
  const newTechStackId = batchUpdateTechStackForm.value.techStackId;  // 获取新的技术栈ID

  try {
    const response = await axios.put('http://127.0.0.1:5000/datasources/batch-update-techstack', {
      ids,
      techStackId: newTechStackId,
    });
    ElMessage.success(response.data.message);
    batchUpdateTechStackDialogVisible.value = false;
    // fetchData();  // 刷新数据
  } catch (error) {
    ElMessage.error('批量更新技术栈失败');
  }
};
// --------------------
// 批量更新币种
const batchUpdateCurrencyDialogVisible = ref(false);  // 控制批量更新币种弹窗显示
const batchUpdateCurrencyForm = ref({
  currencyId: '',  // 用户选择的币种ID
});

// 打开批量更新币种弹窗
const openBatchUpdateCurrencyDialog = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }
  batchUpdateCurrencyDialogVisible.value = true;
};

// 关闭批量更新币种弹窗
const handleBatchUpdateCurrencyDialogClose = () => {
  batchUpdateCurrencyDialogVisible.value = false;
};

// 执行批量更新币种
const handleBatchUpdateCurrency = async () => {
  const ids = selectedRows.value.map(row => row.id);  // 获取选中的数据源ID列表
  const newCurrencyId = batchUpdateCurrencyForm.value.currencyId;  // 获取新的币种ID

  try {
    const response = await axios.put('http://127.0.0.1:5000/datasources/batch-update-currency', {
      ids,
      currencyId: newCurrencyId,
    });
    ElMessage.success(response.data.message);
    batchUpdateCurrencyDialogVisible.value = false;
    // fetchData();  // 刷新数据
  } catch (error) {
    ElMessage.error('批量更新币种失败');
  }
};

// --------------------
// 批量修改状态
const batchUpdateStatusDialogVisible = ref(false)
const batchUpdateStatusForm = ref({
  status: '',
})


const openBatchUpdateStatusDialog = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }
  batchUpdateStatusDialogVisible.value = true;
}

// 关闭批量更新状态的弹窗
const handleBatchUpdateStatusDialogClose = () => {
  batchUpdateStatusDialogVisible.value = false;
};


const handleBatchUpdateStatus = async () => {
  const ids = selectedRows.value.map(row => row.id)
  const newStatus = batchUpdateStatusForm.value.status
  try {
    await axios.put('http://127.0.0.1:5000/datasources/batch-update-status', {
      ids,
      status: newStatus,
    })
    ElMessage.success('批量更新状态成功')
    batchUpdateStatusDialogVisible.value = false
    // fetchData()
  } catch (error) {
    ElMessage.error('批量更新失败')
  }
}


// --------------------
// 批量更新填充分类字段
const batchUpdateCustomCategoryDialogVisible = ref(false)
const batchUpdateCustomCategoryForm = ref({
  customCategory: '',
})


const openBatchUpdateCustomCategoryDialog = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }
  batchUpdateCustomCategoryDialogVisible.value = true;
}

// 关闭批量更新自定义分类的弹窗
const handleBatchUpdateCustomCategoryDialogClose = () => {
  batchUpdateCustomCategoryDialogVisible.value = false;
};

const handleBatchUpdateCustomCategory = async () => {
  const ids = selectedRows.value.map(row => row.id)
  const newCustomCategory = batchUpdateCustomCategoryForm.value.customCategory
  try {
    await axios.put('http://127.0.0.1:5000/datasources/batch-update-custom-category', {
      ids,
      customCategory: newCustomCategory,
    })
    ElMessage.success('批量更新自定义分类成功')
    batchUpdateCustomCategoryDialogVisible.value = false
    // fetchData()
  } catch (error) {
    ElMessage.error('批量更新失败')
  }
}


// ------------------
// 批量修改变体处理方式
const batchUpdateVariantsDialogVisible = ref(false);  // 控制批量更新弹窗显示
const batchUpdateVariantsForm = ref({
  variants: '',  // 用户选择的新变体处理方式
});


// 打开批量更新弹窗
const openBatchUpdateVariantsDialog = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }
  batchUpdateVariantsDialogVisible.value = true;
};

// 关闭批量更新弹窗
const handleBatchUpdateVariantsDialogClose = () => {
  batchUpdateVariantsDialogVisible.value = false;
};

// 执行批量更新
const handleBatchUpdateVariants = async () => {
  const ids = selectedRows.value.map(row => row.id);  // 获取选中的数据源 ID 列表
  const newVariant = batchUpdateVariantsForm.value.variants;  // 获取新的变体处理方式

  try {
    const response = await axios.put('http://127.0.0.1:5000/datasources/batch-update-variants', {
      ids,
      variants: newVariant,
    });
    ElMessage.success(response.data.message);  // 提示成功信息
    batchUpdateVariantsDialogVisible.value = false;  // 关闭弹窗
    // 这里可以重新加载数据源，刷新表格数据
    // fetchData();  // 假设你有一个 fetchData 函数来刷新数据
  } catch (error) {
    ElMessage.error('批量更新失败');
  }
};



// ----------------------
// 抽屉显示状态
const drawerVisible = ref(false)
// 抽屉打开方向
const direction = ref<DrawerProps['direction']>('rtl')
// 显示抽屉的方法
const showDrawer = () => {
  drawerVisible.value = true
}

// 新抽屉显示状态
const newDrawerVisible = ref(false)
// 新抽屉打开方向
const newDirection = ref<DrawerProps['direction']>('rtl') // 你可以根据需要修改方向
// 显示新抽屉的方法
const showNewDrawer = () => {
  newDrawerVisible.value = true
}

// ------------------
// 打开文件夹功能
const openDatasourceFolder = async (id: number) => {
  try {
    const response = await axios.get(`http://127.0.0.1:5000/datasources/folder/${id}`)

    if (response.data.message === 'Folder opened successfully') {
      // 如果文件夹成功打开，显示文件列表
      ElMessage.success(`打开成功`)
    } else {
      // 如果没有文件夹打开成功，显示错误信息
      ElMessage.error(response.data.message || '打开数据目录失败')
    }
  } catch (error) {
    ElMessage.error('打开数据目录失败')
    console.error(error)
  }
}

// ------------------
// 批量打开链接的功能
const openLinksBatchDialog = async () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }

  try {
    const ids = selectedRows.value.map(row => row.id);
    const response = await axios.post('http://127.0.0.1:5000/datasources/batch-url-processing', { ids });

    if (response.status === 200) {
      ElMessage.success('任务已提交');
    } else {
      ElMessage.error('任务提交失败');
    }
  } catch (error) {
    ElMessage.error('请求失败，请检查网络连接或服务器状态');
  }
};

// 循环打开链接设置功能
const openLinksForDialog = async () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }

  try {
    const ids = selectedRows.value.map(row => row.id);
    const response = await axios.post('http://127.0.0.1:5000/datasources/for-url-processing', { ids });

    if (response.status === 200) {
      ElMessage.success('任务已提交');
    } else {
      ElMessage.error('任务提交失败');
    }
  } catch (error) {
    ElMessage.error('请求失败，请检查网络连接或服务器状态');
  }
};


// ------------------
// 一键获取标题描述站名功能
const handleDataTdn = async () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }

  try {
    const ids = selectedRows.value.map(row => row.id);
    const response = await axios.post('http://127.0.0.1:5000/datasources/data-source-tdn', { ids });

    if (response.status === 200) {
      ElMessage.success('任务已提交');
    } else {
      ElMessage.error('任务提交失败');
    }
  } catch (error) {
    ElMessage.error('请求失败，请检查网络连接或服务器状态');
  }
};


// ------------------
// 统一任务提交函数
const handleTaskSubmit = async (taskType: string, taskParams = {}) => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }

  try {
    const ids = selectedRows.value.map(row => row.id);
    const response = await axios.post('http://127.0.0.1:5050/submit_task', {
      ids,
      task_type: taskType,
      task_params: taskParams
    });

    if (response.status === 202) {
      ElMessage.success('任务已提交');
    } else {
      ElMessage.error('任务提交失败');
    }
  } catch (error) {
    ElMessage.error('请求失败，请检查网络连接或服务器状态');
  }
};

// ------------------
// 一键采集任务功能
const handleOneClickCollect = async () => {
  await handleTaskSubmit('data_collection');
};

// ------------------
// 一键数据清洗功能
const handleDataCleaning = async () => {
  await handleTaskSubmit('data_cleaning');
};

// ------------------
// 一键转换币种功能
const handleDataCurrency = async () => {
  await handleTaskSubmit('convert_to_usd');
};

// ------------------
// 一键过滤价格功能
const filterPriceDialogVisible = ref(false)  // 控制弹窗显示
const filterPriceForm = ref({
  minimumPrice: 0.01,  // 最小价格
  maximumPrice: 2000   // 最大价格
})

const handleFilterPrice = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源');
    return;
  }

  // 显示过滤价格的弹窗
  filterPriceDialogVisible.value = true;
}

const handleFilterPriceDialogClose = () => {
  filterPriceForm.value = { minimumPrice: 0.01, maximumPrice: 2000 }  // 重置表单
}

const handleSaveFilterPrice = async () => {
  const { minimumPrice, maximumPrice } = filterPriceForm.value

  // 检查价格范围是否合法
  if (minimumPrice < 0.01 || maximumPrice < 0.01) {
    ElMessage.error('价格必须大于0');
    return;
  }
  if (minimumPrice >= maximumPrice) {
    ElMessage.error('最小价格不能大于或等于最大价格');
    return;
  }

  // 提交过滤价格请求
  await handleTaskSubmit('filter_price', { price_min: minimumPrice, price_max: maximumPrice });

  // 关闭弹窗
  filterPriceDialogVisible.value = false;
}

// ------------------
// 一键过滤关键词功能
const handleDatakeywords = async () => {
  await handleTaskSubmit('filter_keywords');
};

// ------------------
// 一键体检功能
const handleDataExamine = async () => {
  await handleTaskSubmit('perform_health_check');
};

// ------------------
// 一键删除不合格变体
const handleDataDeleteVariants = async () => {
  await handleTaskSubmit('delete_invalid_variants');
};

// ------------------
// 一键处理重复标题
const handleDataRepeatTitle = async () => {
  await handleTaskSubmit('handle_duplicate_titles');
};

// ------------------
// 一条龙清洗处理数据
const allHandleDataCleaning = async () => {
  await handleTaskSubmit('data_cleaning_all');
};

// ------------------
// 一键数据拆分相关逻辑
const splitDialogVisible = ref(false)  // 控制拆分弹窗显示
const splitForm = ref({
  splitSize: 5000,  // 每份拆分的数量
  dataVolume: '',  // 数据量
})

const handleSplit = () => {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择数据源')
    return
  }

  // 获取选中的数据源ID及其dataVolume
  const selectedDataSource = selectedRows.value[0]  // 默认选择第一个选中的数据源
  splitForm.value.dataVolume = selectedDataVolumes.value[selectedDataSource.id]

  // 显示拆分弹窗
  splitDialogVisible.value = true
}

const handleSplitDialogClose = () => {
  splitForm.value = { splitSize: 5000, dataVolume: '' }  // 重置拆分数量和数据量
}

const handleSaveSplit = async () => {
  const splitSize = splitForm.value.splitSize
  if (splitSize <= 0) {
    ElMessage.error('请输入正确的拆分数量')
    return
  }

  // 创建一个包含数据源ID和拆分数量的对象
  const dataSourceInfo = selectedRows.value.map((row) => ({
    id: row.id,
    dataVolume: selectedDataVolumes.value[row.id], // 获取对应的 dataVolume
  }))

  // 打印数据源信息
  console.log("选中的数据源信息:", dataSourceInfo)
  console.log("拆分数量:", splitSize)

  // 提交拆分请求
  await handleTaskSubmit('split_data', { data_source_info: dataSourceInfo, split_size: splitSize });

  // 关闭弹窗并刷新数据
  splitDialogVisible.value = false;
}



// ------------------

</script>


<style>
.active-link {
  color: red;
  /* 设置点击后的颜色 */
}

.drawer-buttons-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.drawer-button {
  width: 100%;
  text-align: center;
}

.new-drawer-buttons-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.new-drawer-buttons-container .el-button {
  width: 100%;
  text-align: center;
}
</style>