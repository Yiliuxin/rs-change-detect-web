// 8000 被本机 Docker 容器占用，后端实际跑在 8010（本地适配，勿提交）
import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  // 多页构建：工作台（index.html）+ 登录页（login.html，注册/登录扩展）
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        login: resolve(__dirname, 'login.html'),
      },
    },
  },
  server: { port: 5173, strictPort: true, proxy: { '/api': { target: 'http://localhost:8010', changeOrigin: true } } }
})
