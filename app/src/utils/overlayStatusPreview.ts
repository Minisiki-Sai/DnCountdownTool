/**
 * 「悬浮窗设置 → Buff / Debuff」右侧实时预览的取样规则。
 *
 * 为什么单独抽出来：预览以前固定取「前 3 条 Buff + 前 2 条 Debuff」，所以
 * 取消勾选 Debuff 后预览里仍然有物理增伤/减抗，看起来像开关没生效。真实
 * 悬浮窗（`useBuffOverlayPresentation.statusRows`）是按同一对开关过滤的，
 * 预览必须跟着走；抽成纯函数才能直接测到这条一致性。
 */

export type StatusPreviewRow = {
  key: string;
  kind: "buff" | "debuff";
};

export type StatusPreviewLimits = {
  buff?: number;
  debuff?: number;
};

const DEFAULT_LIMITS: Required<StatusPreviewLimits> = { buff: 3, debuff: 2 };

export function selectStatusPreviewRows<T extends StatusPreviewRow>(
  rows: readonly T[],
  showBuff: boolean,
  showDebuff: boolean,
  limits: StatusPreviewLimits = {},
): T[] {
  const buffLimit = Math.max(0, limits.buff ?? DEFAULT_LIMITS.buff);
  const debuffLimit = Math.max(0, limits.debuff ?? DEFAULT_LIMITS.debuff);
  const picked: T[] = [];
  if (showBuff) {
    picked.push(...rows.filter(row => row.kind === "buff").slice(0, buffLimit));
  }
  if (showDebuff) {
    picked.push(...rows.filter(row => row.kind === "debuff").slice(0, debuffLimit));
  }
  return picked;
}
