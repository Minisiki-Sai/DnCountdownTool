// 构建期常量：由 vite.config.ts 从仓库根 version.json 注入（见 define）。
// 不 import package.json —— 前端显示的版本号必须与 version.json 同源。
export const APP_NAME = "DN倒计时工具";
export const APP_SHORT_NAME = "DN倒计时工具";
export const APP_PRODUCT_DESCRIPTION = "增益 / 减益与 Boss 技能倒计时";

/** 侧栏展示用；改名只改这一处。 */
export const APP_AUTHOR = "小香批";

export const APP_VERSION =
  typeof __APP_VERSION__ === "undefined" ? "0.0.0" : __APP_VERSION__;
export const APP_VERSION_LABEL = `V${APP_VERSION}`;
