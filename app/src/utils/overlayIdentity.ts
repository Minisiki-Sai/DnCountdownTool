/**
 * 悬浮窗顶部「副本 / Boss」两行身份。
 *
 * 四种状态对应界面上的四种后缀（不再滥用「未识别」）：
 *   announced → (已识别)  识别到了，且该副本勾选在语音播报白名单里
 *   muted     → (不播报)  识别到了副本，但没勾选，不会播报
 *   nonNest   → (非副本)  识别到了位置，但那是城镇/庭院，不是副本
 *   unknown   → 未识别     连位置都没有证据
 *
 * 关键约定：只要抓包读到了场景（哪怕只是「神圣天堂」），就绝不显示「未识别」。
 * 第一行前缀随之切换为「位置」，避免出现「副本: 神圣天堂」这种自相矛盾的写法。
 *
 * 只做展示，不参与任何统计或播报判定。
 */
import { countdownNest } from "./nestSkillCooldowns";

export type SkillIdentityState = "announced" | "muted" | "nonNest" | "unknown";
export type SkillIdentityLine = {
  name: string;
  state: SkillIdentityState;
  /** 名称前的标签，「副本」或「位置」。 */
  prefix: string;
  /** 名称后的括号后缀，空字符串表示不显示。 */
  label: string;
};
export type SkillIdentity = { nest: SkillIdentityLine; boss: SkillIdentityLine };

export const SKILL_IDENTITY_LABEL: Record<SkillIdentityState, string> = {
  announced: "(已识别)",
  muted: "(不播报)",
  nonNest: "(非副本)",
  unknown: "",
};

/** 位置调整模式下的占位示例，避免预览时两行都是「未识别」。 */
export const SKILL_IDENTITY_SAMPLE = {
  nest: "黑龙巢穴（一）",
  boss: "百夫长食人魔巴欧帕",
} as const;

export function resolveSkillIdentity(options: {
  /** 当前场景名：副本名或城镇名，后端 sceneName 优先。 */
  nestName: string;
  bossName: string;
  whitelist: readonly string[];
  /** nest / hub / map / unknown，由后端地图历史给出。 */
  sceneKind?: string;
  sample?: boolean;
}): SkillIdentity {
  const nestName = String(options.nestName || "").trim();
  const bossName = String(options.bossName || "").trim();
  const sceneKind = String(options.sceneKind || "").trim().toLowerCase();
  const whitelisted = options.whitelist.includes(countdownNest(nestName));
  const sample = Boolean(options.sample);

  const nestState: SkillIdentityState = !nestName
    ? sample
      ? "announced"
      : "unknown"
    : sample
      ? "announced"
      : sceneKind === "hub" || sceneKind === "map"
        ? "nonNest"
        : whitelisted
          ? "announced"
          : "muted";

  // Boss 行跟随副本行的结论：不在副本里、或副本不播报时，Boss 也不播报。
  const bossState: SkillIdentityState = !bossName
    ? sample
      ? "announced"
      : "unknown"
    : sample
      ? "announced"
      : nestState === "nonNest"
        ? "nonNest"
        : nestState === "announced"
          ? "announced"
          : "muted";

  const line = (
    name: string,
    state: SkillIdentityState,
    prefix: string,
    sampleName: string,
  ): SkillIdentityLine => {
    const shown = name || (sample ? sampleName : "");
    return {
      name: shown,
      state,
      prefix,
      label: shown ? SKILL_IDENTITY_LABEL[state] : "",
    };
  };

  return {
    nest: line(
      nestName,
      nestState,
      nestState === "nonNest" ? "位置" : "副本",
      SKILL_IDENTITY_SAMPLE.nest,
    ),
    // Boss 行没有目标时显示「未识别」而不是空串：本机停手超过 30 秒后后端会
    // 主动留空（不跟队友目标），界面上要明确告诉用户「现在没有在打的 Boss」，
    // 而不是留一行看不见内容的空白。
    boss: line(
      bossName || (bossState === "unknown" ? "未识别" : ""),
      bossState,
      "Boss",
      SKILL_IDENTITY_SAMPLE.boss,
    ),
  };
}
