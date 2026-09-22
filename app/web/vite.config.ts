import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { copyFileSync, readdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";
const publicFiles = [
  "favicon.png",
  "apple-touch-icon.png",
  "icon-192.png",
  "icon-512.png",
  "icon-maskable.png",
  "manifest.webmanifest",
  "sw.js",
];
// 哈希名每次构建都会变，已“添加到主屏幕”的 iOS 图标会因为 URL 变化而抓不到（表现为 logo 失效）。
// 所以产出阶段把 index.html 改回稳定根路径，并把重复的哈希副本删掉（publicFiles 由 closeBundle 复制到 dist 根）。
const publicStems = publicFiles.map((f) => f.replace(/\.(png|webmanifest)$/, ""));
const HASHED_HREF =
  /\/assets\/(favicon|apple-touch-icon|icon-\d+|icon-maskable|manifest)-[A-Za-z0-9_-]+\.(png|webmanifest)/g;
const hashed = new RegExp(`^(${publicStems.join("|")})-[A-Za-z0-9_-]+\\.(png|webmanifest)$`);
let publicOutputDir = resolve(import.meta.dirname, "dist");
export default defineConfig({
  plugins: [
    react(),
    {
      name: "carme-public-files",
      // Vite 7 在 generateBundle 阶段才把 index.html 里的资源换成 /assets/<name>-<hash>.<ext>，
      // 所以重写要放在 post 插件里，且用 generateBundle（transformIndexHtml 太早，拿不到最终 HTML）。
      enforce: "post",
      configResolved(config) {
        publicOutputDir = resolve(config.root, config.build.outDir);
      },
      generateBundle(_options, bundle) {
        const html = bundle["index.html"];
        if (html && html.type === "asset") {
          html.source = String(html.source).replace(HASHED_HREF, "/$1.$2");
        }
      },
      closeBundle() {
        for (const file of publicFiles)
          copyFileSync(
            resolve(import.meta.dirname, file),
            resolve(publicOutputDir, file),
          );
        for (const file of readdirSync(resolve(publicOutputDir, "assets")))
          if (hashed.test(file)) rmSync(resolve(publicOutputDir, "assets", file));
      },
    },
  ],
  publicDir: false,
  server: { proxy: { "/api": "http://127.0.0.1:8100" } },
  build: { outDir: "dist", emptyOutDir: true },
});
