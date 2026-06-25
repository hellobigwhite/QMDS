<template>
  <div style="margin-bottom: 10px;">
    <el-card>
      <!-- 使用 flex-wrap 以便内容换行显示 -->
      <div style="display: flex; flex-wrap: wrap; justify-content: end; align-items: center; gap: 10px;">
        <el-button type="info" plain @click="handleAdd" size="small">添加</el-button>
        <el-button type="default" plain @click="handleBatchDelete" :disabled="multipleSelection.length === 0"
          size="small">
          批量删除
        </el-button>


        <!-- 新增标签选择按钮 -->
        <el-button type="success" @click="openTagDialog" size="small">
          选择标签
        </el-button>

        <!-- 新增筛选：语言 -->
        <el-select v-model="filterLanguage" placeholder="语言" size="small" style="width: 120px;">
          <el-option v-for="option in languageOptions" :key="option.value" :label="option.label" :value="option.value">
          </el-option>
        </el-select>

        <!-- 新增筛选：币种 -->
        <el-select v-model="filterCurrency" placeholder="币种" size="small" style="width: 120px;">
          <el-option v-for="option in currencyOptions" :key="option.value" :label="option.label" :value="option.value">
          </el-option>
        </el-select>

        <!-- 新增筛选：技术栈 -->
        <el-select v-model="filterTechStack" placeholder="技术栈" size="small" style="width: 120px;">
          <el-option v-for="option in techStackOptions" :key="option.value" :label="option.label" :value="option.value">
          </el-option>
        </el-select>

        <!-- 新增范围查询：产品量 -->
        <el-input v-model="minProductVolume" placeholder="产品量最小" clearable size="small"
          style="width: 100px;"></el-input>
        <el-input v-model="maxProductVolume" placeholder="产品量最大" clearable size="small"
          style="width: 100px;"></el-input>

        <!-- 新增筛选：状态 -->
        <el-select v-model="filterStatus" placeholder="状态" size="small" style="width: 120px;">
          <el-option v-for="option in statusOptions" :key="option.value" :label="option.label" :value="option.value">
          </el-option>
        </el-select>


        <!-- 原搜索输入框 -->
        <el-input v-model="search" placeholder="搜索URL/站点名称/站..." clearable @clear="handleSearch"
          @keyup.enter.native="handleSearch" style="width: 150px;" size="small">
        </el-input>

        <el-button type="info" @click="handleSearch" size="small">筛选</el-button>
        <el-button type="info" plain @click="handleReset" size="small">重置</el-button>
        <el-button type="info" @click="openBatchEditDialog" size="small">批量修改</el-button>

        <el-dropdown>
          <span>
            <el-button type="default" plain size="small" :disabled="multipleSelection.length === 0">
              导出
            </el-button>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="handleExportTXT" :disabled="multipleSelection.length === 0">
                导出TXT
              </el-dropdown-item>
              <el-dropdown-item @click="handleExportXLSX" :disabled="multipleSelection.length === 0">
                导出EXCEL
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

        <el-dropdown>
          <span>
            <el-button type="default" size="small">AUTO</el-button>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="handleRunBackgroundTask('批量打开数据目录')">
                批量打开数据目录
              </el-dropdown-item>
              <el-dropdown-item divided @click="handleRunBackgroundTask('AI总结')">AI总结</el-dropdown-item>
              <el-dropdown-item divided @click="handleRunBackgroundTask('AI标签')">AI标签</el-dropdown-item>
              <el-dropdown-item divided @click="handleRunBackgroundTask('一键SP采集')">一键SP采集</el-dropdown-item>
              <el-dropdown-item @click="handleRunBackgroundTask('一键SP数据清洗')">一键SP数据清洗</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>

      </div>
    </el-card>
  </div>



  <el-table :data="tableData" style="width: 100%" @selection-change="handleSelectionChange" ref="tableRef">
    <!-- 多选框 -->
    <el-table-column type="selection" width="55" />

    <!-- 基本信息列 -->
    <el-table-column label="ID" prop="id" width="50" />
    <el-table-column label="URL" prop="url" width="260">
      <template #default="scope">
        <el-link :href="scope.row.url" type="primary" target="_blank"
          :class="{ 'active-link': scope.$index === activeIndex }" @click="handleLinkClick(scope.$index)">
          {{ scope.row.url }}
        </el-link>
      </template>
    </el-table-column>
    <el-table-column label="站点名称" prop="site_name" show-overflow-tooltip />
    <el-table-column label="站点标题" prop="site_title" show-overflow-tooltip />

    <!-- 补充字段 -->
    <el-table-column label="站点描述" prop="site_describe" show-overflow-tooltip />
    <el-table-column label="站点语言" width="110" prop="site_language" show-overflow-tooltip sortable />
    <el-table-column label="站点币种" width="110" prop="site_currency" show-overflow-tooltip sortable />
    <el-table-column label="技术栈" width="110" prop="site_techstack" show-overflow-tooltip sortable>
      <template #default="scope">
        <div v-if="scope.row.site_techstack" class="flex gap-2">
          <el-tag v-for="(tech, index) in scope.row.site_techstack.split(',')" :key="index" type="primary"
            effect="light">
            {{ tech.trim() }}
          </el-tag>
        </div>
      </template>
    </el-table-column>

    <el-table-column label="数量" prop="product_volume" show-overflow-tooltip sortable />
    <!-- <el-table-column label="数据量" prop="data_volume" show-overflow-tooltip sortable /> -->


    <el-table-column label="分类" prop="custom_field_1" />
    <!-- <el-table-column label="自定义字段2" prop="custom_field_2" /> -->

    <el-table-column label="AI总结" prop="ai_analysis_summary" show-overflow-tooltip />
    <!-- 标签列 -->
    <el-table-column label="产品标签" width="200" show-overflow-tooltip sortable>
      <template #default="scope">
        <div v-if="scope.row.tags" class="flex gap-2">
          <el-tag v-for="(tag, index) in scope.row.tags" :key="index" :type="tag.type || 'info'"
            :effect="tag.effect || 'light'">
            {{ tag.name }}
          </el-tag>
        </div>
      </template>
    </el-table-column>

    <el-table-column label="数据分析" prop="analysis_result" show-overflow-tooltip />

    <!-- 状态与创建日期 -->
    <el-table-column label="状态" width="100" prop="status" show-overflow-tooltip sortable>
      <template #default="scope">
        <div v-if="scope.row.status" class="flex gap-2">
          <el-tag :type="(statusOptions.find(item => item.value === scope.row.status) as any)?.type || 'primary'"
            effect="light">
            {{(statusOptions.find(item => item.value === scope.row.status) || {}).label || scope.row.status}}
          </el-tag>
        </div>
      </template>
    </el-table-column>






    <el-table-column label="创建日期" prop="created_date" width="110" show-overflow-tooltip sortable>
      <template #default="scope">
        {{ formatDate(scope.row.created_date) }}
      </template>
    </el-table-column>

    <el-table-column label="备注" prop="remark" show-overflow-tooltip />

    <!-- 操作列 -->
    <el-table-column label="操作" align="right" width="150">
      <template #default="scope">
        <el-button size="small" @click="handleEdit(scope.$index, scope.row)">编辑</el-button>
        <el-button size="small" @click="handleDelete(scope.$index, scope.row)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>

  <!-- 分页组件 -->
  <el-row>
    <el-col :span="24" style="display: flex; justify-content: center; margin-top: 15px;">
      <div class="grid-content ep-bg-purple-dark">
        <el-button type="default" plain @click="handleSelectAll" size="small">全选</el-button>
        <el-button type="default" plain @click="handleClearSelection" size="small">取消全选</el-button>
      </div>
    </el-col>
  </el-row>

  <el-row>
    <el-col :span="24" style="display: flex; justify-content: center; margin-top: 10px;">
      <div class="grid-content ep-bg-purple-dark">
        <el-pagination v-show="paginationVisible" background layout="prev, pager, next" :current-page="currentPage"
          :page-size="pageSize" :total="total" @current-change="handlePageChange"
          style="margin-top: 20px;"></el-pagination>
      </div>
    </el-col>
  </el-row>



  <!-- 编辑/新增弹窗 -->
  <el-dialog v-model="dialogVisible" title="编辑数据源" @close="handleDialogClose">
    <el-form :model="editForm" ref="form" label-width="120px">
      <el-row :gutter="20">
        <el-col :span="12">
          <el-form-item label="URL">
            <el-input v-model="editForm.url" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="站点名称">
            <el-input v-model="editForm.site_name" />
          </el-form-item>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <el-col :span="12">
          <el-form-item label="站点标题">
            <el-input v-model="editForm.site_title" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="站点描述">
            <el-input v-model="editForm.site_describe" />
          </el-form-item>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <!-- 站点语言 -->
        <el-col :span="12">
          <el-form-item label="站点语言">
            <el-select v-model="editForm.site_language" placeholder="请选择语言">
              <el-option v-for="option in languageOptions" :key="option.value" :label="option.label"
                :value="option.value">
              </el-option>
            </el-select>
          </el-form-item>
        </el-col>
        <!-- 站点币种 -->
        <el-col :span="12">
          <el-form-item label="站点币种">
            <el-select v-model="editForm.site_currency" placeholder="请选择币种">
              <el-option v-for="option in currencyOptions" :key="option.value" :label="option.label"
                :value="option.value">
              </el-option>
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <!-- 技术栈 -->
        <el-col :span="12">
          <el-form-item label="技术栈">
            <el-select v-model="editForm.site_techstack" placeholder="请选择技术栈">
              <el-option v-for="option in techStackOptions" :key="option.value" :label="option.label"
                :value="option.value">
              </el-option>
            </el-select>
          </el-form-item>
        </el-col>
        <!-- 状态 -->
        <el-col :span="12">
          <el-form-item label="状态">
            <el-select v-model="editForm.status" placeholder="请选择状态">
              <el-option v-for="option in statusOptions" :key="option.value" :label="option.label"
                :value="option.value">
              </el-option>
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <el-col :span="24">
          <el-form-item label="AI总结">
            <el-input type="textarea" v-model="editForm.ai_analysis_summary" placeholder="请输入AI总结" />
          </el-form-item>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <el-col :span="12">
          <el-form-item label="产品数量">
            <el-input-number v-model="editForm.product_volume" :min="0" placeholder="请输入产品数量" style="width: 100%;" />
          </el-form-item>
        </el-col>
        <!-- <el-col :span="12">
          <el-form-item label="数据量">
            <el-input-number v-model="editForm.data_volume" :min="0" placeholder="请输入数据量" style="width: 100%;" />
          </el-form-item>
        </el-col> -->
      </el-row>

      <el-row :gutter="20">
        <el-col :span="24">
          <el-form-item label="数据分析">
            <el-input type="textarea" v-model="editForm.analysis_result" placeholder="请输入数据分析" />
          </el-form-item>
        </el-col>
      </el-row>

      <el-row :gutter="20">
        <el-col :span="12">
          <el-form-item label="备注">
            <el-input v-model="editForm.remark" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="标签">
            <el-input v-model="editForm.tags" placeholder="多个标签请用,分割" />
          </el-form-item>
        </el-col>
      </el-row>

      <!-- 新增自定义字段 -->
      <el-row :gutter="20">
        <el-col :span="12">
          <el-form-item label="自定义分类">
            <el-select v-model="editForm.custom_field_1" placeholder="请选择分类">
              <el-option v-for="option in categoryOptions" :key="option.value" :label="option.label"
                :value="option.value">
              </el-option>
            </el-select>
          </el-form-item>
        </el-col>
        <!-- <el-col :span="12">
          <el-form-item label="自定义字段2">
            <el-input v-model="editForm.custom_field_2" placeholder="请输入自定义字段2" />
          </el-form-item>
        </el-col> -->
      </el-row>

    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSave">保存</el-button>
    </span>
  </el-dialog>


  <!-- 批量编辑弹窗 -->
  <el-dialog v-model="batchDialogVisible" title="批量修改数据源">
    <el-form :model="batchEditForm" label-width="120px">
      <!-- 更新状态 -->
      <el-form-item label="状态">
        <el-select v-model="batchEditForm.status" placeholder="请选择状态">
          <el-option v-for="option in statusOptions" :key="option.value" :label="option.label"
            :value="option.value"></el-option>
        </el-select>
      </el-form-item>
      <!-- 更新站点语言 -->
      <el-form-item label="站点语言">
        <el-select v-model="batchEditForm.site_language" placeholder="请选择站点语言">
          <el-option v-for="option in languageOptions" :key="option.value" :label="option.label"
            :value="option.value"></el-option>
        </el-select>
      </el-form-item>
      <!-- 更新站点币种 -->
      <el-form-item label="站点币种">
        <el-select v-model="batchEditForm.site_currency" placeholder="请选择站点币种">
          <el-option v-for="option in currencyOptions" :key="option.value" :label="option.label"
            :value="option.value"></el-option>
        </el-select>
      </el-form-item>
      <!-- 更新技术栈 -->
      <el-form-item label="技术栈">
        <el-select v-model="batchEditForm.site_techstack" placeholder="请选择技术栈">
          <el-option v-for="option in techStackOptions" :key="option.value" :label="option.label"
            :value="option.value"></el-option>
        </el-select>
      </el-form-item>
      <!-- 更新备注 -->
      <el-form-item label="备注">
        <el-input v-model="batchEditForm.remark" />
      </el-form-item>
      <!-- 更新标签 -->
      <el-form-item label="标签">
        <el-input v-model="batchEditForm.tags" placeholder="多个标签请用,分割" />
      </el-form-item>
      <!-- 修改自定义字段：分类 改为下拉框 -->
      <el-form-item label="分类">
        <el-select v-model="batchEditForm.custom_field_1" placeholder="请选择分类">
          <el-option v-for="option in categoryOptions" :key="option.value" :label="option.label"
            :value="option.value"></el-option>
        </el-select>
      </el-form-item>
      <!-- <el-form-item label="自定义字段2">
      <el-input v-model="batchEditForm.custom_field_2" placeholder="请输入自定义字段2" />
    </el-form-item> -->
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="batchDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleBatchEditSave">保存</el-button>
    </span>
  </el-dialog>



  <!-- 标签云展示区域 -->
  <el-dialog v-model="tagDialogVisible" title="选择标签" width="1000px">
    <div style="display: flex; flex-wrap: wrap; gap: 10px;">
      <el-tag v-for="tag in tagList" :key="tag.id" :type="filterTags.includes(tag.id) ? 'success' : 'info'"
        style="cursor: pointer;" @click="toggleTagSelection(tag.id)">
        {{ tag.name }} ({{ tag.count }})
      </el-tag>
    </div>
    <!-- 分页组件 -->
    <div style="text-align: center; margin-top: 20px;">
      <el-pagination background layout="prev, pager, next" :current-page="tagPage" :page-size="tagPageSize"
        :total="tagTotal" @current-change="handleTagPageChange">
      </el-pagination>
    </div>
    <!-- 弹窗底部操作按钮 -->
    <span slot="footer" class="dialog-footer">
      <el-button @click="tagDialogVisible = false">取消</el-button>
      <el-button type="primary" @click="confirmTagSelection">确定</el-button>
    </span>
  </el-dialog>




</template>


<script lang="ts" setup>
import { ref, onMounted } from 'vue';
import axios from 'axios';
import { ElMessage, ElMessageBox } from 'element-plus';
import * as XLSX from 'xlsx';

// 定义分类下拉选项数据
const categoryOptions = [
  { label: "五金", value: "五金" },
  { label: "交通工具", value: "交通工具" },
  { label: "体育用品", value: "体育用品" },
  { label: "保健", value: "保健" },
  { label: "办公用品", value: "办公用品" },
  { label: "动物", value: "动物" },
  { label: "商业", value: "商业" },
  { label: "婴幼儿用品", value: "婴幼儿用品" },
  { label: "媒体", value: "媒体" },
  { label: "宗教", value: "宗教" },
  { label: "家具", value: "家具" },
  { label: "家居与园艺", value: "家居与园艺" },
  { label: "成人", value: "成人" },
  { label: "服饰与配饰", value: "服饰与配饰" },
  { label: "玩具", value: "玩具" },
  { label: "电子产品", value: "电子产品" },
  { label: "相机与光学器件", value: "相机与光学器件" },
  { label: "箱包", value: "箱包" },
  { label: "艺术与娱乐", value: "艺术与娱乐" },
  { label: "软件", value: "软件" },
  { label: "饮食", value: "饮食" },
];

// 定义下拉选项数据
const languageOptions = [
  { label: '中文', value: 'zh' },
  { label: '英文', value: 'en' },
  { label: '法语', value: 'fr' },
  { label: '西班牙语', value: 'es' },
  { label: '德语', value: 'de' },
  { label: '俄语', value: 'ru' },
  { label: '日语', value: 'ja' },
  { label: '韩语', value: 'ko' },
  { label: '意大利语', value: 'it' },
  { label: '葡萄牙语', value: 'pt' },
  { label: '阿拉伯语', value: 'ar' },
  { label: '荷兰语', value: 'nl' },
  { label: '希腊语', value: 'el' },
  { label: '瑞典语', value: 'sv' },
  { label: '泰语', value: 'th' },
  { label: '土耳其语', value: 'tr' },
  { label: '越南语', value: 'vi' },
  { label: '印地语', value: 'hi' },
  { label: '印尼语', value: 'id' },
];


const currencyOptions = [
  { label: '人民币', value: 'CNY' },
  { label: '美元', value: 'USD' },
  { label: '欧元', value: 'EUR' },
  { label: '日元', value: 'JPY' },
  { label: '英镑', value: 'GBP' },
  { label: '澳大利亚元', value: 'AUD' },
  { label: '加拿大元', value: 'CAD' },
  { label: '瑞士法郎', value: 'CHF' },
  { label: '港元', value: 'HKD' },
  { label: '新西兰元', value: 'NZD' },
  { label: '瑞典克朗', value: 'SEK' },
  { label: '韩元', value: 'KRW' },
  { label: '新加坡元', value: 'SGD' },
  { label: '挪威克朗', value: 'NOK' },
  { label: '印度卢比', value: 'INR' },
  { label: '俄罗斯卢布', value: 'RUB' },
  { label: '巴西雷亚尔', value: 'BRL' },
  { label: '南非兰特', value: 'ZAR' },
  { label: '土耳其里拉', value: 'TRY' },
];


const techStackOptions = [
  { label: 'Shopify', value: 'Shopify' },
  { label: 'WordPress', value: 'WordPress' },
  { label: 'Magento', value: 'Magento' },
  { label: 'WooCommerce', value: 'WooCommerce' },
  { label: 'OpenCart', value: 'OpenCart' },
  { label: 'PrestaShop', value: 'PrestaShop' },
  { label: 'BigCommerce', value: 'BigCommerce' },
  { label: 'Salesforce Commerce Cloud', value: 'Salesforce Commerce Cloud' },
  { label: 'SAP Commerce Cloud', value: 'SAP Commerce Cloud' },
  { label: 'Oracle Commerce', value: 'Oracle Commerce' },
  { label: '平台', value: '平台' },
  { label: '其他', value: '其他' },
];


const statusOptions = [
  { label: '新增', value: '新增' },
  { label: '预选', value: '预选' },
  { label: '异常', value: '异常' },
  { label: '已采集', value: '已采集' },
  { label: '已清洗', value: '已清洗' },
  { label: '已处理', value: '已处理' },
  { label: '已分析', value: '已分析' },
  { label: '已上传', value: '已上传' },
  { label: '已使用', value: '已使用' },
];



// 定义标签接口，用于显示列表中的标签信息
interface Tag {
  id: number;
  name: string;
}

// 修改 DataSource 接口，新增 tags 字段（后端返回时为标签数组，编辑时转换为逗号分割的字符串）
interface DataSource {
  id: number;
  url: string;
  site_name: string;
  site_title: string;
  site_describe?: string;
  site_language?: string;
  site_currency?: string;
  site_techstack?: string;
  ai_analysis_summary?: string;
  product_volume?: number;
  data_volume?: number;
  analysis_result?: string;
  remark?: string;
  status?: string;
  custom_field_1?: string;
  custom_field_2?: string;
  created_date?: string;
  tags?: Tag[]; // 后端返回的是标签对象数组
}

// 为编辑表单定义一个类型，此处 tags 为字符串（逗号分割）
interface DataSourceEdit {
  id: number;
  url: string;
  site_name: string;
  site_title: string;
  site_describe?: string;
  site_language?: string;
  site_currency?: string;
  site_techstack?: string;
  status?: string;
  remark?: string;
  tags: string; // 编辑时以逗号分割的标签字符串
  ai_analysis_summary?: string; // AI总结
  product_volume?: number;      // 产品量
  data_volume?: number;         // 数据量
  analysis_result?: string;     // 数据分析
  custom_field_1?: string;
  custom_field_2?: string;
}

const baseUrl = 'http://127.0.0.1:8050/data_sources/';
const search = ref('');
const dialogVisible = ref(false);
// 初始编辑表单，tags 字段设为空字符串
const editForm = ref<DataSourceEdit>({
  id: 0,
  url: '',
  site_name: '',
  site_title: '',
  site_describe: '',
  site_language: '',
  site_currency: '',
  site_techstack: '',
  status: '',
  remark: '',
  tags: '',
  ai_analysis_summary: '',
  product_volume: 0,      // 添加默认值
  data_volume: 0,         // 添加默认值
  analysis_result: '',
  custom_field_1: '',
  custom_field_2: '',
});
const editingRowIndex = ref<number | null>(null);
const tableData = ref<DataSource[]>([]);

// 分页相关变量
const currentPage = ref(1);
const pageSize = ref(15);
const total = ref(0);
const paginationVisible = ref(false); // 后端若返回总数，可设为 true

// 表格引用及多选数据
const tableRef = ref<any>(null);
const multipleSelection = ref<DataSource[]>([]);

// 新增筛选相关的响应式变量
const filterLanguage = ref('');
const filterCurrency = ref('');
const filterTechStack = ref('');
const minProductVolume = ref('');
const maxProductVolume = ref('');
const minDataVolume = ref('');
const maxDataVolume = ref('');
const filterStatus = ref('');


// 格式化日期函数
const formatDate = (dateStr: string) => {
  const date = new Date(dateStr);
  return date.toLocaleString();
};

// 获取数据接口（增加筛选参数）
const fetchData = async (page = 1) => {
  try {
    const skip = (page - 1) * pageSize.value;

    // 构建查询参数
    const params: any = {
      skip,
      limit: pageSize.value,
      search: search.value,
      language: filterLanguage.value,
      currency: filterCurrency.value,
      techstack: filterTechStack.value,
      ...(minProductVolume.value !== '' && { product_volume_min: minProductVolume.value }),
      ...(maxProductVolume.value !== '' && { product_volume_max: maxProductVolume.value }),
      ...(minDataVolume.value !== '' && { data_volume_min: minDataVolume.value }),
      ...(maxDataVolume.value !== '' && { data_volume_max: maxDataVolume.value }),
      status: filterStatus.value,
    };

    // tags 原来的标签查询方式有问题  然后改成 在main.ts里 配置 Axios 的 params 序列化方式查询参数格式  
    if (filterTags.value.length > 0) {
      params['tags'] = filterTags.value; // 这里将 tags 数组转为 'tags=35&tags=38'
    }

    const response = await axios.get(baseUrl, { params });

    // 更新表格数据
    tableData.value = response.data.items;
    total.value = response.data.total;
    paginationVisible.value = true;
    currentPage.value = page;
  } catch (error) {
    ElMessage.error('获取数据失败');
  }
};




// 搜索功能
const handleSearch = () => {
  fetchData(1);
};

// 重置功能，重置所有搜索和筛选条件
// const handleReset = () => {
//   search.value = '';
//   filterLanguage.value = '';
//   filterCurrency.value = '';
//   filterTechStack.value = '';
//   minProductVolume.value = '';
//   maxProductVolume.value = '';
//   minDataVolume.value = '';
//   maxDataVolume.value = '';
//   filterStatus.value = '';
//   fetchData(1);
// };
const handleReset = () => {
  search.value = '';
  filterLanguage.value = '';
  filterCurrency.value = '';
  filterTechStack.value = '';
  minProductVolume.value = '';
  maxProductVolume.value = '';
  minDataVolume.value = '';
  maxDataVolume.value = '';
  filterStatus.value = '';
  filterTags.value = [];  // 重置标签筛选条件
  fetchData(1);
};

// 编辑功能：将标签数组转换为逗号分割字符串
const handleEdit = (index: number, row: DataSource) => {
  editingRowIndex.value = index;
  editForm.value = {
    id: row.id,
    url: row.url,
    site_name: row.site_name,
    site_title: row.site_title,
    site_describe: row.site_describe,
    site_language: row.site_language,
    site_currency: row.site_currency,
    site_techstack: row.site_techstack,
    status: row.status,
    remark: row.remark,
    tags: row.tags ? row.tags.map(tag => tag.name).join(', ') : '',
    ai_analysis_summary: row.ai_analysis_summary,
    product_volume: row.product_volume,
    data_volume: row.data_volume,
    analysis_result: row.analysis_result,
    custom_field_1: row.custom_field_1,
    custom_field_2: row.custom_field_2,
  };
  dialogVisible.value = true;
};

// ------------------------
// 批量编辑
// ------------------------

// 定义批量编辑表单的接口，添加索引签名以允许动态属性访问
interface BatchEditForm {
  [key: string]: string;
  status: string;
  remark: string;
  tags: string;
  site_language: string;
  site_currency: string;
  site_techstack: string;
  custom_field_1: string;
  custom_field_2: string;
  // 如需更多字段，请在此添加
}

// 批量编辑弹窗显示状态
const batchDialogVisible = ref(false);

// 批量编辑的表单数据，初始值全部为空字符串
const batchEditForm = ref<BatchEditForm>({
  status: '',
  remark: '',
  tags: '',
  site_language: '',
  site_currency: '',
  site_techstack: '',
  custom_field_1: '',
  custom_field_2: '',
});

// 打开批量编辑弹窗方法
const openBatchEditDialog = () => {
  if (multipleSelection.value.length === 0) {
    ElMessage.warning('请选择要修改的数据源');
    return;
  }
  // 重置批量编辑表单数据
  batchEditForm.value = {
    status: '',
    remark: '',
    tags: '',
    site_language: '',
    site_currency: '',
    site_techstack: '',
    custom_field_1: '',
    custom_field_2: '',
  };
  batchDialogVisible.value = true;
};

// 批量保存更新方法
const handleBatchEditSave = async () => {
  const ids = multipleSelection.value.map(item => item.id);
  // 构造 updateData 对象，只传入非空字段，类型为 Partial<BatchEditForm>
  const updateData: Partial<BatchEditForm> = {};
  Object.keys(batchEditForm.value).forEach(key => {
    const typedKey = key as keyof BatchEditForm;
    if (batchEditForm.value[typedKey] !== '') {
      updateData[typedKey] = batchEditForm.value[typedKey];
    }
  });
  try {
    await axios.put(baseUrl + 'batch_update/', {
      ids,
      update_data: updateData
    });
    ElMessage.success('批量修改成功');
    batchDialogVisible.value = false;
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('批量修改失败');
  }
};




// 保存数据（新增或更新），提交时包含 tags 字段
const handleSave = async () => {
  try {
    const payload = {
      url: editForm.value.url,
      site_name: editForm.value.site_name,
      site_title: editForm.value.site_title,
      site_describe: editForm.value.site_describe,
      site_language: editForm.value.site_language,
      site_currency: editForm.value.site_currency,
      site_techstack: editForm.value.site_techstack,
      status: editForm.value.status,
      remark: editForm.value.remark,
      tags: editForm.value.tags, // 传递标签字符串
      ai_analysis_summary: editForm.value.ai_analysis_summary,
      product_volume: editForm.value.product_volume,
      data_volume: editForm.value.data_volume,
      analysis_result: editForm.value.analysis_result,
      custom_field_1: editForm.value.custom_field_1,
      custom_field_2: editForm.value.custom_field_2,
    };

    if (editingRowIndex.value !== null) {
      // 更新数据源
      await axios.put(baseUrl, { id: editForm.value.id, ...payload });
      ElMessage.success('更新成功');
    } else {
      // 新增数据源
      await axios.post(baseUrl, payload);
      ElMessage.success('新增成功');
    }
    dialogVisible.value = false;
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('保存失败');
  }
};


// 删除单个数据源
const handleDelete = async (index: number, row: DataSource) => {
  try {
    await ElMessageBox.confirm('确定要删除这条数据吗？', '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    });
    await axios.delete(`${baseUrl}${row.id}`);
    ElMessage.success('删除成功');
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('删除失败');
  }
};

// 批量删除
const handleBatchDelete = async () => {
  if (multipleSelection.value.length === 0) {
    ElMessage.warning('请选择要删除的数据源');
    return;
  }
  try {
    await ElMessageBox.confirm(`确定要删除选中的${multipleSelection.value.length}条数据吗？`, '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    });
    for (const row of multipleSelection.value) {
      await axios.delete(`${baseUrl}${row.id}`);
    }
    ElMessage.success('删除成功');
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('删除失败');
  }
};

// 新增数据源
const handleAdd = () => {
  editingRowIndex.value = null;
  editForm.value = {
    id: 0,
    url: '',
    site_name: '',
    site_title: '',
    site_describe: '',
    site_language: '',
    site_currency: '',
    site_techstack: '',
    status: '',
    remark: '',
    tags: '', // 默认空字符串
    custom_field_1: '',
    custom_field_2: '',
  };
  dialogVisible.value = true;
};

// 关闭编辑弹窗
const handleDialogClose = () => {
  editForm.value = {
    id: 0,
    url: '',
    site_name: '',
    site_title: '',
    site_describe: '',
    site_language: '',
    site_currency: '',
    site_techstack: '',
    status: '',
    remark: '',
    tags: '',
    custom_field_1: '',
    custom_field_2: '',
  };
  editingRowIndex.value = null;
};



// 分页切换
const handlePageChange = (page: number) => {
  fetchData(page);
};

// 处理多选
const handleSelectionChange = (val: DataSource[]) => {
  multipleSelection.value = val;
};

// 全选当前页
const handleSelectAll = () => {
  tableData.value.forEach(row => tableRef.value?.toggleRowSelection(row, true));
};

// 取消全选
const handleClearSelection = () => {
  tableRef.value?.clearSelection();
};


// ------------------------
// 标签筛选数据部分
// ------------------------
const tagDialogVisible = ref(false);
const tagPage = ref(1);
const tagPageSize = ref(100);  //
const tagTotal = ref(0);
const tagList = ref<any[]>([]);  // 当前页展示的标签数据
const filterTags = ref<number[]>([]);

const openTagDialog = () => {
  tagDialogVisible.value = true;
  fetchTagPage(1);
};


const fetchTagPage = async (page: number) => {
  try {
    tagPage.value = page;
    const response = await axios.get("http://127.0.0.1:8050/tags/", {
      params: {
        page: tagPage.value,
        pageSize: tagPageSize.value,
      },
    });
    tagList.value = response.data.data;
    tagTotal.value = response.data.total;
  } catch (error) {
    ElMessage.error("获取标签失败");
  }
};


const handleTagPageChange = (page: number) => {
  fetchTagPage(page);
};


const toggleTagSelection = (tagId: number) => {
  if (filterTags.value.includes(tagId)) {
    filterTags.value = filterTags.value.filter((id: number) => id !== tagId);
  } else {
    filterTags.value.push(tagId);
  }
};


const confirmTagSelection = () => {
  tagDialogVisible.value = false;
  console.log('选中的标签:', filterTags.value);
  fetchData(1);
};



// url点击变色的逻辑
const activeIndex = ref<number | null>(null); // 用于记录当前被点击的链接的索引，可以是 number 或 null

const handleLinkClick = (index: number) => {
  activeIndex.value = index; // 更新当前被点击的链接的索引
};


// 导出选择的链接TXT
const handleExportTXT = () => {
  // 获取当前勾选的行数据
  const selectedRows = multipleSelection.value;

  if (selectedRows.length === 0) {
    ElMessage.warning('请选择要导出的数据');
    return;
  }

  // 提取 URL 列表
  const urls = selectedRows.map(row => row.url);

  // 创建一个 Blob 对象，类型为 text/plain
  const blob = new Blob([urls.join('\n')], { type: 'text/plain' });

  // 创建一个临时的 URL 来下载文件
  const url = window.URL.createObjectURL(blob);

  // 创建一个<a>标签来触发下载
  const a = document.createElement('a');
  a.href = url;
  a.download = 'exported_urls.txt'; // 设置下载的文件名
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  // 释放 URL 对象
  window.URL.revokeObjectURL(url);

  ElMessage.success('导出成功');
};

// 导出选择的链接excel
const handleExportXLSX = () => {
  // 获取当前勾选的行数据
  const selectedRows = multipleSelection.value;

  if (selectedRows.length === 0) {
    ElMessage.warning('请选择要导出的数据');
    return;
  }

  // 将选中的数据转换为适合导出格式的数据
  const exportData = selectedRows.map((row) => ({
    id: row.id,
    url: row.url,
    site_name: row.site_name,
    site_title: row.site_title,
    site_describe: row.site_describe,
    site_language: row.site_language,
    site_currency: row.site_currency,
    site_techstack: row.site_techstack,
    ai_analysis_summary: row.ai_analysis_summary,
    product_volume: row.product_volume,
    data_volume: row.data_volume,
    analysis_result: row.analysis_result,
    remark: row.remark,
    status: row.status,
    // custom_field_1: row.custom_field_1,
    // custom_field_2: row.custom_field_2,
    // created_date: row.created_date,
    // 将标签数组转换为字符串（假设标签对象中有 name 字段）
    tags: row.tags && row.tags.length ? row.tags.map(tag => tag.name).join(',') : '',
  }));

  // 使用 SheetJS 将 JSON 数据转换为工作表
  const worksheet = XLSX.utils.json_to_sheet(exportData);
  // 创建一个新的工作簿
  const workbook = XLSX.utils.book_new();
  // 将工作表添加到工作簿中
  XLSX.utils.book_append_sheet(workbook, worksheet, '数据');

  // 导出工作簿为 xlsx 文件，文件名为 exported_data.xlsx
  XLSX.writeFile(workbook, 'exported_data.xlsx');

  ElMessage.success('导出成功');
};


// 待开发功能提示
const showFeatureNotAvailable = () => {
  ElMessage.info('等有空了在说.....');
};


// 定义处理AUTO下拉菜单点击的函数
function handleAutoAction(action: string) {
  // 根据不同的action执行对应的业务逻辑
  console.log(`执行了 ${action} 操作`)
  // 在这里添加相应的处理代码
}



// 新增：提交后台耗时任务的方法
const handleRunBackgroundTask = async (taskName: string) => {
  // 判断是否有数据被勾选
  if (multipleSelection.value.length === 0) {
    ElMessage.warning('请先选择数据源以提交后台任务');
    return;
  }

  // 弹出确认框，避免误操作
  try {
    await ElMessageBox.confirm(
      `确定要执行 "${taskName}" 任务吗？`,
      '确认操作',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning',
      }
    );

    // 获取选中数据的 id 列表
    const selectedIds = multipleSelection.value.map(item => item.id);

    // 发送请求到后台
    const response = await axios.post('http://127.0.0.1:8050/background_task/', {
      task_name: taskName,
      ids: selectedIds
    });

    ElMessage.success(response.data.message || '后台任务已提交');
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('提交后台任务失败');
      console.error('后台任务请求错误:', error);
    } else {
      ElMessage.info('操作已取消');
    }
  }
};




onMounted(() => {
  fetchTagPage(1);
  fetchData();
});

</script>


<style>
/* 根据需要添加样式 */
.active-link {
  color: rgb(217, 238, 33);
  /* 设置点击后的颜色 */
}

.el-dropdown-link {
  cursor: pointer;
  color: var(--el-color-primary);
  display: flex;
  align-items: center;
}
</style>
