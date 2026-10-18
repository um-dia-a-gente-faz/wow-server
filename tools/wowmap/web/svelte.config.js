import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

export default {
  preprocess: vitePreprocess(),
  kit: {
    // SPA: index.html answers every route (api.py falls back to it for unknown /v2 paths).
    adapter: adapter({ fallback: 'index.html' }),
    paths: { base: '/v2' }
  }
};
