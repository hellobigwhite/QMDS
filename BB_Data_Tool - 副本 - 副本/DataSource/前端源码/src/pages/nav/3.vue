<template>
  <div style="margin-bottom: 10px;">
    <el-card>
      <div style="display: flex; justify-content: end; align-items: center; gap: 10px;">
        <el-input v-model="search" placeholder="搜索标签名称" clearable @clear="handleSearch"
          @keyup.enter.native="handleSearch" style="width: 200px;" size="small"></el-input>
        <el-button type="primary" @click="handleSearch" size="small">搜索</el-button>
        <el-button type="info" @click="handleReset" size="small">重置</el-button>
        <el-button type="info" plain @click="handleAdd" size="small">新增标签</el-button>
        <el-button type="default" plain @click="handleBatchDelete" :disabled="multipleSelection.length === 0"
          size="small">
          批量删除
        </el-button>

        <el-button type="default" plain @click="showFeatureNotAvailable" :disabled="multipleSelection.length === 0" size="small">
        批量合并
      </el-button>
      </div>
    </el-card>
  </div>

  <el-table :data="tableData" style="width: 100%" @selection-change="handleSelectionChange" ref="tableRef">
    <el-table-column type="selection" width="55" />
    <el-table-column label="ID" prop="id" />
    <el-table-column label="标签名称" prop="name" />
    <!-- 新增的列，显示关联数量 -->
    <el-table-column label="关联数量" prop="count" />
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
      <el-button type="default" plain @click="handleSelectAll" size="small">全选</el-button>
      <el-button type="default" plain @click="handleClearSelection" size="small">取消全选</el-button>
    </el-col>
  </el-row>

  <!-- 分页组件 -->
  <el-row>
    <el-col :span="24" style="display: flex; justify-content: center; margin-top: 15px;">
      <el-pagination background layout="prev, pager, next" :current-page="currentPage" :page-size="pageSize"
        :total="total" @current-change="handlePageChange" style="margin-top: 20px;"></el-pagination>
    </el-col>
  </el-row>



  <!-- 编辑组件   -->
  <el-dialog v-model="dialogVisible" title="编辑标签" @close="handleDialogClose">
    <el-form :model="editForm" ref="form" label-width="100px">
      <el-form-item label="标签名称">
        <el-input v-model="editForm.name" />
      </el-form-item>
    </el-form>
    <span slot="footer" class="dialog-footer">
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSave">保存</el-button>
    </span>
  </el-dialog>
</template>

<script lang="ts" setup>
import { ref, onMounted } from 'vue';
import axios from 'axios';
import { ElMessage, ElMessageBox } from 'element-plus';

interface Tag {
  id: number;
  name: string;
  count: number;  // 新增字段：关联数量
}

const baseUrl = 'http://127.0.0.1:8050/tags/';
const search = ref('');
const dialogVisible = ref(false);
const editForm = ref<Tag>({ id: 0, name: '', count: 0 });
const editingRowIndex = ref<number | null>(null);
const tableData = ref<Tag[]>([]);

// 分页相关变量
const currentPage = ref(1);
const pageSize = ref(15);
const total = ref(0);

// 表格引用及多选数据，明确指定 tableRef 类型为 any
const tableRef = ref<any>(null);
const multipleSelection = ref<Tag[]>([]);

// 获取数据接口，支持搜索和分页
const fetchData = async (page = 1) => {
  try {
    const response = await axios.get(baseUrl, {
      params: {
        page,
        pageSize: pageSize.value,
        search: search.value,
      },
    });
    console.log(response.data); // 添加日志输出
    tableData.value = response.data.data;
    total.value = response.data.total;
    currentPage.value = page;
  } catch (error) {
    ElMessage.error('获取数据失败');
  }
};

// 搜索功能
const handleSearch = () => {
  fetchData(1); // 搜索时重置到第一页
};

// 重置功能（清空搜索框但不影响其他状态）
const handleReset = () => {
  search.value = '';
  fetchData(1);
};

// 编辑功能
const handleEdit = (index: number, row: Tag) => {
  editingRowIndex.value = index;
  editForm.value = { ...row };
  dialogVisible.value = true;
};

// 保存数据（新增或更新）
const handleSave = async () => {
  try {
    if (editingRowIndex.value !== null) {
      await axios.put(baseUrl, {
        id: editForm.value.id,
        name: editForm.value.name,
      });
      ElMessage.success('更新成功');
    } else {
      await axios.post(baseUrl, {
        name: editForm.value.name,
      });
      ElMessage.success('新增成功');
    }
    dialogVisible.value = false;
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('保存失败');
  }
};

// 删除单个标签
const handleDelete = async (index: number, row: Tag) => {
  try {
    await ElMessageBox.confirm('确定要删除这条数据吗？', '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    });
    await axios.delete(`${baseUrl}${row.id}`); // 修改这里
    ElMessage.success('删除成功');
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('删除失败');
  }
};

// 批量删除
const handleBatchDelete = async () => {
  if (multipleSelection.value.length === 0) {
    ElMessage.warning('请选择要删除的标签');
    return;
  }
  try {
    await ElMessageBox.confirm(`确定要删除选中的${multipleSelection.value.length}条数据吗？`, '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    });
    for (const row of multipleSelection.value) {
      await axios.delete(`${baseUrl}${row.id}`); // 修改这里
    }
    ElMessage.success('删除成功');
    fetchData(currentPage.value);
  } catch (error) {
    ElMessage.error('删除失败');
  }
};

// 新增标签
const handleAdd = () => {
  editingRowIndex.value = null;
  editForm.value = { id: 0, name: '', count: 0 };
  dialogVisible.value = true;
};

// 关闭编辑弹窗
const handleDialogClose = () => {
  editForm.value = { id: 0, name: '', count: 0 };
  editingRowIndex.value = null;
};

// 分页切换
const handlePageChange = (page: number) => {
  fetchData(page);
};

// 处理多选
const handleSelectionChange = (val: Tag[]) => {
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


// 待开发功能提示
const showFeatureNotAvailable = () => {
  ElMessage.info('等有空了在说.....');
};


onMounted(() => {
  fetchData();
});
</script>

<style>
/* 根据需要添加样式 */
</style>