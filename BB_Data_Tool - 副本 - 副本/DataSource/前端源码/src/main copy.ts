import type { UserModule } from './types'
import { ViteSSG } from 'vite-ssg'

// import "~/styles/element/index.scss";

// import ElementPlus from "element-plus";
// import all element css, uncommented next line
// import "element-plus/dist/index.css";

// or use cdn, uncomment cdn link in `index.html`

import { routes } from 'vue-router/auto-routes'
import App from './App.vue'

import '~/styles/index.scss'

import 'uno.css'
// If you want to use ElMessage, import it.
import 'element-plus/theme-chalk/src/message.scss'
import 'element-plus/theme-chalk/src/message-box.scss'

import axios from 'axios';

// if you do not need ssg:
// import { createApp } from "vue";

// const app = createApp(App);
// app.use(createRouter({
//   history: createWebHistory(),
//   routes,
// }))
// // app.use(ElementPlus);
// app.mount("#app");

// https://github.com/antfu/vite-ssg
export const createApp = ViteSSG(
  App,
  {
    routes,
    base: import.meta.env.BASE_URL,
    
  },
  (ctx) => {
    // install all modules under `modules/`
    Object.values(import.meta.glob<{ install: UserModule }>('./modules/*.ts', { eager: true }))
      .forEach(i => i.install?.(ctx))
    // ctx.app.use(Previewer)
  },
)

// 配置 Axios 的 params 序列化方式
axios.defaults.paramsSerializer = (params) => {
  const parts: string[] = [];
  Object.entries(params).forEach(([key, value]) => {
    if (Array.isArray(value)) {
      // 处理数组：直接生成 key=value1&key=value2
      value.forEach((item) => parts.push(`${key}=${encodeURIComponent(item)}`));
    } else if (value !== null && value !== undefined) {
      parts.push(`${key}=${encodeURIComponent(value)}`);
    }
  });
  return parts.join('&');
};