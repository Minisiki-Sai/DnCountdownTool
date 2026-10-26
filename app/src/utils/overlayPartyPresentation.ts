export type OverlayPartyPlayer = {
  source_id: string;
  job?: string;
  name?: string;
};

type OverlayBuffOption = {
  key: string;
  scope?: string;
  fullDurationTeam?: boolean;
};

function textValue(value: unknown): string {
  return String(value ?? "").trim();
}

/** Do not turn an unobserved teammate receipt into a missing-Buff claim. */
export function confirmedMissingBuffsByTarget(
  observations: readonly {
    target_id: string; catalog_key: string; end_seconds: number; permanent: boolean;
  }[],
  activeBuffs: readonly Record<string, unknown>[],
  seconds: number,
): Map<string, Set<string>> {
  const missing = new Map<string, Set<string>>();
  if (!Number.isFinite(seconds)) return missing;
  const present = new Set<string>();
  for (const row of activeBuffs) {
    present.add(`${textValue(row.target_id)}|${textValue(row.catalog_key)}`);
  }
  // Presence wins over an ended application from another provider/component.
  for (const row of observations) {
    if (row.permanent || Number(row.end_seconds) > seconds) {
      present.add(`${textValue(row.target_id)}|${textValue(row.catalog_key)}`);
    }
  }
  for (const row of observations) {
    const target = textValue(row.target_id);
    const key = textValue(row.catalog_key);
    const end = Number(row.end_seconds);
    if (!target || !key || !Number.isFinite(end) || end <= 0 || end > seconds
      || row.permanent || present.has(`${target}|${key}`)) continue;
    if (!missing.has(target)) missing.set(target, new Set());
    missing.get(target)?.add(key);
  }
  return missing;
}

export function uniqueOverlayPartyPlayers<T extends OverlayPartyPlayer>(players: readonly T[]): T[] {
  const bySourceId = new Map<string, T>();
  for (const player of players) {
    const sourceId = textValue(player.source_id);
    if (!sourceId) continue;
    const current = bySourceId.get(sourceId);
    if (!current || (!textValue(current.job) && textValue(player.job))) {
      bySourceId.set(sourceId, player);
    }
  }
  return [...bySourceId.values()];
}

/** Buff targets and cached character names are not evidence of membership. */
export function confirmedBuffPartyPlayers(
  players: readonly OverlayPartyPlayer[],
  partyMembers: readonly Record<string, unknown>[] = [],
): OverlayPartyPlayer[] {
  const hasPlayerJob = (job: unknown) => {
    const value = textValue(job);
    return !!value && !/^(?:未知|未知职业|boss(?:\s.*)?|lv\s*\d+)$/i.test(value);
  };
  const confirmed = new Map(uniqueOverlayPartyPlayers(players)
    .filter(player => hasPlayerJob(player.job))
    .map(player => [textValue(player.source_id), player]));
  for (const member of partyMembers) {
    const sourceId = textValue(member.actor_id);
    if (!sourceId || !(Number(member.confidence) >= 0.65) || !hasPlayerJob(member.job)) continue;
    const current = confirmed.get(sourceId);
    confirmed.set(sourceId, {
      source_id: sourceId,
      job: textValue(current?.job) || textValue(member.job),
      name: textValue(current?.name) || textValue(member.name),
    });
  }
  return [...confirmed.values()];
}

export function exactOverlayPlayerName(player: OverlayPartyPlayer): string {
  const sourceId = textValue(player.source_id);
  const name = textValue(player.name);
  return (
    name
    && name !== sourceId
    && !/^角色\s+0x[0-9a-f]+$/i.test(name)
    && !/^0x[0-9a-f]+$/i.test(name)
    && !/^(?:角色|队友|未知角色|未知职业|职业\d*)$/.test(name)
  ) ? name : "";
}

/** Prefer an exact spawn name; fall back to job/job1/job2 when unavailable. */
export function buildOverlayProfessionLabels(players: readonly OverlayPartyPlayer[]): Map<string, string> {
  const groups = new Map<string, OverlayPartyPlayer[]>();
  const labels = new Map<string, string>();
  for (const player of uniqueOverlayPartyPlayers(players)) {
    const sourceId = textValue(player.source_id);
    const exactName = exactOverlayPlayerName(player);
    if (exactName) {
      labels.set(sourceId, exactName);
      continue;
    }
    const job = textValue(player.job) || "未知职业";
    const group = groups.get(job) || [];
    group.push(player);
    groups.set(job, group);
  }
  for (const [job, rows] of groups) {
    const ordered = [...rows].sort((left, right) =>
      textValue(left.source_id).localeCompare(textValue(right.source_id), "zh-CN"),
    );
    ordered.forEach((row, index) => {
      labels.set(textValue(row.source_id), ordered.length > 1 ? `${job}${index + 1}` : job);
    });
  }
  return labels;
}

export function overlayProfessionLabel(
  labels: ReadonlyMap<string, string>,
  sourceId: unknown,
  fallbackJob?: unknown,
): string {
  return labels.get(textValue(sourceId)) || textValue(fallbackJob) || "未知职业";
}

/**
 * Remember only reviewed, continuously maintainable team Buffs that actually
 * appeared in this capture. A packet's raw `permanent` bit is intentionally
 * ignored: finite skills can carry an unrelated open-ended status component.
 */
export function updateObservedFullDurationTeamBuffs(
  observed: Set<string>,
  options: readonly OverlayBuffOption[],
  buffs: readonly Record<string, unknown>[],
): void {
  const eligible = new Set(
    options
      .filter(row => row.scope === "team" && row.fullDurationTeam === true)
      .map(row => textValue(row.key))
      .filter(Boolean),
  );
  for (const key of [...observed]) if (!eligible.has(key)) observed.delete(key);
  for (const row of buffs) {
    const key = textValue(row.catalog_key);
    if (eligible.has(key)) observed.add(key);
  }
}
