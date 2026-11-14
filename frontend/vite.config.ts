import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  // Fail the production build if the API URL is missing (see src/lib/api.ts).
  if (mode === 'production' && !(process.env.VITE_API_BASE_URL || env.VITE_API_BASE_URL)) {
    throw new Error('VITE_API_BASE_URL is required for a production build (the public URL of the API).')
  }
  return { plugins: [react()] }
})
