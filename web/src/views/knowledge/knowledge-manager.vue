<script lang="ts" setup>
import { onMounted, ref } from 'vue'
import type { UploadFileInfo } from 'naive-ui'
import { useMessage } from 'naive-ui'
import { uploadKnowledgeFile, searchKnowledgeBase } from '@/api/knowledge'

const message = useMessage()

// ===== 上传 =====
const uploadLoading = ref(false)
const kbName = ref('default')

async function handleUpload({ file }: { file: UploadFileInfo }) {
  if (!file.file) {
    message.error('文件不存在')
    return
  }
  uploadLoading.value = true
  try {
    const resp = await uploadKnowledgeFile(file.file, kbName.value || undefined)
    const result = await resp.json()
    if (result.code === 200) {
      message.success(`上传成功，共 ${result.data.chunk_count} 个文本块`)
      await loadSearchHistory()
    } else {
      message.error(result.msg || '上传失败')
    }
  } catch (e: any) {
    message.error(`上传失败: ${e.message}`)
  } finally {
    uploadLoading.value = false
  }
}

// ===== 检索 =====
const searchText = ref('')
const searchKbName = ref('default')
const searchTopK = ref(6)
const searchResult = ref('')
const searchLoading = ref(false)
const searchHistory = ref<{ question: string, kbName: string, result: string, time: string }[]>([])

async function handleSearch() {
  if (!searchText.value.trim()) {
    message.warning('请输入查询内容')
    return
  }
  searchLoading.value = true
  searchResult.value = ''
  try {
    const resp = await searchKnowledgeBase({
      question: searchText.value.trim(),
      kb_name: searchKbName.value || undefined,
      top_k: searchTopK.value,
    })
    const result = await resp.json()
    if (result.code === 200) {
      searchResult.value = result.data.context || '未找到相关内容'
      searchHistory.value.unshift({
        question: searchText.value.trim(),
        kbName: searchKbName.value || 'default',
        result: searchResult.value,
        time: new Date().toLocaleString(),
      })
    } else {
      message.error(result.msg || '检索失败')
    }
  } catch (e: any) {
    message.error(`检索失败: ${e.message}`)
  } finally {
    searchLoading.value = false
  }
}

function clearHistory() {
  searchHistory.value = []
}

function loadFromHistory(item: { question: string, kbName: string }) {
  searchText.value = item.question
  searchKbName.value = item.kbName
}

function loadSearchHistory() {
  // 仅用于刷新，实际历史存储在 searchHistory 中
}

onMounted(() => {
  loadSearchHistory()
})
</script>

<template>
  <div class="p-24 h-full overflow-y-auto">
    <h1 class="text-24 font-bold mb-24">知识库管理</h1>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-24">
      <!-- === 左侧：上传 === -->
      <div class="bg-white rounded-12 p-24 shadow-sm b b-solid b-gray-200">
        <h2 class="text-18 font-semibold mb-16 flex items-center gap-8">
          <span class="i-material-symbols:upload-file text-22 text-primary" />
          上传文档
        </h2>

        <div class="mb-16">
          <label class="text-14 text-gray-600 mb-4 block">知识库名称</label>
          <n-input
            v-model:value="kbName"
            placeholder="default"
            clearable
          />
          <p class="text-12 text-gray-400 mt-4">不同知识库之间数据隔离，同名知识库会自动追加内容</p>
        </div>

        <n-upload
          accept=".docx,.ppt,.pptx,.pdf,.txt,.xlsx,.csv"
          :default-upload="false"
          :multiple="false"
          :show-file-list="true"
          :disabled="uploadLoading"
          @change="handleUpload"
        >
          <n-button
            type="primary"
            :loading="uploadLoading"
            :disabled="uploadLoading"
          >
            <template #icon>
              <span class="i-material-symbols:upload" />
            </template>
            选择文件并上传
          </n-button>
        </n-upload>

        <p class="text-12 text-gray-400 mt-12">
          支持格式: DOCX, PPT, PDF, TXT, XLSX, CSV | 上传后自动进行文本解析、分块、向量化
        </p>
      </div>

      <!-- === 右侧：检索 === -->
      <div class="bg-white rounded-12 p-24 shadow-sm b b-solid b-gray-200">
        <h2 class="text-18 font-semibold mb-16 flex items-center gap-8">
          <span class="i-material-symbols:search text-22 text-primary" />
          检索知识库
        </h2>

        <div class="flex gap-8 mb-16">
          <n-input
            v-model:value="searchKbName"
            placeholder="知识库名称 (默认 default)"
            clearable
            class="w-180"
          />
          <n-input-number
            v-model:value="searchTopK"
            :min="1"
            :max="20"
            placeholder="返回条数"
            class="w-100"
          >
            <template #prefix>Top</template>
          </n-input-number>
        </div>

        <div class="flex gap-8 mb-16">
          <n-input
            v-model:value="searchText"
            placeholder="请输入查询内容..."
            clearable
            type="textarea"
            :rows="3"
            @keydown.ctrl.enter="handleSearch"
          />
        </div>

        <div class="flex gap-8 mb-16">
          <n-button
            type="primary"
            :loading="searchLoading"
            @click="handleSearch"
          >
            <template #icon>
              <span class="i-material-symbols:search" />
            </template>
            检索
          </n-button>
          <n-button
            v-if="searchResult"
            secondary
            @click="searchResult = ''"
          >
            清空结果
          </n-button>
        </div>

        <!-- 检索结果 -->
        <div
          v-if="searchResult"
          class="bg-gray-50 rounded-8 p-16 mt-8"
        >
          <h3 class="text-14 font-semibold mb-8 text-gray-500">检索结果</h3>
          <div class="text-14 whitespace-pre-wrap leading-7">
            {{ searchResult }}
          </div>
        </div>
      </div>
    </div>

    <!-- === 检索历史 === -->
    <div
      v-if="searchHistory.length > 0"
      class="mt-24 bg-white rounded-12 p-24 shadow-sm b b-solid b-gray-200"
    >
      <div class="flex items-center justify-between mb-16">
        <h2 class="text-16 font-semibold flex items-center gap-8">
          <span class="i-material-symbols:history text-20" />
          检索历史
        </h2>
        <n-button text size="small" @click="clearHistory">
          清空历史
        </n-button>
      </div>

      <div class="space-y-8">
        <div
          v-for="(item, idx) in searchHistory"
          :key="idx"
          class="b b-solid b-gray-100 rounded-8 p-12 hover:bg-gray-50 cursor-pointer transition-colors"
          @click="loadFromHistory(item)"
        >
          <div class="flex items-center justify-between mb-4">
            <span class="text-14 font-medium text-primary">{{ item.question }}</span>
            <span class="text-12 text-gray-400">{{ item.time }}</span>
          </div>
          <div class="text-13 text-gray-500 line-clamp-2">
            {{ item.result }}
          </div>
          <div class="text-12 text-gray-400 mt-4">
            知识库: {{ item.kbName }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>