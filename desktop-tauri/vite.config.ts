import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from 'tailwindcss'
import autoprefixer from 'autoprefixer'
import { resolve } from 'node:path'

// Tauri dev 固定端口,tauri.conf.json 的 devUrl 与之对应
const host = process.env.TAURI_DEV_HOST || 'localhost'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  clearScreen: false,
  server: {
    port: 1421,
    strictPort: true,
    host: host === false ? false : host,
    watch: {
      ignored: ['**/src-tauri/**']
    }
  },
  envPrefix: ['VITE_', 'TAURI_ENV_*'],
  build: {
    target: 'chrome110',
    minify: 'esbuild',
    sourcemap: false
  },
  css: {
    postcss: {
      plugins: [tailwindcss(), autoprefixer()]
    }
  }
})
