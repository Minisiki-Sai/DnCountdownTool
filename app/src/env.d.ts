/// <reference types="vite/client" />

/**
 * 构建期注入的版本号，值来自仓库根 `version.json`。
 * 注入点：`app/vite.config.ts` 的 `define`。
 */
declare const __APP_VERSION__: string;
