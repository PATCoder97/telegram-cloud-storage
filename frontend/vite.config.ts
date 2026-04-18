import path from "node:path";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import VueI18nPlugin from "@intlify/unplugin-vue-i18n/vite";
import { compression } from "vite-plugin-compression2";

const plugins = [
  vue(),
  VueI18nPlugin({
    include: [path.resolve(__dirname, "./src/i18n/**/*.json")],
  }),
  compression({ include: /\.js$/, deleteOriginalAssets: false }),
];

const resolve = {
  alias: {
    // vue: "@vue/compat",
    "@/": `${path.resolve(__dirname, "src")}/`,
  },
};

// https://vitejs.dev/config/
export default defineConfig(({ command }) => {
  if (command === "serve") {
    return {
      plugins,
      resolve,
      server: {
        proxy: {
          "/api": "http://127.0.0.1:5010",
        },
      },
    };
  } else {
    return {
      plugins,
      resolve,
      base: "",
      build: {
        outDir: "../frontend_dist",
        emptyOutDir: true,
        chunkSizeWarningLimit: 1200,
      },
    };
  }
});
