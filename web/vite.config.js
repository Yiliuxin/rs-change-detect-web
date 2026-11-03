export default {
  server: { port: 5173, strictPort: true, proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } } }
}
