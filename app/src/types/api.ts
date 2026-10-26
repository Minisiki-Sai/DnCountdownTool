export type ModuleStatus = "native" | "planned";

export interface SimulatorModule {
  id: string;
  label: string;
  group: "build" | "skills" | "team" | "live";
  status: ModuleStatus;
}

export interface BootstrapPayload {
  version: string;
  title: string;
  engine: { status: string; loadedMs: number; source: string };
  modules: SimulatorModule[];
  jobs: {
    base?: string[];
    first: string[];
    second: Record<string, string[]>;
    firstByBase?: Record<string, string[]>;
  };
}

export interface EquipmentItem {
  slot: string;
  name: string;
  suffix?: string;
  enhance?: number;
  enchants?: string[];
  variant?: string;
}
export interface GlyphAItem { level: string; name: string; extra?: string | null }
export interface GlyphSItem {
  level: string;
  name: string;
  base_attr1?: string | null;
  rank1?: string | number | null;
  base_attr2?: string | null;
  rank2?: string | number | null;
  base_attr3?: string | null;
  rank3?: string | number | null;
  base_attr4?: string | null;
  rank4?: string | number | null;
  extra?: string | null;
}
export interface RuneEntry { attr?: string | null; rank?: string | number | null }
export interface NormalRuneItem { type: string; level?: string | null; entries: RuneEntry[] }
export interface MasteryItem { name: string; level: number }
export interface FashionItem { slot: string; name: string }
export interface CardGradeItem { name: string; grade: string }
export interface BuffItem { skill?: string | null; level?: string | number | null }

export interface FreeInputsPayload {
  percent: Record<string, number>;
  stats: Record<string, number>;
  stat_percent: Record<string, number>;
}

export interface SkillGlyphSelection {
  skill: string;
  effect: string;
  option: string;
  value: number;
  selection_source?: "combat_inferred" | "auto_fill" | "manual_override";
  inference_source?: string;
}
export interface SkillGlyphOptimization {
  count: number;
  rows: SkillGlyphSelection[];
  baseline: number;
  optimized: number;
  gain: number;
  timeline: TimelinePayload;
}

export interface BuildPayload {
  first_job: string;
  second_job: string;
  boss?: string;
  crit_resist_value?: number;
  element_resists?: Record<string, number>;
  monster_defense?: number;
  freeze_coefficient?: number;
  free_percent?: Record<string, number>;
  free_stats?: Record<string, number>;
  free_stat_percent?: Record<string, number>;
  free_inputs?: FreeInputsPayload;
  equipment?: EquipmentItem[];
  jewelry?: EquipmentItem[];
  glyph_a?: GlyphAItem[];
  glyph_s?: GlyphSItem[];
  normal_runes?: NormalRuneItem[];
  core_rune?: Record<string, string | number | null>;
  mastery?: MasteryItem[];
  fashion?: FashionItem[];
  title?: string;
  collection_level?: number;
  card_grades?: CardGradeItem[];
  card_bonus_levels?: Record<string, number>;
  talismans?: Array<{
    slot: string;
    grade: string;
    name: string;
    extra: string;
  }>;
  engravings?: Array<{
    slot: string;
    grade: string;
    value_rank: string;
    main_value: number;
    secondary_value: number;
    extra: string;
    extra_tier: string;
    extra_rank?: string;
    extra_value: number;
  }>;
  buffs?: BuffItem[];
  skill_build?: { second_job?: string; levels?: Record<string, number> };
  skill_mastery_build?: { second_job?: string; levels?: Record<string, number> };
  skill_glyphs?: { second_job?: string; count?: number; rows?: SkillGlyphSelection[] } | null;
  skill_multiplier_overrides?: {
    second_job?: string;
    rows?: Array<{ skill: string; value: number }>;
  } | Record<string, number>;
  skill_sim_settings?: Record<string, unknown>;
  skill_sim?: Array<{
    skill: string;
    level: string;
    cooldown: number;
    multiplier: number;
    cooldownOverride?: boolean;
  }>;
  team_sim?: Record<string, unknown>;
  profile_sync_review?: {
    source?: string;
    timestampMs?: number;
    targetId?: string;
    job?: string;
    items?: ProfileSyncItem[];
  };
  [key: string]: unknown;
}

export interface ResultRow {
  attr: string;
  base: number;
  equipment: number;
  card: number;
  talisman: number;
  glyph: number;
  normal_rune: number;
  core_mastery: number;
  actual: number;
  converted: number;
  buffed: number;
  percent: number | null;
}
export interface CalculationPayload {
  rows: ResultRow[];
  summary: Record<string, number>;
  elapsedMs: number;
}

export interface BuildCatalog {
  equipment: Record<string, string[]>;
  jewelry: Record<string, string[]>;
  enchants: { attack: string[]; defense: string[]; all: string[] };
  glyphA: { levels: string[]; names: string[]; extras: string[] };
  glyphS: {
    levels: string[];
    names: string[];
    extras: string[];
    ranks: string[];
    baseOptions: Record<string, string[]>;
    extraOptions: Record<string, string[]>;
  };
  runes: {
    levels: string[];
    attackAttrs: string[];
    defenseAttrs: string[];
    ranks: string[];
    coreLevels: string[];
    conversions: string[];
    coreRatioRanks: Record<string, string[]>;
    coreCoeffRanks: Record<string, string[]>;
  };
  cards: string[];
  cardLevels: Record<string, number>;
  talismans: {
    slots: Array<{ name: string; percent: number }>;
    grades: string[];
    names: Record<string, string[]>;
    extras: Record<string, string[]>;
  };
  titles: string[];
  fashion: Record<string, string[]>;
  engraving: {
    slots: string[];
    grades: string[];
    extras: string[];
    tiers: string[];
    mainValues: Record<string, string[]>;
    secondaryValues: Record<string, string[]>;
    extraRanks: Record<string, string[]>;
  };
  buffs: string[];
  bosses: Record<string, Record<string, number>>;
  autoEquipTargets: Record<string, string[]>;
  limits: Record<string, [number, number]>;
}

export interface BuildDetails {
  resonance?: { active: boolean; label: string; reason: string; text: string; attrs: Record<string, number>; tiers: Array<{ enhance: number; attrs: Record<string, number> }> };
  equipment: Array<{ slot: string; attrs: Record<string, number>; text: string }>;
  equipmentSet: string;
  jewelry: Array<{ slot: string; attrs: Record<string, number>; text: string }>;
  jewelrySet: string;
  glyphA: Array<{ index: number; attrs: Record<string, number>; text: string }>;
  glyphS: Array<{ index: number; attrs: Record<string, number>; text: string }>;
  runes: Array<{ index: number; attrs: Record<string, number>; text: string }>;
  coreRune: Record<string, string | number>;
  coreRuneText: string;
  cards: Array<{ name: string; attrs: Record<string, number>; text: string }>;
  cardTotals: Record<string, number>;
  cardBoosted: Record<string, number>;
  talismans: Array<{
    slot: string;
    percent: number;
    attrs: Record<string, number>;
    text: string;
  }>;
  talismanTotals: Record<string, number>;
  collection: string;
  title: string;
  mastery: Array<{ name: string; text: string }>;
  fashion: Array<{ slot: string; text: string }>;
  buffs: Array<{ index: number; text: string }>;
  engravings: Array<{ slot: string; attrs: Record<string, number>; text: string }>;
}

export interface SkillLevelData {
  level: number;
  required_level: number;
  sp: number;
  cooldown: number;
  desc: string;
}
export interface SkillTreeEntry {
  id: string;
  name: string;
  max_level: number;
  required_skill_id?: string;
  levels: SkillLevelData[];
  [key: string]: unknown;
}
export interface SkillTreeGroup {
  job: { id: number; name: string; sp: number; index: number };
  skills: SkillTreeEntry[];
}
export interface SkillGlyphCatalogEntry {
  job: string;
  skill_id: number;
  skill: string;
  effects: Record<string, Array<{ grade: string; value: number }>>;
}
export interface SkillBuildPayload {
  job: string;
  tree: { total_sp: number; groups: SkillTreeGroup[] };
  mastery: { total_sp: number; groups: SkillTreeGroup[] };
  glyphs: SkillGlyphCatalogEntry[];
  defaults: Partial<BuildPayload>;
}

export interface SkillBuildScheme {
  name: string;
  second_job: string;
  builtin?: boolean;
  saved_at?: number;
  skill_build?: BuildPayload["skill_build"];
  skill_mastery_build?: BuildPayload["skill_mastery_build"];
  skill_glyphs?: BuildPayload["skill_glyphs"];
  skill_multiplier_overrides?: BuildPayload["skill_multiplier_overrides"];
}

export interface SkillDamageRow {
  displayName?: string;
  variantNote?: string;
  comparisonRows?: SkillDamageRow[];
  channelMultipliers?: Record<string, number>;
  skill: string;
  level: string;
  effectiveLevel: string;
  masteryBonus: number;
  requiredLevel: number;
  requiredLevelText: string;
  baseCooldown?: number;
  cooldown: number;
  automaticCooldown?: number;
  cooldownOverride?: boolean;
  multiplier: number;
  settledMultiplier?: number;
  actionSpeedOutputMultiplier?: number;
  referenceHitCount?: number | null;
  modeledHitCount?: number | null;
  hitModelNote?: string;
  burningHandPercent: number;
  rawPercent: number;
  effectivePercent: number;
  actualDamage: number;
  dps: number;
  dpa: number;
  castTime: number;
  castTimeText: string;
  element: string;
  elementMultiplier: number;
  dpsMultiplier: number;
  channels: Record<string, number>;
  rawParts: string[];
  effectiveParts: string[];
  dpsParts: string[];
  dpaParts: string[];
  periodicCycle: number;
  periodicNote: string;
  description: string;
}
export interface SkillRowsPayload {
  job: string;
  rows: SkillDamageRow[];
  summary: Record<string, number>;
  attributes: ResultRow[];
  totalDps?: number;
}

export interface TimelineSegment {
  skill: string;
  start: number;
  end: number;
  output: number;
  effectiveScore: number;
  element: string;
  actionSpeed: number;
  type?: string;
  kind?: string;
  castTime?: number;
  dpa?: number;
  [key: string]: unknown;
}
export interface TimelinePayload {
  job: string;
  window: number;
  segments: TimelineSegment[];
  total: number;
  dps: number;
  contribution: Array<{ skill: string; count: number; output: number; share: number }>;
  planner: string;
  effectWindows?: Record<string, unknown>[];
  blockedIntervals?: number[][];
  legacyParity?: boolean;
  fallbackReason?: string;
  auto?: TimelineAxisPayload;
  manual?: TimelineAxisPayload;
  candidates?: TimelineCandidate[];
  availableSkills?: string[];
  manualSequence?: string[];
  autoSequence?: string[];
  manualIssues?: string[];
  scenarioComparison?: TimelineScenario[];
}
export interface TimelineAxisPayload {
  segments: Array<TimelineSegment & Record<string, unknown>>;
  total: number;
  dps: number;
  contribution: Array<{ skill: string; count: number; output: number; rawOutput?: number; share: number }>;
}
export interface TimelineCandidate {
  skill: string;
  dpa: number;
  dps: number;
  cooldown: number;
  castTime: number;
  remainingCooldown: number;
  ready: boolean;
  tracksCooldown?: boolean;
  ultimate: boolean;
}
export interface TimelineScenario {
  name: string;
  outputPerSecond: number;
  gain: number;
}
export interface TeamMemberResult {
  job: string;
  damage: number;
  dps: number;
  share: number;
  timeline: TimelinePayload;
  handMultiplier?: number;
  baseDamage?: number;
  baseDps?: number;
  teamGain?: number;
  teamMultiplier?: number;
  gainPercent?: number;
  customBuild?: boolean;
}
export interface TeamPreviewPayload {
  members: TeamMemberResult[];
  window: number;
  total: number;
  dps: number;
  settings?: Record<string, unknown>;
  ranking?: Array<{ job: string; damage: number; dps: number }>;
  optimizer?: string;
  stages?: Record<string, unknown>;
  activeDps?: number;
  activeSeconds?: number;
  blockedSeconds?: number;
  mechanisms?: Array<{ name: string; start: number; end: number }>;
  buffSchedule?: Array<Record<string, unknown>>;
  scheduleMode?: string;
  planner?: string;
  comparison?: Array<{
    job: string;
    standardDps: number;
    customDps: number;
    loss: number;
    lossRate: number;
    share: number;
  }>;
}
export interface TeamEffectRow {
  id: string;
  enabled: boolean;
  provider: string;
  skill: string;
  level: string;
  cooldown: number;
  duration: number;
  usageRatio: number;
  coverage: number;
  attrs: Record<string, number>;
  effect: string;
  contribution: number;
  contributionPercent: number;
  removalLossPercent: number;
}
export interface TeamSetupPayload {
  effects: TeamEffectRow[];
  members: string[];
  effectCount: number;
  cdrSkills: string[];
  auraJobs: string[];
  auraOptions: string[];
}

export interface JobStatus<T = unknown> {
  done: boolean;
  state: "queued" | "running" | "paused" | "completed" | "cancelled" | "failed" | "missing";
  progress: number;
  label: string;
  result: T | null;
  error: string;
}

export interface JobSummary extends Omit<JobStatus, "result"> {
  jobId: string;
  kind: string;
  createdAt: number;
}

export interface NativeNode {
  type: string;
  id: string;
  note?: string;
  windowSeconds?: number;
  extentSeconds?: number;
  pixelsPerSecond?: number;
  segmentLabel?: string;
  referenceLabel?: string;
  segments?: Record<string, unknown>[];
  referenceSegments?: Record<string, unknown>[];
  effects?: Record<string, unknown>[];
  metrics?: Array<{
    name: string;
    segments: Array<{ name?: string; points: Array<[number, number]> }>;
    minimum?: number;
    maximum?: number;
    suffix?: string;
    color?: string;
    positiveColor?: string;
    negativeColor?: string;
    deltaColors?: boolean;
    zeroLine?: boolean;
    stepAfter?: boolean;
    detail?: string;
  }>;
  editable?: boolean;
  selected?: number;
  rangeSelection?: boolean;
  series?: unknown;
  bossHp?: unknown;
  bossPhases?: Record<string, unknown>[];
  responsive?: boolean;
  keyEvents?: Array<{ id: string; second: number; label: string; kind: "death" | "boss" }>;
  focusedEventId?: string;
  highlightedRanges?: Array<{ start: number; end: number }>;
  viewStart?: number;
  timeOffsetSeconds?: number;
  smoothingSeconds?: number;
  curveStyle?: "linear" | "step-after";
  occurrences?: Record<string, unknown>[];
  playerDetails?: Record<string, unknown>;
}

export interface ReplaySkill {
  skill_id: string;
  name: string;
  damage: number;
  healing: number;
  hits: number;
  /** Transported damage rows before status/additional rows are removed. */
  damage_records?: number;
  critical_hits: number;
  critical_samples?: number;
  cast_count?: number;
  direct_cast_count?: number;
  inferred_cast_count?: number;
  average_hits?: number | null;
  share: number;
  trigger_skills?: Array<[string, string, number, number, string, "exact" | "candidate"]>;
  additional_damage?: Array<[string, string, number, number]>;
}

export interface ReplayPlayer {
  source_id: string;
  name: string;
  job: string;
  damage: number;
  healing: number;
  dps: number;
  hits: number;
  critical_hits: number;
  critical_samples?: number;
  critical_rate: number;
  active_seconds: number;
  incoming_damage: number;
  skills: ReplaySkill[];
  skills_unattributed?: ReplaySkill[];
  skills_attributed?: ReplaySkill[];
  skills_unattributed_burning_hand_merged?: ReplaySkill[];
  skills_attributed_burning_hand_merged?: ReplaySkill[];
  damage_series: Array<[number, number]>;
}
export interface DeathReportEvent {
  timestamp_ms: number;
  seconds: number;
  relative_seconds: number;
  source_id: string;
  source_name: string;
  job: string;
  skill_id: string;
  skill_name: string;
  amount?: number;
  healing?: boolean;
  target_id?: string;
  target_name?: string;
  target_job?: string;
}

export interface DeathReportHealthPoint {
  timestamp_ms: number;
  seconds: number;
  relative_seconds: number;
  current_hp: number;
  hp_change: number | null;
  percent: number | null;
}

export interface DeathReport {
  ordinal: number;
  actor_death_ordinal: number;
  death_timestamp_ms: number;
  death_seconds: number;
  before_seconds: number;
  after_seconds: number;
  actor_id: string;
  name: string;
  job: string;
  max_hp: number;
  max_hp_source: string;
  health_points: DeathReportHealthPoint[];
  previous_positive?: DeathReportHealthPoint | null;
  next_positive?: DeathReportHealthPoint | null;
  incoming_damage: DeathReportEvent[];
  actor_casts: DeathReportEvent[];
  previous_cast?: DeathReportEvent | null;
  next_cast?: DeathReportEvent | null;
  healing: DeathReportEvent[];
}

export interface ReplayPayload {
  castability_windows_by_source?: Record<string, Array<[number, number]>>;
  composition_pending?: boolean;
  composition_revision?: number;
  additional_damage_attribution?: {
    available: boolean;
    algorithmVersion?: string;
    mode?: string;
    movedHits?: number;
    movedDamage?: number;
    classifiedHits?: number;
    classifiedDamage?: number;
    mergedHits?: number;
    mergedDamage?: number;
    statusCounts?: Record<string, number>;
    note?: string;
    reason?: string;
  };
  instance_completion?: {
    version: number;
    status: "cleared" | "not_cleared" | "unknown";
    cleared: boolean | null;
    label: string;
    reason: string;
    server_elapsed_seconds?: number;
    settlement_timestamp_ms?: number;
    clear_table_id?: number;
    countdown_timestamp_ms?: number;
    trigger_index?: number;
    trigger_grid?: string;
    trigger_name?: string;
    trigger_map_id?: number;
  };
  elapsed_seconds: number;
  dps_elapsed_seconds: number;
  total_damage: number;
  total_healing: number;
  players: ReplayPlayer[];
  damage_series: Array<[number, number]>;
  damage_targets: Record<string, unknown>[];
  boss_encounters: Record<string, unknown>[];
  boss_effect_views: Array<{
    key: string;
    name: string;
    target_ids: string[];
    series: Array<[string, Array<[number, number]>]>;
    freeze_coverage?: number;
    freeze_covered_seconds?: number;
    freeze_layers?: Array<{
      layer: number;
      label: string;
      parameters: string;
      intervals: Array<[number, number]>;
      refresh_count: number;
      covered_seconds: number;
      coverage: number;
    }>;
    output_windows?: Array<[number, number]>;
    castability_windows?: Array<[number, number]>;
    output_seconds?: number;
  }>;
  boss_effects: Record<string, unknown>[];
  boss_effect_events: Record<string, unknown>[];
  boss_effect_summary: Array<[string, number]>;
  boss_effect_series: Array<[string, Array<[number, number]>]>;
  boss_effect_current_seconds: number;
  player_buffs: Record<string, unknown>[];
  player_buff_events: Record<string, unknown>[];
  player_buff_series: Record<string, unknown>[];
  incoming_damage_events: Record<string, unknown>[];
  skill_cast_events: Record<string, unknown>[];
  boss_action_events: Record<string, unknown>[];
  player_buff_clear_events?: Record<string, unknown>[];
  death_reports?: DeathReport[];
  boss_hp_tracks: Record<string, unknown>[];
  boss_hp_curves: Record<string, unknown>[];
  party_members: Record<string, unknown>[];
  player_profiles: Record<string, unknown>[];
  profile_panels: Record<string, {
    state: string;
    targetId?: string;
    job?: string;
    message?: string;
    rows?: Array<{
      key: string;
      label: string;
      format: "number" | "percent";
      confirmed: number;
      estimated: number;
    }>;
    coverage?: { mapped: number; total: number; percent: number };
    actualItems?: {
      records: number;
      mapped: number;
      details?: Array<{
        slot: string;
        name: string;
        variant?: string;
        mappedName: string;
        serverText: string;
        simulatorText: string;
        status: string;
      }>;
    };
    setBonuses?: {
      sets: number;
      details: Array<{
        scope: string;
        setId: number;
        count: number;
        members: string[];
        appliedTiers: number[];
        attributes: Record<string, number>;
        slot: string;
        name: string;
        mappedName: string;
        serverText: string;
        status: string;
      }>;
    };
    calculation?: {
      engine: string;
      sameAsBuildPage: boolean;
      order: string[];
      excluded: string[];
    };
    jewelry?: {
      records: number;
      mapped: number;
      variantNotes: string[];
      details?: Array<{
        slot: string;
        name: string;
        variant: string;
        mappedName: string;
        mappedVariant: string;
        serverText: string;
        simulatorText: string;
        status: string;
      }>;
    };
    engravings?: {
      records: number;
      mapped: number;
      details?: Array<{
        slot: string;
        serverText: string;
        mappedExtra: string;
        mappedRank: string;
        mappedValue: number;
        status: string;
      }>;
    };
    cardMastery?: Array<{ name: string; attr: string; level: number }>;
    accountDefaults?: {
      collectionLevel: number;
      mastery: Array<{ name: string; level: number }>;
      source: "team-default" | "build";
    };
    cardEstimate?: {
      cardsSource: "observed" | "estimated";
      observedCards: number;
      totalCards: number;
      estimatedGrade: string;
      estimatedMasteryLevel: number;
      quality: number;
      initialEstimateMinRatio: number;
      initialEstimateMaxRatio: number;
      initialEstimateExpectedRatio: number;
      rows: Array<{
        name: string;
        attr: string;
        initial: number;
        initialMin: number;
        initialMax: number;
        level: number;
        boosted: number;
        levelSource: "observed" | "estimated" | "fixed";
      }>;
    };
    passiveBuffs?: Array<{
      skill: string;
      level: string;
      effect: string;
    }>;
    notes?: string[];
  }>;
  sourceFile: string;
  sourceHash: string;
  dungeon_map?: {
    map_id: number;
    map_name: string;
    nest_name?: string;
    official_name: string;
    content_kind: string;
    observed_at: number;
    layout: string;
    confidence?: string;
    template_id?: number;
    inferred?: boolean;
  } | null;
  dungeon_map_history?: Array<{
    map_id: number;
    map_name: string;
    nest_name?: string;
    official_name: string;
    content_kind: string;
    observed_at: number;
    layout: string;
    confidence?: string;
    template_id?: number;
    inferred?: boolean;
  }>;
  [key: string]: unknown;
}

/** [timeSeconds, worldX, worldY, startsNewPath]. */
export type MovementPointTuple = [number, number, number, 0 | 1];

export interface MovementTrack {
  actorId: string;
  profileId?: string;
  runtimeAliases?: string[];
  name: string;
  job: string;
  kind: "player" | "boss" | "entity" | string;
  isLocal: boolean;
  pointCount: number;
  points: MovementPointTuple[];
  overviewPoints?: MovementPointTuple[];
  samplingMode?: "movement" | "event";
  /** Confirmed entity lifetime bounds; absent means keep the last position. */
  visibleFrom?: number;
  visibleUntil?: number;
}

export interface MovementSegment {
  key: string;
  mapId: number;
  mapType: 3;
  mapName: string;
  officialName: string;
  contentKind: string;
  mapConfidence?: string;
  mapAssignment?: string;
  mapEvidenceTier?: "server_exact" | "route_inferred" | "coordinate_plane";
  mapEvidenceLabel?: string;
  startSeconds: number;
  endSeconds: number;
  pointCount: number;
  trackCount: number;
  bounds: {
    minX: number;
    maxX: number;
    minY: number;
    maxY: number;
  } | null;
  map: {
    available: boolean;
    assetAvailable?: boolean;
    fallbackReason?: string;
    gridDirectory?: string;
    imageWidth?: number;
    imageHeight?: number;
    worldWidth?: number;
    worldHeight?: number;
    cellSize?: number;
    coordinateWidth?: number;
    coordinateHeight?: number;
    coordinateMinX?: number;
    coordinateMinY?: number;
    swapXY?: boolean;
    invertX?: boolean;
    invertY?: boolean;
  };
  diagnosticTrackCount?: number;
  diagnosticPointCount?: number;
  diagnosticTracks?: Array<{
    actorId: string;
    reason?: "unconfirmed_local_candidate" | "unconfirmed_entity" | string;
    pointCount: number;
    firstSeconds: number;
    lastSeconds: number;
    sourceLayouts: string[];
  }>;
  projection?: {
    status: "normal" | "review" | "fallback" | "coordinate_plane" | string;
    sampleCount: number;
    outOfBoundsCount: number;
    outOfBoundsRate: number;
  };
  tracks: MovementTrack[];
}

export interface MovementPayload {
  schema: "movement-ledger-v1";
  state: "ready" | "no_movement_evidence";
  message: string;
  sourceHash: string;
  sourceFile: string;
  durationSeconds: number;
  segmentCount: number;
  trackCount: number;
  pointCount: number;
  coordinateSystem: {
    axes: ["x", "y"];
    verticalIncluded: false;
    wireEncoding: "client-float24-ieee754-truncated";
    wireBytesPerComponent: 3;
    discardedMantissaBits: 5;
    mapYInverted: boolean;
  };
  segments: MovementSegment[];
  diagnostics: Record<string, unknown>;
}

export interface TeammateInfoStatus {
  state: "listening" | "ready" | "unavailable";
  revision: number;
  /** Changes when the same decoded body finishes its background panel projection. */
  presentationRevision?: number;
  inspectionKey?: string;
  name?: string;
  job?: string;
  gameId?: string;
  profileId?: string;
  identityResolved?: boolean;
  /** Stable identity is sufficient for score history even without equipment. */
  historyReady?: boolean;
  /** The encrypted inspect body was decoded; panelState reports projection readiness. */
  bodyDecoded?: boolean;
  panelState?: "waiting_body" | "calculating" | "ready" | "error";
  projectionMs?: number;
  packetRequestEpochMs?: number;
  decodedPublishedEpochMs?: number;
  statusPublishedEpochMs?: number;
  message?: string;
  physicalAttackMin?: number;
  physicalAttackMax?: number;
  magicAttackMin?: number;
  magicAttackMax?: number;
  critPercent?: number;
  critDamagePercent?: number;
  finalPercent?: number;
  element?: string;
  elementAttackPercent?: number;
}

export interface CharacterScoreHistoryItem {
  historyId: string;
  characterName: string;
  /** Present when the cloud has masked the public name with the row's session profile ID. */
  profileId?: string;
  softwareHidden?: boolean;
  job: string;
  nest: string;
  recordedAt: string;
  outputSeconds?: number | null;
  completionStatus?: "cleared" | "not_cleared" | "unknown";
  actualDps?: number | null;
  totalScore: number;
  outputScore: number;
  supportScore: number;
  specialtyScore: number | null;
  survivalPenalty: number;
  theoryDamageCompletion: number | null;
  score_schema_version?: number | null;
  benchmarkSampleCount?: number;
  benchmarkScoreMetrics?: Record<string, {
    mean?: number | null;
    sample_count?: number;
    retained_count?: number;
    percentiles?: number[];
  }>;
}

export interface ScoreHistoryTeamMember {
  historyId: string;
  sampleOrdinal?: number;
  isCurrent?: boolean;
  characterName: string;
  profileId?: string;
  softwareHidden?: boolean;
  job: string;
  actualDps?: number | null;
  totalScore: number;
  outputScore: number;
  supportScore: number;
  specialtyScore: number | null;
  survivalPenalty: number;
}

export interface CharacterScoreHistoryStatus {
  state: "ready" | "empty" | "hidden" | "unavailable" | "error" | "loading";
  message: string;
  characterName: string;
  profileId?: string;
  total: number | null;
  offset?: number;
  limit: number;
  cursor: string;
  nextCursor: string;
  hasMore: boolean;
  nest: string;
  nests: string[];
  scoreSchemaVersions: number[];
  facetsDeferred?: boolean;
  items: CharacterScoreHistoryItem[];
}

export interface ProfileScoreVisibilityStatus {
  state: "synced" | "pending_identity" | "unavailable" | "error";
  message: string;
  profileId: string;
  characterName: string;
  overlayHidden: boolean;
  softwareHidden: boolean;
}

export type ProfileScoreOverlayVisibilityStatus = ProfileScoreVisibilityStatus;

export interface ProfessionScoreHistoryStatus {
  state: "ready" | "empty" | "unavailable" | "error" | "loading";
  message: string;
  job: string;
  total: number | null;
  offset?: number;
  limit: number;
  cursor: string;
  nextCursor: string;
  hasMore: boolean;
  nests: string[];
  scoreSchemaVersions: number[];
  facetsDeferred?: boolean;
  items: CharacterScoreHistoryItem[];
}

export interface DatabaseRankingFacet<T extends string | number = string> {
  value: T;
  count: number;
}

export interface DatabaseProfessionRankingRow {
  job: string;
  recordCount: number;
  captureCount: number;
  identifiedRecordCount: number;
  identityCoverage: number;
  characterCount: number;
  dpsCharacterCount: number;
  totalScoreP25: number | null;
  totalScoreMedian: number | null;
  totalScoreP75: number | null;
  totalScoreP90: number | null;
  outputScoreP25: number | null;
  outputScoreMedian: number | null;
  outputScoreP75: number | null;
  outputScoreP90: number | null;
  supportScoreP25: number | null;
  supportScoreMedian: number | null;
  supportScoreP75: number | null;
  supportScoreP90: number | null;
  specialtyScoreP25: number | null;
  specialtyScoreMedian: number | null;
  specialtyScoreP75: number | null;
  specialtyScoreP90: number | null;
  survivalPenaltyMedian: number | null;
  theoryDamageCompletionMedian: number | null;
  theoryDamageCompletionP90: number | null;
  actualDpsP25: number | null;
  actualDpsMedian: number | null;
  actualDpsP75: number | null;
  actualDpsP90: number | null;
  stability: number | null;
  clearCount: number;
  completionKnownCount: number;
  clearRate: number | null;
  clearTimeMedian: number | null;
  clearTimeP90: number | null;
  latestRecordedAt: string;
}

export interface DatabaseSkillRankingRow {
  metrics?: Record<string, {
    median: number | null; p90: number | null; sampleCount: number;
    examples?: Array<{ historyId: string; job: string; nest: string; recordedAt: string;
      characterName?: string; softwareHidden?: boolean; actualDps?: number | null; clearSeconds?: number | null;
      scoreSchemaVersion: number; value: number; percentile: "median" | "p90" }>;
  }>;
  job: string;
  skillName: string;
  highValue: boolean;
  characterCount: number;
  recordCount: number;
  hitCompletionMedian: number | null;
  hitCompletionP90: number | null;
  powerCompletionMedian: number | null;
  powerCompletionP90: number | null;
  actualPowerMedian: number | null;
  theoryPowerMedian: number | null;
  stability: number | null;
}

export interface DatabaseSupportRankingRow {
  job: string;
  skillName: string;
  effectKey: string;
  kind: string;
  characterCount: number;
  recordCount: number;
  actualCoverageMedian: number | null;
  actualCoverageP90?: number | null;
  completionMedian: number | null;
  completionP90?: number | null;
  actualContributionMedian: number | null;
  actualContributionP90: number | null;
  allProfessionContributionMedian?: number | null;
  allProfessionContributionP90?: number | null;
  allProfessionCharacterCount?: number;
  stability: number | null;
}

export interface DatabaseSupportProfessionRankingRow {
  job: string;
  characterCount: number;
  recordCount: number;
  totalGainMedian: number | null;
  totalGainP90: number | null;
  totalCoverageMedian: number | null;
  stability: number | null;
}

export interface DatabaseTeamRankingRow {
  composition: string;
  jobs: string[];
  memberCount: number;
  captureCount: number;
  clearCount: number;
  completionKnownCount: number;
  clearRate: number | null;
  totalDpsMedian: number | null;
  totalDpsP90: number | null;
  averageScoreMedian: number | null;
  averageScoreP90: number | null;
  supportScoreMedian: number | null;
  clearTimeMedian: number | null;
  clearTimeP90: number | null;
  stability: number | null;
}

export interface DatabaseHistogramRow {
  job: string;
  metric: "totalScore" | "outputScore" | "supportScore" | "actualDps";
  sampleCount: number;
  bins: Array<{ lower: number; upper: number; count: number }>;
}

export interface DatabaseTeamCaptureRow {
  captureHash: string;
  historyId: string;
  job: string;
  nest: string;
  recordedAt: string;
  teamDps: number | null;
  memberCount: number;
  completionStatus: string;
}

export interface BossDebuffArchive {
  v: 1;
  extent: number;
  effects: Record<string, unknown>[];
  series: Array<[string, Array<[number, number]>]>;
  views: ReplayPayload["boss_effect_views"];
  phases: Record<string, unknown>[];
}

export interface DatabaseMyPositionRow {
  profileId: string;
  characterName?: string;
  job: string;
  recordCount: number;
  characterCount: number;
  totalScoreMedian: number | null;
  outputScoreMedian: number | null;
  supportScoreMedian: number | null;
  actualDpsMedian: number | null;
  totalPercentile: number | null;
  outputPercentile: number | null;
  supportPercentile: number | null;
  dpsPercentile: number | null;
}

export interface DatabaseRankingSectionStatus<T> {
  state: "ready" | "empty" | "unavailable" | "error" | "loading";
  message: string;
  generatedAt: string;
  section?: string;
  rows: T[];
  professionRows?: DatabaseSupportProfessionRankingRow[];
}

export interface DatabaseProfessionRankingStatus {
  state: "ready" | "empty" | "unavailable" | "error" | "loading";
  message: string;
  generatedAt: string;
  filters: {
    nest: string;
    scoreSchemaVersions: number[];
    completionStatus?: string;
    recordedFrom?: string;
    recordedTo?: string;
    profileId?: string;
    characterName?: string;
    resolvedProfileId?: string;
  };
  facets: {
    nests: Array<DatabaseRankingFacet<string>>;
    scoreSchemaVersions: Array<DatabaseRankingFacet<number>>;
  };
  summary: {
    recordCount?: number;
    captureCount?: number;
    identifiedRecordCount?: number;
    characterCount?: number;
    professionCharacterCount?: number;
    professionCount?: number;
    clearCount?: number;
    completionKnownCount?: number;
    clearRate?: number | null;
    clearTimeMedian?: number | null;
    latestRecordedAt?: string;
  };
  rows: DatabaseProfessionRankingRow[];
  myPositions?: DatabaseMyPositionRow[];
  legacyFallback?: boolean;
}

export interface CharacterScoreHistoryDetailStatus {
  state: "ready" | "empty" | "unavailable" | "error" | "loading";
  message: string;
  characterName: string;
  detail: ({
    historyId: string;
    characterName: string;
    profileId?: string;
    softwareHidden?: boolean;
    job: string;
    nest: string;
    recordedAt: string;
    outputSeconds?: number | null;
    completionStatus?: "cleared" | "not_cleared" | "unknown";
    actualDps?: number | null;
    totalScore: number;
    outputScore: number;
    supportScore: number;
    specialtyScore: number | null;
    survivalPenalty: number;
    theoryDamageCompletion: number | null;
    skillDetails: Record<string, unknown>[];
    supportDetails: Record<string, unknown>[];
    specialtyDetail: Record<string, unknown>;
    survivalDetail: Record<string, unknown>;
    compactTimeline?: Record<string, unknown> | null;
    bossDebuffs?: BossDebuffArchive | null;
    bossDebuffsMessage?: string;
  damageOverview?: Record<string, any> | null;
  damageOverviewMessage?: string;
    notes: string[];
    teamMembers?: ScoreHistoryTeamMember[];
    teamRosterAvailable?: boolean;
    teamRosterMessage?: string;
  }) | null;
}

export type ProfessionScoreHistoryDetailStatus = Omit<CharacterScoreHistoryDetailStatus, "characterName"> & {
  job: string;
};

export interface ScoreSyncSettings {
  adminPasswordConfigured: boolean;
  enabled: boolean;
  captureFileUploadEnabled: boolean | null;
  endpoint: string;
  functionUrl: string;
  tokenConfigured: boolean;
  installationId: string;
  ready: boolean;
  pendingCaptures: number;
  pendingCaptureFiles: number;
  queuePath: string;
  running: boolean;
  lastOk: boolean | null;
  message: string;
  locked?: boolean;
}

export interface AnonymousUsageDay {
  day: string;
  activeUsers: number;
}

export interface AnonymousUsageVersion {
  version: string;
  installations: number;
  activeToday: number;
}

export interface AnonymousUsageQuarterHour {
  bucketStart: string;
  label: string;
  activeUsers: number;
}

export interface AnonymousUsageEventTotals {
  launches: number;
  captures: number;
  scores: number;
  updates: number;
  crashes: number;
}

export interface AnonymousUsageSuccessRate {
  attempts: number;
  successes: number;
  failures: number;
  rate: number | null;
}

export interface AnonymousUsageSuccessRates {
  capture: AnonymousUsageSuccessRate;
  score: AnonymousUsageSuccessRate;
  update: AnonymousUsageSuccessRate;
}

export interface AnonymousUsageStatistics {
  timezone: string;
  generatedAt: string;
  daysRequested: number;
  totalInstallations: number;
  activeToday: number;
  active7Days: number;
  active30Days: number;
  curve: AnonymousUsageDay[];
  dau30Curve?: AnonymousUsageDay[];
  active24hCurve?: AnonymousUsageQuarterHour[];
  eventsToday?: AnonymousUsageEventTotals;
  successRates?: AnonymousUsageSuccessRates;
  versions: AnonymousUsageVersion[];
}

export interface CaptureStatus {
  state: "idle" | "starting" | "running" | "stopping" | "stopped" | "error";
  status: string;
  error: string;
  sourceFile: string;
  process: string;
  pid: number;
  udpPorts: number[];
  tcpPorts: number[];
  adapterCount?: number;
  acceleratorAdapterCount?: number;
  acceleratorDiscovery?: boolean;
  startDiagnostics?: Record<string, number | boolean>;
  scoreReady?: boolean;
  scoreSourceHash?: string;
  autoScoreJobId?: string;
  autoScoreState?: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  autoScoreProgress?: number;
  autoScoreStatus?: string;
  autoScoreError?: string;
  pageReady?: boolean;
  npcapInstalled: boolean;
  autoEnabled: boolean;
  autoStage: "off" | "waiting_game" | "waiting_ports" | "monitoring" | "confirmed" | "capturing" | "paused" | "error";
  autoStatus: string;
  nestKey?: string;
  nestLabel?: string;
  nestName?: string;
  nestConfidence?: string;
  nestTemplateId?: number;
  nestObservedAt?: number;
  nestMapId?: number;
  nestMapName?: string;
  nestMapLayout?: string;
  nestMapActive?: boolean;
  unconfiguredMapIds?: number[];
  snapshot?: ReplayPayload | null;
  teammateInfoRevision?: number;
  teammatePresentationRevision?: number;
  teammateProfiles?: Record<string, unknown>[];
  teammateProfilePanels?: ReplayPayload["profile_panels"];
}

export interface LiveTimelineStatus {
  voiceEvents?: Array<{ sequence: number; kind?: string; skill: number; source: string; timestamp: number; observedAt: number; templateId?: number; hpPercent?: number; threshold?: number; confirmedEndTimestamp?: number; recipients?: Array<{ id: string; job: string }> }>;
  state: CaptureStatus["state"];
  sessionId: string;
  elapsedSeconds: number;
  oldestSequence: number;
  latestSequence: number;
  reset: boolean;
  windowSeconds: number;
  maxEvents: number;
  events: Array<Record<string, unknown>>;
}

export interface BuffOverlayStatus {
  endedBossIds?: string[];
  /** 悬浮窗前两行的身份信息：副本名与当前 Boss 名，只用于展示。 */
  /** 当前场景名：可能是副本，也可能是城镇/庭院。抓到了就不该显示未识别。 */
  sceneName?: string;
  /** nest / hub / map / unknown，决定第一行前缀与后缀。 */
  sceneKind?: string;
  nestName?: string;
  nestLabel?: string;
  nestKey?: string;
  nestConfidence?: string;
  bossName?: string;
  bossTemplateId?: number;
  bossTargetId?: string;
  bossHealth?: Array<{ instanceId: string; targetId: string; partId?: number; name: string; percent: number | null;
    currentHp: number | null; maxHp: number | null; confidence: string; updatedSeconds: number }>;
  selfAttackTarget?: { targetId: string; timestampMs: number } | null;
  state: CaptureStatus["state"];
  status: string;
  elapsedSeconds: number;
  overlayRevision?: number;
  buffStateRevision?: number;
  latency?: {
    captureToDecodeMs?: number;
    decoderQueueDelayMs?: number;
    pendingRawFrames?: number;
    pendingDecodedEvents?: number;
    thresholdExceeded?: boolean;
  };
  selfSourceId: string;
  players: Array<{
    source_id: string;
    job: string;
    name: string;
    damage: number;
    dps: number;
    damage_series: Array<[number, number]>;
    attack_types: Array<"physical" | "magic">;
    elements: Array<"fire" | "water" | "light" | "dark">;
  }>;
  buffs: Array<Record<string, unknown>>;
  /** Latest received lifetime, including ended Buffs; absence is unknown. */
  buffObservations?: Array<{
    target_id: string;
    catalog_key: string;
    end_seconds: number;
    permanent: boolean;
  }>;
  debuffs: Array<Record<string, unknown>>;
  cooldowns: Array<{
    key: string;
    catalog_key: string;
    skill_name: string;
    source_id: string;
    source_job: string;
    cast_seconds: number;
    base_cooldown_seconds?: number;
    cooldown_seconds: number;
    cooldown_source?: string;
    cooldown_source_label?: string;
    intrinsic_cooldown_multiplier?: number;
    cooldown_glyph_percent?: number;
    cooldown_refund_seconds?: number;
    cooldown_refund_events?: number;
    cooldown_refund_evidence?: string;
    ready_seconds: number;
    remaining_seconds: number;
  }>;
  summons: Array<{
    key: string;
    catalog_key: string;
    skill_id: string;
    skill_name: string;
    source_id: string;
    source_name: string;
    source_job: string;
    start_seconds: number;
    end_seconds: number;
    duration_seconds: number;
    remaining_seconds: number;
    detection_source: string;
  }>;
  playerHealth: Array<{
    target_id: string;
    target_name: string;
    target_job: string;
    current_hp: number;
    max_hp: number;
    current_percent: number | null;
    exact_percent: boolean;
    max_hp_source: string;
  }>;
  buffClearRevision?: number;
  buffClearEvents?: Array<{
    key: string;
    revision: number;
    timestamp_seconds: number;
    source_id: string;
    source_name: string;
    skill_id: string;
    skill_name: string;
    targets: Array<{
      target_id: string;
      target_name: string;
      target_job: string;
    }>;
  }>;
  buffOptions: Array<{
    key: string;
    skill: string;
    label: string;
    category: string;
    scope: "team" | "personal";
    fullDurationTeam: boolean;
    provider: string;
    displayOrder: number;
    providerOrder: number;
    iconIndex?: number;
    cooldownTrackable?: boolean;
  }>;
  summonOptions: Array<{
    key: string;
    skill: string;
    durationSeconds: number;
    maxInstances: number;
    provider: string;
  }>;
}

export interface BuffOverlayChangeStatus {
  changed: boolean;
  buffStateRevision: number;
}

export interface BuffOverlaySettings {
  /** Visual migration marker. Version 2 defaults overlay backgrounds to 50% opacity. */
  overlaySurfaceVersion: number;
  /** Merge persistent status panels and suspend empty transparent windows. */
  selectedBuffs: string[];
  buffOrder: string[];
  debuffOrder: Array<"damage" | "resistance" | "defense" | "critical" | "freeze">;
  /** Unified order: `buff:<catalog key>` and `debuff:<category>`. */
  statusOrder: string[];
  showBuffOverlay: boolean;
  showDebuffOverlay: boolean;
  showSkillCountdownOverlay: boolean;
  selectedSkillCountdowns: string[];
  /** 分边副本（黑龙硬核／沙龙硬核）只显示本机正在打的那只 Boss 的倒计时。 */
  splitBossOnlyCurrent: boolean;
  showDamage: boolean;
  showResistance: boolean;
  showCritical: boolean;
  showDefense: boolean;
  showFreeze: boolean;
  damageMode: "auto" | "physical" | "magic" | "both";
  resistanceMode: "auto" | "fire" | "water" | "light" | "dark" | "all";
  defenseMode: "auto" | "physical" | "magic" | "both";
  /** Skill breakdown for amplification and elemental-resistance totals. */
  debuffBreakdownMode: "summary" | "skills" | "summary_and_skills";
  /** Buff rows and icons are independently selectable from Debuff skills. */
  buffLayoutMode: "rows" | "icons";
  /** Rows preserve Debuff details; icons compact concrete contribution skills. */
  statusLayoutMode: "rows" | "icons";
  statusIconSize: number;
  statusIconBackgroundColor: string;
  statusIconBackgroundOpacity: number;
  statusIconNameColor: string;
  statusIconTimeColor: string;
  statusIconNameFontSize: number;
  statusIconTimeFontSize: number;
  /** Optional per-skill overrides keyed by the stable Buff catalogue key. */
  statusIconBackgroundColors: Record<string, string>;
  statusIconNameColors: Record<string, string>;
  statusIconTimeColors: Record<string, string>;
  /** Per-skill row colour override for the Buff/Debuff status rows. */
  effectColors: Record<string, string>;
  toneColors: Record<"speed" | "power" | "element" | "guard" | "critical" | "freeze" | "general", string>;
  columnWidth: number;
  maxRowsPerColumn: number;
  rowHeight: number;
  fontSize: number;
  gap: number;
  borderRadius: number;
  backgroundOpacity: number;
  showProvider: boolean;
  timeFormat: "decimal" | "subsecond";
}

export interface ProfileSyncItem {
  category: string;
  item: string;
  detected: string;
  mapped: string;
  status: "matched" | "unmatched" | "preserved";
  note: string;
}
export interface ProfileSyncPayload {
  state: "idle" | "listening" | "ready" | "applied" | "cancelled" | "error";
  message?: string;
  startedCapture?: boolean;
  source?: "self_profile_cache" | "live_capture" | string;
  cacheAgeSeconds?: number;
  captureReady?: boolean;
  captureState?: string;
  captureStatus?: string;
  capture?: {
    state?: string;
    status?: string;
    error?: string;
    npcapInstalled?: boolean;
  };
  profile?: {
    targetId: string;
    job: string;
    status: string;
    mappedItems: number;
    totalItems: number;
    cardMasteryCount?: number;
  };
  summary?: Array<{ category: string; matched: number; total: number }>;
  items?: ProfileSyncItem[];
  cardMastery?: Array<{ name: string; attr: string; level: number }>;
  build?: BuildPayload;
}

export interface ScoreSummary {
  sourceId: string;
  name: string;
  job: string;
  role: string;
  roleReason: string;
  grade: string;
  totalScore: number;
  outputScore: number;
  supportScore: number;
  specialtyScore: number | null;
  specialtyContribution?: number;
  survivalPenalty: number;
  outputWeight: number;
  supportWeight: number;
  specialtyWeight: number;
  specialtyLabel: string;
  theoryDamageCompletion: number;
  burstUsage: number;
  hasBurstWindows: boolean;
  coverageCompletion: number;
  contributionCompletion: number;
  actualStandardPower: number;
  theoryStandardPower: number;
  notes: string[];
}
export interface ScorePayload {
  scoreRuleVersion?: "v1" | "v2";
  bossPreset?: string;
  scoreKey: string;
  sourceHash: string;
  live?: boolean;
  liveRevision?: number;
  liveAnchor?: {
    elapsedSeconds: number;
    dpsElapsedSeconds: number;
    activeCombatSeconds?: number;
    outputWindows?: Array<[number, number]>;
    players: Array<{
      sourceId: string;
      damage: number;
      scoredDamage?: number;
      scoredSkillNames?: string[];
      scoredSkillIds?: string[];
      healing: number;
      incomingDamage: number;
    }>;
  } | null;
  bossTargetIds: string[];
  rows: ScoreSummary[];
  glyphsBySource?: Record<string, SkillGlyphSelection[]>;
  equipmentBySource?: Record<string, ScoringEquipmentRecord>;
  diagnostics: Record<string, number>;
  scoreSync?: ScoreSyncSettings;
}

export interface ScoringEquipmentRecord {
  schema: string;
  source: "standard_default" | "manual_override";
  build: BuildPayload;
}
export interface ScoreEquipmentPayload {
  sourceId: string;
  sourceHash: string;
  source: "standard_default" | "manual_override";
  build: BuildPayload;
  summary: Record<string, number>;
  preview?: Pick<BuildDetails, 'buffs' | 'resonance'>;
  options: ScoreEquipmentOptions;
}
export interface ScoreEquipmentOptions {
  equipmentTiers: Array<{ value: string; label: string; level: string }>;
  jewelryTiers: Array<{ value: string; label: string; level: string }>;
  glyphALevels: string[];
  glyphSLevels: string[];
  talismanGrades: string[];
  engravingGrades: string[];
  cardGrades: string[];
}
export interface UniformScoreEquipmentSettings {
  equipmentTier?: string;
  jewelryTier?: string;
  weaponEnhance?: number | string;
  armorEnhance?: number | string;
  glyphALevel?: string;
  glyphSLevel?: string;
  talismanGrade?: string;
  engravingGrade?: string;
  cardGrade?: string;
  cardBonusLevel?: number | string;
}
export interface ScoreEquipmentBatchPayload {
  sourceHash: string;
  players: Record<string, ScoreEquipmentPayload>;
  settings: UniformScoreEquipmentSettings;
}
