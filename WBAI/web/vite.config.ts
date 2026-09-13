import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { copyFileSync } from "node:fs";
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
let publicOutputDir = resolve(import.meta.dirname, "dist");
export default defineConfig({
  plugins: [
    react(),
    {
      name: "carme-public-files",
      configResolved(config) {
        publicOutputDir = resolve(config.root, config.build.outDir);
      },
      closeBundle() {
        for (const file of publicFiles)
          copyFileSync(
            resolve(import.meta.dirname, file),
            resolve(publicOutputDir, file),
          );
      },
    },
  ],
  publicDir: false,
  server: { proxy: { "/api": "http://127.0.0.1:8100" } },
  build: { outDir: "dist", emptyOutDir: true },
});
