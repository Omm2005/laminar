import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Two pages: the landing page (index.html, plain HTML + three.js) and the React console (console.html).
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: { main: 'index.html', console: 'console.html' },
    },
  },
})
