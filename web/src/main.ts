import App from '@/App.vue'

import InstallGlobalComponents from '@/components'
import { setupRouter } from '@/router'

// 修复 Vue Router 4.6.x 与 Edge 浏览器的兼容性问题
// Edge 在 replaceState 的 state 参数不为空时会触发"页面激活"机制，
// 导致浏览器窗口最小化后立即弹出。将 state 置为 null 可避免此问题，
// 仅影响滚动位置恢复，不影响路由导航功能。
const __origReplaceState = window.history.replaceState
window.history.replaceState = function (state, title, url) {
  return __origReplaceState.call(this, null, title, url)
}

import { setupStore } from '@/store'

import 'virtual:uno.css'

const app = createApp(App)

function setupPlugins() {
  app.use(InstallGlobalComponents)
}

async function setupApp() {
  setupStore(app)
  await setupRouter(app)
  app.mount('#app')
}

setupPlugins()
setupApp()

// 初始化用户状态
const userStore = useUserStore()
userStore.init()

export default app
