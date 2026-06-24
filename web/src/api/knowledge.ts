/**
 * 知识库 API
 */
import { useUserStore } from '@/store/business/userStore'

const BASE_URL = `${location.origin}/sanic/knowledge-base`

const getHeaders = () => {
  const userStore = useUserStore()
  const token = userStore.getUserToken()
  return {
    'Authorization': `Bearer ${token}`,
  }
}

/**
 * 上传文件到知识库
 * @param file 文件对象
 * @param kbName 知识库名称
 */
export async function uploadKnowledgeFile(file: File, kbName?: string) {
  const formData = new FormData()
  formData.append('file', file)
  if (kbName) {
    formData.append('kb_name', kbName)
  }

  const url = new URL(`${BASE_URL}/upload`)
  const req = new Request(url, {
    mode: 'cors',
    method: 'post',
    headers: getHeaders(),
    body: formData,
  })
  return fetch(req)
}

/**
 * 检索知识库
 * @param question 查询问题
 * @param kbName 知识库名称（可选）
 * @param kbId 知识库 ID（可选）
 * @param topK 返回结果数量（可选）
 */
export async function searchKnowledgeBase(params: {
  question: string
  kb_name?: string
  kb_id?: number
  top_k?: number
}) {
  const url = new URL(`${BASE_URL}/search`)
  const req = new Request(url, {
    mode: 'cors',
    method: 'post',
    headers: {
      ...getHeaders(),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(params),
  })
  return fetch(req)
}