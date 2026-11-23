import { createRouter, createWebHistory } from 'vue-router'

import AlertConsole from '@/views/AlertConsole.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 告警值班台：按采样核对连续超限、回查判定与归因证据
    { path: '/console/alert', name: 'alert-console', component: AlertConsole },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
