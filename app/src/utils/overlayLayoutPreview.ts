import type { BuffOverlaySettings, BuffOverlayStatus } from "../types/api";

/** Presentation-only samples. Never publish these to the capture/inspection bus. */
export function buildOverlayLayoutPreview(settings: BuffOverlaySettings, snapshot: BuffOverlayStatus | null): BuffOverlayStatus {
  const second = snapshot?.elapsedSeconds || 60;
  const self = snapshot?.selfSourceId || "layout-preview-self";
  const options = snapshot?.buffOptions || [];
  const names = ["时间加速", "精神爆发", "幽灵之魂", "阿兹纳之息", "审判力量", "鸡尾酒"];
  const buffKeys = settings.selectedBuffs.length ? settings.selectedBuffs
    : options.length ? options.slice(0, 6).map(row => row.key) : names;
  const option = (key: string) => options.find(row => row.key === key);
  const remaining = (index: number) => 8 + index % 4 * 5;
  const buffs = buffKeys.map((key, index) => ({
    key: `layout-buff:${key}`, catalog_key: key, skill_name: option(key)?.skill || key,
    target_id: self, source_id: self, job: option(key)?.provider || "黑暗女王",
    start_seconds: second - 5, end_seconds: second + remaining(index), duration_seconds: remaining(index) + 5,
    icon_index: option(key)?.iconIndex ?? -1, data_source: "位置调整示例（非实战）",
  }));
  const debuffs = [
    ["damage", "magic", "魔法增伤", 30, "能量反射"],
    ["resistance", "dark", "减暗抗", 30, "灵魂之门 EX"],
    ["defense", "magic", "减魔防", 20, "破甲"],
    ["critical", "critical", "减致命抵抗", 20, "旋风斩 EX"],
    ["freeze", "freeze", "叠冰", 2, "冰河"],
  ].map(([category, mode, label, value, skill], index) => ({
    key: `layout-debuff:${category}`, category, mode, label, current_value: value,
    end_seconds: second + remaining(index), duration_seconds: remaining(index) + 5,
    contributors: [{ key: `layout-skill:${category}`, skill_name: skill, source_id: self,
      source_job: "黑暗女王", current_value: value, end_seconds: second + remaining(index),
      duration_seconds: remaining(index) + 5, data_source: "位置调整示例（非实战）" }],
    data_source: "位置调整示例（非实战）",
  }));
  return {
    state: "running", status: "位置调整示例", elapsedSeconds: second, selfSourceId: self,
    players: snapshot?.players.length ? snapshot.players : [{ source_id: self, job: "黑暗女王", name: "示例角色",
      damage: 100_000_000, dps: 1_800_000, attack_types: ["magic"], elements: ["dark"],
      damage_series: Array.from({ length: 61 }, (_, index) => [second - 60 + index, 1_000_000 + index % 8 * 150_000]) }],
    buffs: snapshot?.buffs.length ? snapshot.buffs : buffs,
    debuffs: snapshot?.debuffs.length ? snapshot.debuffs : debuffs,
    cooldowns: snapshot?.cooldowns || [],
    summons: snapshot?.summons || [],
    playerHealth: [], buffOptions: options, summonOptions: snapshot?.summonOptions || [],
  };
}

export function browserLayoutPreviewRequested(): boolean {
  return !("__TAURI_INTERNALS__" in window)
    && new URLSearchParams(window.location.search).get("layoutPreview") === "1";
}
