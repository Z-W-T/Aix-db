<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { UploadFileInfo } from 'naive-ui'
import { useMessage } from 'naive-ui'
import { listKnowledgeBase, searchKnowledgeBase, uploadKnowledgeFile } from '@/api/knowledge'

const message = useMessage()
const router = useRouter()

function handleBack() {
  // 优先返回上一页，无历史时回退到聊天页
  if (window.history.length > 1) {
    router.back()
  } else {
    router.push({ name: 'ChatIndex' })
  }
}

// ===== 知识库目录 =====
interface ChunkItem { chunk_index: number, content: string }
interface FileItem {
  source_file_key: string
  parse_file_key: string
  file_name: string
  chunk_count: number
  create_time: string
  chunks: ChunkItem[]
}
interface KbItem {
  kb_id: number
  kb_name: string
  enabled: boolean
  create_time: string
  chunk_count: number
  files: FileItem[]
}

const kbList = ref<KbItem[]>([])
const kbLoading = ref(false)
// 当前选中的知识库（用于上传/检索默认值）
const selectedKbId = ref<number | null>(null)
const selectedKbName = ref<string>('default')

async function loadKbList() {
  kbLoading.value = true
  try {
    const resp = await listKnowledgeBase()
    const result = await resp.json()
    if (result.code === 200) {
      kbList.value = result.data || []
    } else {
      message.error(result.msg || '加载知识库列表失败')
    }
  } catch (e: any) {
    message.error(`加载知识库列表失败: ${e.message}`)
  } finally {
    kbLoading.value = false
  }
}

function selectKb(kb: KbItem) {
  selectedKbId.value = kb.kb_id
  selectedKbName.value = kb.kb_name
  searchKbName.value = kb.kb_name
}

// ===== 上传（带进度条） =====
const uploadLoading = ref(false)
const kbName = ref('default')
// 上传进度（0-100）
const uploadProgress = ref(0)
// 上传阶段描述
const uploadStage = ref('')
// 上传文件名
const uploadFileName = ref('')

async function handleUpload({ file }: { file: UploadFileInfo }) {
  if (!file.file) {
    message.error('文件不存在')
    return
  }
  uploadLoading.value = true
  uploadFileName.value = file.name
  uploadProgress.value = 0
  uploadStage.value = '准备上传...'

  // 模拟进度推进（实际后端为单次请求，无法获取真实进度）
  // 阶段：上传文件 -> 解析文本 -> 分块 -> 向量化 -> 入库
  const stages = [
    { until: 20, text: '上传文件中...' },
    { until: 40, text: '解析文件文本...' },
    { until: 60, text: '文本分块...' },
    { until: 90, text: '生成向量 embedding...' },
    { until: 100, text: '写入数据库...' },
  ]
  let stageIdx = 0
  const timer = setInterval(() => {
    const target = stages[stageIdx].until
    if (uploadProgress.value < target) {
      uploadProgress.value = Math.min(target, uploadProgress.value + Math.random() * 5 + 1)
      uploadStage.value = stages[stageIdx].text
    } else if (stageIdx < stages.length - 1) {
      stageIdx++
      uploadStage.value = stages[stageIdx].text
    }
  }, 300)

  try {
    const resp = await uploadKnowledgeFile(file.file, kbName.value || undefined)
    const result = await resp.json()
    // 完成
    clearInterval(timer)
    uploadProgress.value = 100
    uploadStage.value = '完成'

    if (result.code === 200) {
      message.success(`上传成功，共 ${result.data.chunk_count} 个文本块`)
      await loadKbList()
    } else {
      message.error(result.msg || '上传失败')
      uploadStage.value = `失败: ${result.msg || '未知错误'}`
    }
  } catch (e: any) {
    clearInterval(timer)
    message.error(`上传失败: ${e.message}`)
    uploadStage.value = `失败: ${e.message}`
  } finally {
    // 稍后重置进度条，让用户看到完成状态
    setTimeout(() => {
      uploadLoading.value = false
      uploadProgress.value = 0
      uploadStage.value = ''
      uploadFileName.value = ''
    }, 1200)
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

// 将检索结果按行解析为结构化条目，便于独立展示
interface ResultEntry { index: number, content: string, source: string }
const parsedResultEntries = computed<ResultEntry[]>(() => {
  if (!searchResult.value) return []
  const lines = searchResult.value.split('\n')
  const entries: ResultEntry[] = []
  for (const line of lines) {
    // 匹配 [n] content (source: xxx)
    const m = line.match(/^\[(\d+)\]\s+(.*?)(?:\s*\(source:\s*(.+?)\))?$/)
    if (m) {
      entries.push({
        index: Number(m[1]),
        content: m[2] || '',
        source: m[3] || '',
      })
    } else if (line.trim() && !line.startsWith('Knowledge base context')) {
      // 非标题、非空行作为普通内容
      entries.push({ index: entries.length + 1, content: line.trim(), source: '' })
    }
  }
  return entries
})

const isNoResult = computed(() => searchResult.value === '未找到相关内容')

onMounted(() => {
  loadKbList()
})
</script>

<template>
  <div class="p-24 h-full overflow-y-auto">
    <div class="flex items-center gap-12 mb-24">
      <div
        class="kb-back-btn flex items-center gap-6 px-12 py-6 rounded-6 cursor-pointer text-14 text-gray-600 hover:text-gray-900 hover:bg-gray-100 transition"
        @click="handleBack"
      >
        <div class="i-hugeicons:arrow-left-01 text-20"></div>
        <span>返回</span>
      </div>
      <h1 class="text-24 font-bold">知识库管理</h1>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-24">
      <!-- === 左侧：上传 + 目录 === -->
      <div class="flex flex-col gap-24">
        <!-- 上传卡片 -->
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
            :show-file-list="false"
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

          <!-- 上传进度条 -->
          <div v-if="uploadLoading || uploadProgress > 0" class="mt-16">
            <div class="flex items-center justify-between mb-4">
              <span class="text-13 text-gray-600 truncate" :title="uploadFileName">
                {{ uploadFileName }}
              </span>
              <span class="text-12 text-gray-400 ml-8">{{ Math.floor(uploadProgress) }}%</span>
            </div>
            <n-progress
              type="line"
              :percentage="Math.floor(uploadProgress)"
              :height="8"
              :border-radius="4"
              :status="uploadStage.startsWith('失败') ? 'error' : (uploadProgress >= 100 ? 'success' : 'default')"
              :show-indicator="false"
            />
            <p class="text-12 text-gray-500 mt-6 flex items-center gap-4">
              <span v-if="uploadProgress < 100 && !uploadStage.startsWith('失败')" class="i-svg-spinners:6-dots-rotate text-14" />
              <span>{{ uploadStage }}</span>
            </p>
          </div>

          <p class="text-12 text-gray-400 mt-12">
            支持格式: DOCX, PPT, PDF, TXT, XLSX, CSV | 上传后自动进行文本解析、分块、向量化
          </p>
        </div>

        <!-- 知识库目录 -->
        <div class="bg-white rounded-12 p-24 shadow-sm b b-solid b-gray-200">
          <div class="flex items-center justify-between mb-16">
            <h2 class="text-18 font-semibold flex items-center gap-8">
              <span class="i-material-symbols:folder-list text-22 text-primary" />
              知识库目录
            </h2>
            <n-button text size="small" :loading="kbLoading" @click="loadKbList">
              <template #icon>
                <span class="i-carbon-renew" />
              </template>
              刷新
            </n-button>
          </div>

          <n-spin :show="kbLoading">
            <n-empty
              v-if="kbList.length === 0"
              description="暂无知识库，请先上传文档"
              class="py-24"
            />
            <n-collapse v-else arrow-placement="left">
              <n-collapse-item
                v-for="kb in kbList"
                :key="kb.kb_id"
                :name="kb.kb_id"
              >
                <template #header>
                  <div class="flex items-center gap-8 w-full" @click.stop="selectKb(kb)">
                    <span class="i-material-symbols:folder text-18 text-primary" />
                    <span class="text-15 font-medium">{{ kb.kb_name }}</span>
                    <n-tag size="small" round type="info">
                      {{ kb.chunk_count }} 块
                    </n-tag>
                    <n-tag size="small" round>
                      {{ kb.files.length }} 文件
                    </n-tag>
                  </div>
                </template>

                <div class="pl-24">
                  <n-collapse arrow-placement="left">
                    <n-collapse-item
                      v-for="file in kb.files"
                      :key="file.parse_file_key"
                      :name="file.parse_file_key"
                    >
                      <template #header>
                        <div class="flex items-center gap-8">
                          <span class="i-material-symbols:description text-16 text-gray-500" />
                          <span class="text-14">{{ file.file_name }}</span>
                          <n-tag size="tiny" round>{{ file.chunk_count }} 块</n-tag>
                        </div>
                      </template>

                      <div class="pl-24 space-y-6 max-h-200 overflow-y-auto">
                        <div
                          v-for="chunk in file.chunks"
                          :key="chunk.chunk_index"
                          class="text-13 text-gray-600 bg-gray-50 rounded-6 p-8 leading-6 break-all"
                        >
                          <span class="text-gray-400 mr-4">#{{ chunk.chunk_index }}</span>
                          {{ chunk.content }}
                        </div>
                      </div>
                    </n-collapse-item>
                  </n-collapse>
                </div>
              </n-collapse-item>
            </n-collapse>
          </n-spin>
        </div>
      </div>

      <!-- === 右侧：检索 === -->
      <div class="bg-white rounded-12 p-24 shadow-sm b b-solid b-gray-200 flex flex-col">
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

        <!-- 检索结果：独立滚动区域，结构化展示避免字符重叠 -->
        <div
          v-if="searchResult"
          class="bg-gray-50 rounded-8 p-16 mt-8 flex-1 flex flex-col min-h-0"
        >
          <h3 class="text-14 font-semibold mb-8 text-gray-500 flex-shrink-0">
            检索结果
            <span v-if="parsedResultEntries.length" class="text-12 text-gray-400 ml-8">
              共 {{ parsedResultEntries.length }} 条
            </span>
          </h3>

          <!-- 无结果 -->
          <div v-if="isNoResult" class="text-14 text-gray-400 py-24 text-center">
            未找到相关内容
          </div>

          <!-- 结构化条目展示，独立滚动 -->
          <div
            v-else-if="parsedResultEntries.length"
            class="overflow-y-auto pr-4 flex-1 min-h-0"
            style="max-height: 400px;"
          >
            <div
              v-for="entry in parsedResultEntries"
              :key="entry.index"
              class="b b-solid b-gray-200 rounded-8 p-12 mb-8 bg-white last:mb-0"
            >
              <div class="flex items-center gap-8 mb-6">
                <n-tag size="small" round type="primary">{{ entry.index }}</n-tag>
                <span
                  v-if="entry.source"
                  class="text-11 text-gray-400 truncate"
                  :title="entry.source"
                >
                  source: {{ entry.source }}
                </span>
              </div>
              <div class="text-14 text-gray-700 leading-7 break-all whitespace-pre-wrap">
                {{ entry.content }}
              </div>
            </div>
          </div>

          <!-- 原始文本兜底展示 -->
          <div
            v-else
            class="overflow-y-auto pr-4 flex-1 min-h-0 text-14 text-gray-700 leading-7 break-all whitespace-pre-wrap"
            style="max-height: 400px;"
          >
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
          <div class="text-13 text-gray-500 line-clamp-2 break-all">
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
