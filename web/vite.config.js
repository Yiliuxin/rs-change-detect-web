import { defineConfig, loadEnv } from 'vite';
import { resolve } from 'path';

// 后端地址注入方式（按优先级）：shell 环境变量 > web/.env.local > 默认 8000。
// 本机适配（如 8000 被 Docker 容器占用、后端改跑 8010）写 web/.env.local，不入库：
//   VITE_API_TARGET=http://localhost:8010
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, '');
  const API_TARGET = process.env.VITE_API_TARGET || env.VITE_API_TARGET || 'http://localhost:8000';

  return {
    // 多页构建：工作台（index.html）+ 登录页（login.html，注册/登录扩展）
    build: {
      rollupOptions: {
        input: {
          main: resolve(__dirname, 'index.html'),
          login: resolve(__dirname, 'login.html'),
        },
      },
    },
    server: { port: 5173, strictPort: true, proxy: { '/api': { target: API_TARGET, changeOrigin: true } } }
  };
});
