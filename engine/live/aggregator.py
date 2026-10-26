from __future__ import annotations

from types import SimpleNamespace
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Iterable, Mapping

from ..data_loader import DataStore, norm
from ..team_support_rules import team_effect_duration_override
from ..effect_stacking import EFFECT_STACKING_ENGINE, EffectInput
from .boss_recognizer import BossRecognizer, target_identity_key
from .boss_hp import BossHpLedger
from .damage_ledger import (
    AttributionLedgerResult,
    RawDamageLedger,
    SkillAttributionLedger,
)
from .additional_damage_triggers import additional_damage_composition_name
from .additional_damage_shadow import is_folded_additional_damage_event
from .freeze_engine import advance_freeze_hit_count
from .healing_attribution import (
    HealingAttributionLedger,
    HealingAttributionResult,
)
from .summon_presence import SummonPresenceLedger, summon_overlay_options
from .settlement import SettlementEvent, SettlementLedger
from .models import (
    BossActionEvent,
    BossEffectSnapshot,
    BossEffectViewSnapshot,
    BossSpawnEvent,
    BossHealthEvent,
    MonsterAppearanceEvent,
    BossStatusEvent,
    CombatEvent,
    CombatSnapshot,
    DamageTargetSnapshot,
    PlayerBuffSeriesSnapshot,
    PartyMemberEvent,
    PlayerHealthEvent,
    PlayerHealthPointSnapshot,
    PlayerHealthTrackSnapshot,
    PlayerLifecycleEvent,
    PlayerInspectEvent,
    PlayerSnapshot,
    StatusEventSnapshot,
    SkillCastEvent,
    SkillParameterEvent,
    SkillSnapshot,
    SummonRemovalEvent,
)
from .combat_core import resolve_combat_clock
from .mechanic_damage import is_mechanic_damage_skill
from .critical_stats import is_critical_rate_sample
from .buff_clear import player_buff_is_clear_immune
from .aura_state import AURA_SKILL_KEYS, is_inferred_aura_row
from .timeline_evidence import is_timeline_recovered_cast
from .shock_damage_identity import (
    is_shock_damage_identity_parameter,
    reconcile_shock_damage_identity,
)


# A direct-hit packet contains the server's resolved hit value, which can be
# much larger than the HP actually removed by an overkill.  Most captures do
# not include a verified player panel, so use the upper end of the expected
# v576 player-HP range until a real HP value is available from inspect data.
_FALLBACK_PLAYER_MAX_HP = 300_000


_MAX_PARTY_PLAYERS = 8
_DPS_IDLE_GAP_MS = 5_000
# Status history is part of scoring input, not merely a UI detail cache. Long
# raids can exceed the old 4k/8k caps well before the final Boss, which silently
# removed later buff/debuff windows and changed the score. Keep the complete
# history; individual views already filter it to their visible time range.
_MAX_STATUS_DETAIL_EVENTS: int | None = None
_MAX_BOSS_STATUS_DETAIL_EVENTS: int | None = None
_UNKNOWN_JOB_NAMES = {"", "未知", "未知职业"}
# The v576 decoder emits 0.65 for packet-backed party members whose runtime
# actor and profession are proven but whose name is still anonymous, and 0.95
# once a real name is proven. Both are useful actor-context evidence; the
# anonymous-name guard below keeps the 0.65 row from erasing a real identity.
_RELIABLE_PARTY_MEMBER_CONFIDENCE = 0.65
_DEFAULT_FREEZE_BREAK_HITS = 100
_ALCHEMIST_FREEZE_BREAK_HITS = 200
_OPEN_ENDED_STATUS_MS = (1 << 62)
_CHAKRA_CURE_SKILL_IDS = {"6302", "6605"}
# Verified from v576 monster skill descriptions. These skills remove positive
# effects from the party members they hit; legacy and hard-mode indices are
# both retained because Green Dragon records can contain either family.
_PLAYER_BUFF_CLEAR_BOSS_SKILL_IDS = frozenset(
    {
        31357,
        31503,
        31553,
        31554,
        31556,
        31560,
        31581,
        31583,
        31584,
        31586,
        31592,
        31593,
        325599,
        325603,
        325604,
        325606,
        325610,
        325614,
        325616,
        325617,
        325619,
        325621,
        325622,
        325641,
        325642,
        325643,
    }
)
# v576 ``skilltable_character.dnt`` has only four entries with
# ``_Dissolvable = 1``. Boss clear-positive-effects packets leave these
# protected player effects active.
# Holy Hammer damage shares skill ID 3224 with the form buff. The attack cast
# itself is emitted from its server-authoritative three-component status
# record; neither the 3224 buff action nor a damage packet may create it.
_TRANSFORMED_BASIC_ATTACK_DAMAGE_NAMES: dict[str, str] = {
    "3224": "神圣之锤攻击",
}

# Crippling Punisher currently has no separately verified transformed-action
# packet in the calibration samples, so it retains the status-gated damage
# grouping until such evidence is available.
_DAMAGE_TRANSFORMED_BASIC_ATTACKS: dict[str, tuple[str, str, str]] = {
    "6212": ("致残惩罚者", "致残惩罚者攻击", "134400"),
}
_TRANSFORMED_ATTACK_DEDUPE_MS = 40

# Cooldown-refund statuses are factual server events, not simulated Hit
# counts. Hunter Instinct emits one 134c00 pulse with ``1701;3000`` whenever
# a qualifying hit refunds three seconds from Silver Hunter. Preserve the
# pulses in a compact accumulator before normal status coalescing removes
# their multiplicity.
_LIVE_COOLDOWN_REFUND_STATUS_SKILLS: dict[str, tuple[str, str]] = {
    "1958": ("1701", "银色猎人"),
}


def _overlay_skill_family_key(value: object) -> str:
    """Fold display variants so a team status can find its real cast."""

    return (
        "".join(str(value or "").split())
        .upper()
        .replace("ＥＸ", "EX")
        .replace("EX", "")
    )


def _composition_cast_name_key(value: object) -> str:
    """Fold harmless display differences when a cast and Hit use base/EX names."""

    return (
        "".join(str(value or "").split())
        .upper()
        .replace("ＥＸ", "EX")
        .removesuffix("EX")
    )


def _is_inferred_cast_evidence(packet_kind: object) -> bool:
    kind = str(packet_kind or "").strip().casefold()
    return "inferred" in kind or "recovered" in kind


def _is_player_buff_clear_immune(track: "_BossEffectTrack") -> bool:
    return player_buff_is_clear_immune(
        skill_id=track.skill_id,
        skill_name=track.skill_name,
        permanent=track.permanent,
    )


@lru_cache(maxsize=1)
def _player_job_stages() -> dict[str, int]:
    """Return profession specificity so late shared-skill packets cannot downgrade it."""

    store = DataStore.load()
    stages: dict[str, int] = {}
    for first_job in store.first_jobs():
        first_name = norm(first_job)
        if not first_name:
            continue
        stages[first_name] = max(stages.get(first_name, 0), 1)
        for second_job in store.second_jobs(first_name):
            second_name = norm(second_job)
            if second_name:
                stages[second_name] = max(stages.get(second_name, 0), 2)
    return stages


def _is_recognized_player_job(job: str) -> bool:
    """Reject monster/summon level labels masquerading as professions."""
    value = str(job or "").strip()
    if value in _UNKNOWN_JOB_NAMES:
        return False
    folded = value.casefold()
    if folded.startswith("lv") and folded[2:].strip().isdigit():
        return False
    if folded == "boss" or folded.startswith("boss "):
        return False
    return True


@lru_cache(maxsize=4096)
def _is_anonymous_actor_name(
    name: str,
    actor_id: str = "",
    job: str = "",
) -> bool:
    """Return whether *name* is only an actor/job placeholder.

    Runtime actor ids change after reconnects. Names such as
    ``角色 0x12345678`` are therefore useful packet diagnostics but must never
    replace a previously proven character name in the shared actor context.
    """

    value = str(name or "").strip()
    if not value:
        return True
    folded = value.casefold()
    actor_folded = str(actor_id or "").strip().casefold()
    job_folded = str(job or "").strip().casefold()
    compact = "".join(folded.split())
    return (
        bool(actor_folded and folded == actor_folded)
        or bool(job_folded and folded == job_folded)
        or compact.startswith("角色0x")
        or compact.startswith("actor0x")
    )


@lru_cache(maxsize=1024)
def _prefer_specific_player_job(previous: str, current: str) -> str:
    """Keep the most specific proven profession for one runtime actor."""

    old_job = str(previous or "").strip()
    incoming_job = str(current or "").strip()
    if not old_job:
        return incoming_job
    if not incoming_job:
        return old_job
    stages = _player_job_stages()
    if stages.get(incoming_job, 0) < stages.get(old_job, 0):
        return old_job
    return incoming_job


def _is_chakra_cure_channel_marker(event: BossStatusEvent) -> bool:
    """Return whether *event* is the caster-only meditation channel marker.

    Chakra Cure emits permanent-looking 135000/0.5 and 134900/0 statuses while
    meditation is active, in addition to the real two-second team refreshes.
    They are action-state markers, not attack buffs; keeping either in the
    ledger turns the caster's coverage into a false 100 percent.
    """
    if (
        str(event.skill_id) not in _CHAKRA_CURE_SKILL_IDS
        or not event.permanent
        or event.operation != "apply"
        or event.source_id != event.target_id
    ):
        return False
    try:
        marker_value = float(str(event.parameters or "").strip())
    except ValueError:
        return False
    return (
        event.effect_code == "135000"
        and abs(marker_value - 0.5) <= 1e-6
    ) or (
        event.effect_code == "134900"
        and abs(marker_value) <= 1e-6
    )


def _freeze_break_hit_limit(event: BossStatusEvent) -> int:
    """Return the configured hit durability used for inferred shatter.

    v576 freeze packets expose vectors such as ``1;100`` and ``0.6;100``.
    The trailing value alone does not distinguish the 200-hit alchemist
    rule, so profession mechanics take precedence over that fallback field.
    """
    if event.job == "炼金圣士" and event.skill_name in {
        "寒冰戳刺",
        "寒冰戳刺EX",
        "冰光束",
    }:
        return _ALCHEMIST_FREEZE_BREAK_HITS
    if event.job == "冰灵":
        return _DEFAULT_FREEZE_BREAK_HITS
    for token in reversed(str(event.parameters or "").split(";")):
        try:
            value = float(token.strip())
        except ValueError:
            continue
        if value.is_integer() and 1 <= value <= 10_000:
            return int(value)
    return _DEFAULT_FREEZE_BREAK_HITS


def _effect_value_signature(
    value: float | None, parameters: str
) -> tuple[str, object]:
    """Keep equal visible effects together without losing value changes."""
    if value is not None:
        return ("value", round(float(value), 6))
    return ("parameters", str(parameters or "").strip())


@dataclass(slots=True)
class _SkillTotals:
    name: str = ""
    damage: int = 0
    healing: int = 0
    hits: int = 0
    critical_hits: int = 0
    critical_samples: int = 0
    attribution: Counter[str] = field(default_factory=Counter)
    trigger_hits: Counter[tuple[str, str, str, str]] = field(
        default_factory=Counter
    )
    trigger_damage: Counter[tuple[str, str, str, str]] = field(
        default_factory=Counter
    )


@dataclass(slots=True)
class _PlayerTotals:
    name: str = ""
    job: str = ""
    damage: int = 0
    healing: int = 0
    hits: int = 0
    critical_hits: int = 0
    critical_samples: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    skills: dict[str, _SkillTotals] = field(default_factory=dict)
    damage_by_second: dict[int, int] = field(default_factory=lambda: defaultdict(int))


@dataclass(slots=True)
class _BossEffectTrack:
    effect_key: str
    effect_label: str
    value: float | None
    status_kind: str
    effect_code: str
    parameters: str
    stackable: bool
    stack_group: str
    target_id: str
    source_id: str
    source_name: str
    job: str
    skill_id: str
    skill_name: str
    intervals: list[list[int]] = field(default_factory=list)
    last_refresh_ms: int = 0
    refresh_count: int = 0
    break_hit_limit: int = 0
    hits_since_refresh: int = 0
    permanent: bool = False
    value_source: str = ""
    duration_source: str = ""
    effect_instance_id: int = 0


# Temporary statuses that change the live maximum-HP denominator. Permanent
# passives and guild attributes are already part of the character's baseline
# and must not remove every 08 6F sample from consideration.
_TRANSIENT_MAX_HP_BUFF_SKILL_NAMES = frozenset(
    {"幽灵之魂", "神圣化身", "九天之舞3阶"}
)
_MAX_HP_BUFF_LEAD_PADDING_MS = 250
_MAX_HP_BUFF_TAIL_PADDING_MS = 500


def _int_defaultdict() -> defaultdict[int, int]:
    """Pickle-safe factory for nested incremental counters."""
    return defaultdict(int)


# 分边判断只看最近 10 秒还在攻击某只 Boss 的人。
_ATTACK_PLAYER_WINDOW_MS = 10_000.0
# 更短的「此刻还在打」窗口，专供分边过滤用：攻击有明显的节奏间隔，4 秒足够
# 覆盖连续输出，又能把「AOE 偶尔蹭到对侧」排除掉。
_SPLIT_ATTACK_WINDOW_MS = 4_000.0
# 「我当前在打谁」的时效。``lastAttackTargets`` 是永久表（用于名字反查），
# 不能直接当「当前目标」：Boss 离场/阶段结束后没人再碰它，这条记录会永远
# 停在那里，于是悬浮窗 Boss 行、分边过滤都会跟着一个早就不在的 Boss 走
# （实测梦魇海龙 P2：本机最后目标是已经在 725s 飞走的海龙 4237003，它还被
# 当成「另一侧」把真正的冰眼/龙息过滤掉了）。15 秒覆盖跑位和读条间隔。
_ATTACK_TARGET_TTL_MS = 15_000.0
# 悬浮窗「Boss:」这一行的时效。比业务判定（15s）长一倍：播报必须立刻跟上换边，
# 但显示层跟着换会闪；30 秒够覆盖读条、被控和一次跑机制，超过就按「未识别」留空，
# 不回落队友目标（用户要求：不打怪时这一行只认自己打过的）。
_DISPLAY_TARGET_TTL_MS = 30_000.0
# 「我此刻主要在打谁」的加权窗口。分边/播报的白名单判定不能只看「最后命中的
# 是谁」：AOE 扫过一群怪时，最后命中的那个单位常常是杂兵或对侧 Boss。按窗口
# 内的伤害占比取最高者，才不会被一次溅射改掉主目标。
_ATTACK_SCORE_WINDOW_MS = 15_000.0
# 每个 (玩家, 目标) 按秒分桶，只留窗口内的桶；20 桶足够覆盖 15 秒 + 边界。
_ATTACK_SCORE_MAX_BUCKETS = 20


class CombatAggregator:
    def __init__(
        self,
        *,
        authoritative_boss_max_hp: bool = False,
        retain_source_events: bool = False,
    ) -> None:
        # v576 monster tables expose an unscaled template HP value. Dungeon
        # difficulty and party scaling are not present in that value, so the
        # live parser must not present it as an authoritative health bar.
        self._authoritative_boss_max_hp = bool(authoritative_boss_max_hp)
        # Live capture may need one exact, in-memory rebuild when a reconnect
        # runtime ID is proven after old events have already been aggregated.
        # This stores references only (not copies), and remains disabled for
        # replay/scoring aggregators so disk caches do not grow.
        self._retain_source_events = bool(retain_source_events)
        # Production skill composition is evidence-only. The former
        # recent-cast/time-window resolver remains readable for old cache and
        # regression fixtures, but is no longer used by the application.
        self._skill_attribution_ledger = SkillAttributionLedger(
            mode="evidence_v2"
        )
        self._healing_attribution_ledger = HealingAttributionLedger()
        self.reset()

    def observe_attack_target(
        self,
        source_id: str,
        target_id: str,
        timestamp_ms: float,
        target_name: str = "",
        amount: float = 0.0,
    ) -> None:
        """只记账：某玩家最后一次攻击的目标。

        轻量版（倒计时工具）不把伤害事件交给聚合器，因此没有 DPS 统计；但
        分边副本（黑龙硬核 / 沙龙硬核）要靠 ``lastAttackTargets[selfSourceId]``
        判断「我在打朱武还是卡基夫」。这条旁路只维护一个 dict，不累计伤害、
        不建曲线，成本可忽略。

        ``amount`` 为这次命中的伤害量，用于「15 秒内主要目标」的加权占比
        （见 ``_ATTACK_SCORE_WINDOW_MS``）。不回传时按 0 处理，此时主目标
        退化回「最近一次命中」，与旧行为一致。

        时间戳必须单调，迟到的包不能把目标改回去。
        """

        if not source_id or not target_id:
            return
        previous = self._overlay_last_attack_targets.get(source_id, {})
        if timestamp_ms >= float(previous.get("timestampMs", -1)):
            self._overlay_last_attack_targets[source_id] = {
                "targetId": target_id,
                "timestampMs": timestamp_ms,
            }
        if amount > 0:
            self._score_attack_target(source_id, target_id, timestamp_ms, amount)
        # 目标行的 first_ms/last_ms 只用于「谁最近在被打」的排序，不参与伤害
        # 统计。不更新它的话，轻量版里这些时间全是 0，身份识别就只能回退到
        # 「按 target_id 排序第一个」——Boss 行于是永远停在第一关。
        bounds = self._overlay_target_bounds.setdefault(
            target_id, [int(timestamp_ms), int(timestamp_ms)]
        )
        bounds[0] = min(bounds[0], int(timestamp_ms))
        bounds[1] = max(bounds[1], int(timestamp_ms))

        if target_name:
            self._overlay_target_names[target_id] = str(target_name).strip()
            if len(self._overlay_target_names) > 256:
                self._overlay_target_names = dict(
                    list(self._overlay_target_names.items())[-128:]
                )

        template = str(target_id).rsplit("@", 1)[-1].strip()
        if template.isdigit():
            players = self._overlay_attack_players.setdefault(template, {})
            players[source_id] = float(timestamp_ms)
            # 只保留窗口内的人：分边判断是「现在谁在打」，不是历史。
            cutoff = float(timestamp_ms) - _ATTACK_PLAYER_WINDOW_MS
            stale = [key for key, stamp in players.items() if stamp < cutoff]
            for key in stale:
                players.pop(key, None)
            if len(self._overlay_attack_players) > 64:
                self._overlay_attack_players = {
                    key: value
                    for key, value in self._overlay_attack_players.items()
                    if any(stamp >= cutoff for stamp in value.values())
                }

    def _score_attack_target(
        self,
        source_id: str,
        target_id: str,
        timestamp_ms: float,
        amount: float,
    ) -> None:
        """按 (玩家, 目标) 累计伤害，按秒分桶并裁掉窗口外的桶。

        只服务「本机 15 秒内在主要打谁」这一个判断。用分桶而不是逐事件流水，
        是因为一次团队副本里每秒可能有上百次命中，而这里只需要一个可比较的
        权重；按秒合并后每个 (玩家, 目标) 最多 20 个数字。
        """

        second = int(timestamp_ms // 1000)
        per_player = self._overlay_attack_scores.setdefault(source_id, {})
        buckets = per_player.setdefault(target_id, {})
        buckets[second] = buckets.get(second, 0.0) + float(amount)
        if len(buckets) <= _ATTACK_SCORE_MAX_BUCKETS:
            return
        cutoff = second - int(_ATTACK_SCORE_WINDOW_MS // 1000)
        for key in [key for key in buckets if key < cutoff]:
            buckets.pop(key, None)

    def attack_scores(self, timestamp_ms: float) -> dict[str, dict[str, float]]:
        """窗口内每个玩家对每个目标的累计伤害（按玩家分组）。

        供 ``focus hint`` 选「本机 15 秒内伤害占比最高的目标」。窗口按传入
        的战斗时钟计算，不用墙钟，回放和实时走同一条路径。
        """

        floor = float(timestamp_ms) - _ATTACK_SCORE_WINDOW_MS
        scores: dict[str, dict[str, float]] = {}
        for source, targets in self._overlay_attack_scores.items():
            rows: dict[str, float] = {}
            for target, buckets in targets.items():
                total = sum(
                    value for second, value in buckets.items()
                    if second * 1000.0 >= floor
                )
                if total > 0:
                    rows[target] = round(total, 3)
            if rows:
                scores[source] = rows
        return scores

    def reset(self) -> None:
        self._replay_snapshot = None
        # Keep this deliberately typed as object: the concrete live-event
        # union is owned by sources.py and importing it here would create a
        # module cycle. ingest() below remains the authoritative type gate.
        self._source_events: list[object] = []
        self._monster_appearance_events = deque(maxlen=128)
        self._monster_appearance_sequence = 0
        self._settlement_ledger = SettlementLedger()
        self._players: dict[str, _PlayerTotals] = {}
        self._actor_context: dict[str, tuple[str, str]] = {}
        self._target_max_hp: dict[str, int] = {}
        self._boss_hp_ledger = BossHpLedger(
            authoritative_max_hp=self._authoritative_boss_max_hp
        )
        self._damage_by_second: dict[int, int] = defaultdict(int)
        # The desktop overlay owns a deliberately small incremental view.
        # These counters are updated during ingest so publishing the current
        # Boss and short DPS curve never scans the append-only combat ledger.
        self._overlay_target_damage: Counter[str] = Counter()
        self._overlay_target_hits: Counter[str] = Counter()
        self._overlay_target_bounds: dict[str, list[int]] = {}
        self._overlay_last_attack_targets: dict[str, dict[str, object]] = {}
        # 模板 -> {玩家: 最后一次打它的时间}。分边副本要靠「打 A 的人和打 B 的人
        # 有没有重叠」来区分「左右两队各打一只」和「同一关两只都要打」。
        self._overlay_attack_players: dict[str, dict[str, float]] = {}
        # 玩家 -> 目标 -> 秒桶 -> 伤害。只用于「本机主要目标」的加权占比，
        # 与 _overlay_target_damage（全团、全时段累计）刻意分开。
        self._overlay_attack_scores: dict[str, dict[str, dict[int, float]]] = {}
        # target_id -> 名字。轻量版没有血量行/伤害累计行，「Boss:」这一行的名字
        # 只能从伤害事件里带出来（CombatEvent.target_name）。
        self._overlay_target_names: dict[str, str] = {}
        self._boss_output_window_cache_signature: tuple[object, ...] = ()
        self._boss_output_window_cache: dict[
            tuple[str, str], tuple[tuple[int, int], ...]
        ] = {}
        self._overlay_player_damage_by_second: dict[
            str, dict[int, int]
        ] = defaultdict(_int_defaultdict)
        self._overlay_incoming_damage: Counter[str] = Counter()
        self._overlay_player_health: dict[str, dict[str, object]] = {}
        self._overlay_protocol_max_hp: dict[str, int] = defaultdict(int)
        self._overlay_unbuffed_hp_peak: dict[str, int] = defaultdict(int)
        self._boss_effects: dict[tuple[object, ...], _BossEffectTrack] = {}
        self._freeze_tracks_by_target: dict[
            str, list[_BossEffectTrack]
        ] = defaultdict(list)
        self._boss_status_events: list[BossStatusEvent] = []
        # Index only statuses tentatively filed as Boss states. Identity
        # confirmation touches this actor's rows once, never the damage ledger.
        self._boss_status_indexes_by_target: dict[str, list[int]] = defaultdict(list)
        self._confirmed_player_ids: set[str] = set()
        self._authoritative_boss_ids: set[str] = set()
        self._player_buffs: dict[tuple[object, ...], _BossEffectTrack] = {}
        self._player_buff_events: list[BossStatusEvent] = []
        self._player_buff_indexes_by_target: dict[str, list[int]] = defaultdict(list)
        self._pending_player_statuses_by_source: dict[str, list[BossStatusEvent]] = defaultdict(list)
        self._player_buff_boundaries_by_target: dict[str, list[tuple[int, str]]] = defaultdict(list)
        self._party_members: dict[str, PartyMemberEvent] = {}
        self._player_health_events: list[PlayerHealthEvent] = []
        self._player_session_evidence: list[PlayerHealthEvent] = []
        self._player_lifecycle_events: list[PlayerLifecycleEvent] = []
        # 05 11 is retransmitted. Keep only real inactive/active edges so a
        # heartbeat cannot close Buffs or split HP/healing intervals again.
        self._player_lifecycle_active_by_target: dict[str, bool] = {}
        self._player_profiles: dict[str, PlayerInspectEvent] = {}
        self._raw_damage_ledger = RawDamageLedger()
        # Damage and healing are deliberately separate factual ledgers.  The
        # compatibility alias below is damage-only; healing is reconciled from
        # positive HP deltas and 08 6F states by HealingAttributionLedger.
        self._combat_events = self._raw_damage_ledger.events
        self._raw_healing_events: list[CombatEvent] = []
        # The display/attribution view may add a synthetic transformed-basic
        # attack label, but it must never rewrite the append-only raw ledger.
        self._display_combat_events: list[CombatEvent] = []
        self._incoming_damage_events: list[CombatEvent] = []
        self._skill_cast_events: list[SkillCastEvent] = []
        self._skill_parameter_events: list[SkillParameterEvent] = []
        # Only two reviewed response families can restore a generic shock
        # row's concrete skill identity.  Keep their compact evidence separate
        # so live snapshots do not rescan every skill parameter response.
        self._shock_damage_identity_parameters: list[SkillParameterEvent] = []
        self._shock_identity_ex_rehydrated = False
        # Key Buff CD is about the player who pressed the skill, not every
        # recipient-local status mirror emitted by the server.  Keep one tiny,
        # incremental cast index for skills that are later proven to be team
        # buffs, so the overlay never needs to rescan the full cast history.
        self._overlay_latest_skill_casts: dict[
            tuple[str, str], SkillCastEvent
        ] = {}
        self._overlay_player_buff_skill_keys: set[str] = set()
        self._overlay_team_buff_skill_keys: set[str] = set()
        self._overlay_cooldown_refunds: dict[
            tuple[str, str], dict[str, object]
        ] = {}
        # A small active-only ledger powers the independent summon overlay.
        # It never scans or copies the append-only damage/scoring history.
        self._summon_presence = SummonPresenceLedger()
        # Keep the same factual removal event for offline scoring.  The live
        # overlay consumes it incrementally above; this bounded-by-capture
        # ledger prevents the score path from trying to infer deaths again.
        self._summon_removal_events: list[SummonRemovalEvent] = []
        self._last_transformed_attack_ms: dict[tuple[str, str], int] = {}
        self._boss_action_events: list[BossActionEvent] = []
        self._player_buff_clear_events: list[BossActionEvent] = []
        # Explicit Boss clear-positive-effects events are a separate overlay
        # signal. Player death/lifecycle transitions also close active Buff
        # intervals, but must never be presented as "Buff 被清除".
        self._overlay_player_buff_clear_revision = 0
        self._overlay_player_buff_clear_events: list[dict[str, object]] = []
        # Player-Buff lifecycle changes have their own monotonic revision.  A
        # damage event must not wake every Buff WebView, while an apply,
        # refresh or removal should be visible without waiting for the normal
        # 500 ms overlay cadence.
        self._overlay_player_buff_state_revision = 0
        self._first_ms: int | None = None
        self._last_ms: int | None = None
        # Complete state/timeline clock. The legacy first/last pair above is
        # retained for DPS denominators; this pair also observes casts, HP,
        # lifecycle and Boss actions so live and replay state cannot lag them.
        self._state_first_ms: int | None = None
        self._state_last_ms: int | None = None
        self._revision = 0
        self._normalized_visible_signature: tuple[tuple[str, str, str], ...] = ()
        self._normalized_combat_event_count = 0
        self._normalized_skill_cast_event_count = 0
        self._normalized_combat_events: tuple[CombatEvent, ...] = ()
        self._normalized_skill_cast_events: tuple[SkillCastEvent, ...] = ()
        self._normalized_attribution_revision = -1
        self._attribution_revision = -1
        self._attribution_input_signature = (-1, -1, -1)
        self._attribution_result = AttributionLedgerResult(
            events=(),
            cast_events=(),
            instances=(),
            raw_damage=0,
            attributed_damage=0,
            raw_healing=0,
            attributed_healing=0,
            matched_hits=0,
            unresolved_hits=0,
            inferred_casts=0,
            reassigned_hits=0,
        )
        # Both the full live page and the compact in-combat projection must
        # consume the exact same V2 damage buckets.  Keep the materialized
        # buckets beside the reconciled ledger so repeated overlay refreshes
        # at one revision do not rescan every hit.
        self._damage_composition_revision = -1
        self._damage_composition_skills: dict[
            str, dict[str, _SkillTotals]
        ] = {}
        self._damage_composition_events: tuple[CombatEvent, ...] = ()
        self._healing_attribution_revision = -1
        self._healing_visible_signature: frozenset[str] = frozenset()
        self._healing_attribution_result = HealingAttributionResult(
            events=(),
            instances=(),
            raw_packet_total=0,
            factual_healing_total=0,
            attributed_healing_total=0,
            unattributed_healing_total=0,
            suppressed_overheal_total=0,
            matched_events=0,
            unattributed_events=0,
            ambiguous_events=0,
            method_counts=(),
        )

    @staticmethod
    def _profile_max_hp(profile: PlayerInspectEvent | None) -> int:
        if profile is None:
            return 0
        for key, value in (*profile.stats, *profile.fields):
            normalized = str(key).strip().casefold().replace(" ", "_")
            if normalized not in {
                "hp",
                "max_hp",
                "maximum_hp",
                "最大hp",
                "生命值",
                "最大生命值",
            }:
                continue
            try:
                parsed = int(float(str(value).replace(",", "").strip()))
            except ValueError:
                continue
            if parsed > 0:
                return parsed
        return 0

    @staticmethod
    def _is_transient_max_hp_track(track: _BossEffectTrack) -> bool:
        description = " ".join(
            (
                str(track.skill_name or ""),
                str(track.effect_key or ""),
                str(track.effect_label or ""),
            )
        )
        return (
            any(
                skill_name in description
                for skill_name in _TRANSIENT_MAX_HP_BUFF_SKILL_NAMES
            )
            or (
                "最大生命" in description
                and any(marker in description for marker in ("增加", "提高", "上限"))
            )
        )

    def _max_hp_buff_intervals(self, target_id: str) -> tuple[tuple[int, int], ...]:
        return tuple(
            (int(start_ms), int(end_ms))
            for track in self._player_buffs.values()
            if track.target_id == target_id
            and self._is_transient_max_hp_track(track)
            for start_ms, end_ms in track.intervals
        )

    def _is_transient_max_hp_buff_active(
        self,
        target_id: str,
        timestamp_ms: int,
    ) -> bool:
        return any(
            start_ms - _MAX_HP_BUFF_LEAD_PADDING_MS
            <= timestamp_ms
            <= end_ms + _MAX_HP_BUFF_TAIL_PADDING_MS
            for start_ms, end_ms in self._max_hp_buff_intervals(target_id)
        )

    def _resolved_player_max_hp(
        self,
        target_id: str,
        events: Iterable[PlayerHealthEvent],
    ) -> tuple[int, str, bool, int, int]:
        """Resolve a baseline continuously calibrated by absolute HP states.

        ``21 0C`` is an exact entry baseline. ``08 6F`` contains current HP,
        not a separate maximum, so an unbuffed state may safely raise that
        baseline but a lower state cannot prove that maximum HP decreased.
        """

        rows = tuple(events)
        regular_protocol_max_hp = max(
            (
                event.max_hp
                for event in rows
                if event.max_hp > 0
            ),
            default=0,
        )
        evidence_protocol_max_hp = max(
            (
                event.max_hp
                for event in getattr(
                    self, "_player_session_evidence", ()
                )
                if event.max_hp > 0
                and event.target_id == target_id
                and event.packet_kind
                == "s2c-udp-player-resource-sync-210c-v576"
            ),
            default=0,
        )
        protocol_max_hp = max(
            regular_protocol_max_hp,
            evidence_protocol_max_hp,
        )
        buff_intervals = self._max_hp_buff_intervals(target_id)
        observed_unbuffed_peak = max(
            (
                event.current_hp
                for event in rows
                if event.current_hp > 0
                and event.packet_kind
                == "s2c-udp-player-hp-state-086f-v576"
                and not any(
                    start_ms - _MAX_HP_BUFF_LEAD_PADDING_MS
                    <= event.timestamp_ms
                    <= end_ms + _MAX_HP_BUFF_TAIL_PADDING_MS
                    for start_ms, end_ms in buff_intervals
                )
            ),
            default=0,
        )
        maximum = max(protocol_max_hp, observed_unbuffed_peak)
        calibrated_by_086f = observed_unbuffed_peak > protocol_max_hp
        source = (
            "protocol_210c_calibrated_086f"
            if protocol_max_hp and calibrated_by_086f
            else "protocol_210c"
            if protocol_max_hp
            else "unbuffed_086f_peak"
            if observed_unbuffed_peak
            else "missing_protocol_and_unbuffed_086f"
        )
        return (
            maximum,
            source,
            bool(protocol_max_hp and not calibrated_by_086f),
            protocol_max_hp,
            observed_unbuffed_peak,
        )

    def _refresh_overlay_player_max_hp(self, target_id: str) -> None:
        target_events = tuple(
            event
            for event in self._player_health_events
            if event.target_id == target_id
        )
        if not target_events:
            return
        maximum, source, exact, protocol_max_hp, observed_peak = (
            self._resolved_player_max_hp(target_id, target_events)
        )
        self._overlay_protocol_max_hp[target_id] = protocol_max_hp
        self._overlay_unbuffed_hp_peak[target_id] = observed_peak
        row = self._overlay_player_health.get(target_id)
        if row is None:
            return
        current = max(0, int(row.get("current_hp", 0) or 0))
        row["max_hp"] = maximum
        row["current_percent"] = (
            max(0.0, min(100.0, current / maximum * 100.0))
            if maximum > 0
            else None
        )
        row["exact_percent"] = exact
        row["max_hp_source"] = source

    def _player_health_tracks_snapshot(
        self,
        visible_source_ids: set[str],
    ) -> tuple[PlayerHealthTrackSnapshot, ...]:
        """Build factual HP tracks with an unbuffed maximum-HP baseline.

        Once combat-state records exist for a player, signed notifications are
        never replayed into that curve. ``21 0C`` supplies the entry baseline,
        while every later unbuffed ``08 6F`` state may raise and calibrate it.
        Theory HP is a scoring-layer fallback only when neither source exists.
        """
        grouped: dict[str, list[PlayerHealthEvent]] = defaultdict(list)
        for event in self._player_health_events:
            if event.target_id in visible_source_ids:
                grouped[event.target_id].append(event)

        tracks: list[PlayerHealthTrackSnapshot] = []
        for target_id, target_events in grouped.items():
            events = sorted(
                target_events,
                key=lambda event: (
                    event.timestamp_ms,
                    0
                    if event.packet_kind
                    == "s2c-udp-player-hp-state-086f-v576"
                    else 1,
                    event.record_offset,
                ),
            )
            combat_states = [
                event
                for event in events
                if event.current_hp >= 0
                and event.packet_kind
                == "s2c-udp-player-hp-state-086f-v576"
            ]
            anchors = [event for event in events if event.current_hp > 0]
            if not anchors:
                continue
            maximum, max_hp_source, exact_percent, protocol_max_hp, _peak = (
                self._resolved_player_max_hp(target_id, events)
            )
            if combat_states:
                points = tuple(
                    PlayerHealthPointSnapshot(
                        timestamp_seconds=event.timestamp_ms / 1000.0,
                        # Clamp temporary over-cap HP to the unbuffed baseline
                        # in this score-oriented curve. Otherwise expiration of
                        # an HP buff would look like incoming damage.
                        current_hp=(
                            max(0, min(maximum, event.current_hp))
                            if maximum > 0
                            else max(0, event.current_hp)
                        ),
                        percent=(
                            max(
                                0.0,
                                min(100.0, event.current_hp / maximum * 100.0),
                            )
                            if maximum > 0
                            else None
                        ),
                    )
                    for event in combat_states
                )
                name, job = self._actor_context.get(target_id, ("", ""))
                tracks.append(
                    PlayerHealthTrackSnapshot(
                        target_id=target_id,
                        target_name=name or combat_states[-1].target_name or target_id,
                        target_job=job or combat_states[-1].target_job,
                        current_hp=points[-1].current_hp,
                        max_hp=maximum,
                        current_percent=points[-1].percent,
                        max_hp_source=max_hp_source,
                        exact_percent=exact_percent,
                        points=points,
                        state_source="absolute_086f",
                    )
                )
                continue
            if maximum <= 0:
                continue
            first_anchor_index = next(
                index
                for index, event in enumerate(events)
                if event.current_hp > 0
            )
            points_by_timestamp: dict[int, PlayerHealthPointSnapshot] = {}

            def remember(timestamp_ms: int, hp: int, *, overwrite: bool) -> None:
                point = PlayerHealthPointSnapshot(
                    timestamp_seconds=timestamp_ms / 1000.0,
                    current_hp=hp,
                    percent=max(
                        0.0,
                        min(100.0, hp / maximum * 100.0),
                    ),
                )
                if overwrite:
                    points_by_timestamp[timestamp_ms] = point
                else:
                    points_by_timestamp.setdefault(timestamp_ms, point)

            # Do not extrapolate backwards across a missing entry sync. The
            # exact maximum remains useful for scoring, but a current-HP curve
            # starts only at the first observed absolute state.
            current_hp = None
            for event in events[first_anchor_index:]:
                if event.current_hp > 0:
                    current_hp = max(0, min(maximum, event.current_hp))
                elif current_hp is not None and event.hp_delta:
                    current_hp = max(
                        0,
                        min(maximum, current_hp + event.hp_delta),
                    )
                else:
                    continue
                remember(event.timestamp_ms, current_hp, overwrite=True)
            points = tuple(
                points_by_timestamp[key]
                for key in sorted(points_by_timestamp)
            )
            if not points:
                continue
            name, job = self._actor_context.get(target_id, ("", ""))
            tracks.append(
                PlayerHealthTrackSnapshot(
                    target_id=target_id,
                    target_name=name or anchors[-1].target_name or target_id,
                    target_job=job or anchors[-1].target_job,
                    current_hp=points[-1].current_hp,
                    max_hp=maximum,
                    current_percent=points[-1].percent,
                    max_hp_source=(
                        "protocol_210c"
                    ),
                    exact_percent=exact_percent,
                    points=points,
                    state_source="resource_replay",
                )
            )
        tracks.sort(key=lambda row: (row.target_job, row.target_name, row.target_id))
        return tuple(tracks)

    def _effective_incoming_damage_events(
        self,
        visible_source_ids: set[str],
    ) -> tuple[CombatEvent, ...]:
        """Return one canonical effective-HP-loss stream.

        Signed HP deltas are already effective losses and take precedence for
        that player. Older captures without those records retain the legacy
        damage/healing replay. A verified ``21 0C`` maximum is attached here;
        the completed snapshot later fills an unbuffed ``08 6F`` baseline.
        The scoring layer supplies theoretical-plan HP only if both are absent.
        """

        if not visible_source_ids:
            return ()
        health_events_by_target: dict[str, list[PlayerHealthEvent]] = defaultdict(list)
        for event in self._player_health_events:
            if event.target_id in visible_source_ids:
                health_events_by_target[event.target_id].append(event)
        authoritative_max_hp = {
            source_id: self._resolved_player_max_hp(
                source_id,
                health_events_by_target.get(source_id, ()),
            )[0]
            for source_id in visible_source_ids
        }
        exact_events = [
            event
            for event in self._incoming_damage_events
            if event.target_id in visible_source_ids
            and event.amount > 0
            and event.attribution_method == "incoming_hp_delta"
        ]
        # Run only while constructing the canonical snapshot, not on the
        # packet-ingestion thread. Unnamed HP losses remain incoming damage
        # unless independently proven to be a reviewed skill's HP cost.
        from .self_hp_cost import annotate_skill_hp_costs

        exact_events = annotate_skill_hp_costs(
            exact_events,
            self._skill_cast_events,
            authoritative_max_hp,
            {actor_id: context[1] for actor_id, context in self._actor_context.items()},
            (
                event for event in self._incoming_damage_events
                if event.attribution_method != "incoming_hp_delta"
            ),
            {actor_id: self._max_hp_buff_intervals(actor_id) for actor_id in visible_source_ids},
        )
        periodic_groups: dict[tuple[str, int], list[CombatEvent]] = defaultdict(list)
        for event in exact_events:
            periodic_groups[(event.target_id, int(event.amount))].append(event)
        periodic_self_event_ids: set[int] = set()
        for (target_id, amount), rows in periodic_groups.items():
            ordered = sorted(rows, key=lambda event: event.timestamp_ms)
            if (
                len(ordered) < 20
                or ordered[-1].timestamp_ms - ordered[0].timestamp_ms < 60_000
            ):
                continue
            intervals = [
                right.timestamp_ms - left.timestamp_ms
                for left, right in zip(ordered, ordered[1:])
                if right.timestamp_ms > left.timestamp_ms
            ]
            if not intervals:
                continue
            median_interval = sorted(intervals)[len(intervals) // 2]
            tolerance = max(250, round(median_interval * 0.2))
            regular_ratio = sum(
                abs(interval - median_interval) <= tolerance
                for interval in intervals
            ) / len(intervals)
            maximum = max(0, authoritative_max_hp.get(target_id, 0))
            if (
                not 1_000 <= median_interval <= 3_000
                or regular_ratio < 0.75
                or (maximum > 0 and amount > maximum * 0.01)
            ):
                continue
            periodic_self_event_ids.update(id(event) for event in ordered)
        exact_events = [
            replace(
                event,
                source_id=event.target_id,
                source_name=event.target_name or event.target_id,
                job=event.target_job,
                skill_id="periodic_self_hp_loss",
                skill_name="周期性自身生命损失",
            )
            if id(event) in periodic_self_event_ids
            else event
            for event in exact_events
        ]
        exact_event_targets = {event.target_id for event in exact_events}
        self_loss_candidates = [
            event
            for event in self._incoming_damage_events
            if event.target_id in exact_event_targets
            and event.amount > 0
            and event.attribution_method != "incoming_hp_delta"
            and str(event.source_id)
            and str(event.source_id) == str(event.target_id)
        ]
        annotated_exact_events: list[CombatEvent] = []
        used_self_loss_candidates: set[int] = set()
        for event in exact_events:
            candidate_index = min(
                (
                    index
                    for index, candidate in enumerate(self_loss_candidates)
                    if index not in used_self_loss_candidates
                    and candidate.target_id == event.target_id
                    and candidate.amount == event.amount
                    and abs(candidate.timestamp_ms - event.timestamp_ms) <= 350
                ),
                key=lambda index: abs(
                    self_loss_candidates[index].timestamp_ms - event.timestamp_ms
                ),
                default=None,
            )
            if candidate_index is None:
                annotated_exact_events.append(event)
                continue
            used_self_loss_candidates.add(candidate_index)
            candidate = self_loss_candidates[candidate_index]
            annotated_exact_events.append(
                replace(
                    event,
                    source_id=candidate.source_id,
                    source_name=candidate.source_name,
                    job=candidate.job,
                    skill_id=candidate.skill_id,
                    skill_name=candidate.skill_name,
                    raw_skill_id=candidate.raw_skill_id or candidate.skill_id,
                    raw_skill_name=candidate.raw_skill_name or candidate.skill_name,
                )
            )
        exact_events = annotated_exact_events
        exact_events = [
            replace(
                event,
                target_max_hp=max(
                    0, authoritative_max_hp.get(event.target_id, 0)
                ),
            )
            for event in exact_events
        ]
        state_events_by_target: dict[str, list[PlayerHealthEvent]] = defaultdict(list)
        for event in self._player_health_events:
            if (
                event.target_id in visible_source_ids
                and event.current_hp >= 0
                and event.packet_kind
                == "s2c-udp-player-hp-state-086f-v576"
            ):
                state_events_by_target[event.target_id].append(event)
        state_targets = set(state_events_by_target)
        from .nightmare_sea_dragon import matches as sea_matches
        from .periodic_health_mechanism import sea_tick_ids, SEA_HP_TICK
        mechanism_ids = sea_tick_ids(exact_events, sea_present=sea_matches(self._authoritative_boss_ids))
        mechanisms_by_target = defaultdict(list)
        for event in exact_events:
            if id(event) in mechanism_ids:
                mechanisms_by_target[event.target_id].append(event)
        positive_deltas = defaultdict(list)
        for event in self._player_health_events:
            if event.hp_delta > 0 and event.packet_kind.endswith("1c1b0005-v576"):
                positive_deltas[event.target_id].append(event)
        state_loss_events: list[CombatEvent] = []
        consumed_mechanism_ids: set[int] = set()
        self_loss_by_target: dict[str, list[CombatEvent]] = defaultdict(list)
        for event in exact_events:
            if (
                event.target_id in state_targets
                and str(event.source_id) == str(event.target_id)
                and id(event) not in mechanism_ids
            ):
                self_loss_by_target[event.target_id].append(event)
        matched_explicit_self_ids = {
            id(self_loss_candidates[index]) for index in used_self_loss_candidates
        }
        explicit_state_self_targets: set[str] = set()
        for event in self._incoming_damage_events:
            if (
                event.target_id in state_targets
                and event.amount > 0
                and event.source_id == event.target_id
                and event.attribution_method != "incoming_hp_delta"
                and id(event) not in matched_explicit_self_ids
            ):
                self_loss_by_target[event.target_id].append(event)
                explicit_state_self_targets.add(event.target_id)
        lifecycle_by_target: dict[str, list[PlayerLifecycleEvent]] = defaultdict(list)
        for event in self._player_lifecycle_events:
            if event.target_id in state_targets:
                lifecycle_by_target[event.target_id].append(event)
        for target_id, target_states in state_events_by_target.items():
            indexed_states = list(enumerate(target_states))
            indexed_states.sort(
                key=lambda row: (
                    row[1].timestamp_ms,
                    row[0],
                    row[1].record_offset,
                )
            )
            rows = [row for _index, row in indexed_states]
            target_max_hp = max(
                0,
                authoritative_max_hp.get(target_id, 0),
            )
            target_self_losses = sorted(
                self_loss_by_target.get(target_id, ()),
                key=lambda event: event.timestamp_ms,
            )
            for previous, current in zip(rows, rows[1:]):
                if any(
                    previous.timestamp_ms < edge.timestamp_ms <= current.timestamp_ms
                    for edge in lifecycle_by_target.get(target_id, ())
                ):
                    continue
                restored = sum(e.hp_delta for e in positive_deltas[target_id]
                               if previous.timestamp_ms < e.timestamp_ms <= current.timestamp_ms)
                total_loss = max(0, previous.current_hp - current.current_hp + restored)
                mechanism_rows = [e for e in mechanisms_by_target[target_id]
                                  if previous.timestamp_ms < e.timestamp_ms <= current.timestamp_ms]
                # Preserve timed mechanism losses even when recovery cancels the
                # snapshot difference. Do not charge their amount a second time.
                for e in mechanism_rows:
                    consumed_mechanism_ids.add(id(e))
                    state_loss_events.append(replace(e, skill_id=SEA_HP_TICK,
                        source_id="__sea_hp_mechanism__", source_name="海龙机制",
                        skill_name="海龙周期机制扣血", attribution_method="incoming_hp_state",
                        target_max_hp=target_max_hp))
                total_loss = max(0, total_loss - sum(e.amount for e in mechanism_rows))
                if total_loss <= 0:
                    continue
                interval_self_losses = [
                    event for event in target_self_losses
                    if previous.timestamp_ms < event.timestamp_ms <= current.timestamp_ms
                ]
                self_loss = min(total_loss, sum(event.amount for event in interval_self_losses))
                self_skills = {(event.skill_id, event.skill_name) for event in interval_self_losses}
                self_skill_id, self_skill_name = (
                    next(iter(self_skills)) if len(self_skills) == 1
                    else ("actual_self_hp_loss", "实际自身生命损失")
                )
                target_name, target_job = self._actor_context.get(
                    target_id,
                    (current.target_name, current.target_job),
                )
                if self_loss > 0:
                    state_loss_events.append(
                        CombatEvent(
                            timestamp_ms=current.timestamp_ms,
                            source_id=target_id,
                            source_name=target_name or target_id,
                            job=target_job,
                            skill_id=self_skill_id,
                            skill_name=self_skill_name,
                            amount=self_loss,
                            target_id=target_id,
                            target_name=target_name,
                            target_job=target_job,
                            target_max_hp=target_max_hp,
                            attribution_method="incoming_hp_state",
                            raw_attribution_method=current.packet_kind,
                        )
                    )
                incoming_loss = total_loss - self_loss
                if incoming_loss <= 0:
                    continue
                state_loss_events.append(
                    CombatEvent(
                        timestamp_ms=current.timestamp_ms,
                        source_id="__incoming_hp_state__",
                        source_name="服务器HP状态",
                        job="",
                        skill_id="incoming_hp_state",
                        skill_name="实际生命损失",
                        amount=incoming_loss,
                        target_id=target_id,
                        target_name=target_name,
                        target_job=target_job,
                        target_max_hp=target_max_hp,
                        attribution_method="incoming_hp_state",
                        raw_attribution_method=current.packet_kind,
                    )
                )

        # Older/non-local captures may expose only absolute HP snapshots.
        # Match their observed loss too, but never charge a cast twice when
        # this target already has the signed-delta stream above.
        state_only_ids = state_targets - {event.target_id for event in exact_events} - explicit_state_self_targets
        if state_only_ids:
            state_only_costs = annotate_skill_hp_costs(
                (event for event in state_loss_events if event.target_id in state_only_ids),
                self._skill_cast_events,
                authoritative_max_hp,
                {actor_id: context[1] for actor_id, context in self._actor_context.items()},
                (
                    event for event in self._incoming_damage_events
                    if event.attribution_method != "incoming_hp_delta"
                ),
                {actor_id: self._max_hp_buff_intervals(actor_id) for actor_id in state_only_ids},
            )
            state_loss_events = [
                event for event in state_loss_events if event.target_id not in state_only_ids
            ] + state_only_costs

        legacy_exact_events = [
            replace(event, skill_id=SEA_HP_TICK, skill_name="海龙周期机制扣血",
                    source_id="__sea_hp_mechanism__", source_name="海龙机制",
                    attribution_method="incoming_hp_state")
            if id(event) in mechanism_ids else event
            for event in exact_events
            if (event.target_id not in state_targets
                or (id(event) in mechanism_ids and id(event) not in consumed_mechanism_ids))
        ]
        exact_targets = {
            event.target_id
            for event in legacy_exact_events
            if not (
                str(event.source_id)
                and str(event.source_id) == str(event.target_id)
            )
        }
        matched_self_loss_candidate_ids = {
            id(self_loss_candidates[index])
            for index in used_self_loss_candidates
        }
        legacy_targets = visible_source_ids - exact_targets - state_targets
        max_hp_by_target = {
            source_id: max(
                1,
                authoritative_max_hp.get(source_id, 0)
                or _FALLBACK_PLAYER_MAX_HP,
            )
            for source_id in legacy_targets
        }
        current_hp = dict(max_hp_by_target)
        timeline: list[tuple[int, int, CombatEvent | PlayerLifecycleEvent]] = []
        timeline.extend(
            (event.timestamp_ms, 0, event)
            for event in self._incoming_damage_events
            if event.target_id in legacy_targets
            and event.amount > 0
            and event.attribution_method != "incoming_hp_delta"
            and id(event) not in matched_self_loss_candidate_ids
        )
        timeline.extend(
            (event.timestamp_ms, 1, event)
            for event in self._raw_healing_events
            if event.target_id in legacy_targets
            and event.amount > 0
        )
        timeline.extend(
            (event.timestamp_ms, 2, event)
            for event in self._player_lifecycle_events
            if event.target_id in legacy_targets
        )

        normalized: list[CombatEvent] = []
        hp_bar_alive = {target_id: True for target_id in visible_source_ids}
        for _timestamp_ms, kind, event in sorted(
            timeline,
            key=lambda row: (row[0], row[1]),
        ):
            target_id = event.target_id
            maximum = max_hp_by_target[target_id]
            if kind == 2:
                assert isinstance(event, PlayerLifecycleEvent)
                was_alive = hp_bar_alive[target_id]
                hp_bar_alive[target_id] = event.active
                if not event.active:
                    current_hp[target_id] = 0
                elif not was_alive:
                    # A zero -> active edge is the server-backed revival
                    # boundary. Start a fresh life so later incoming damage is
                    # not discarded merely because the previous life ended.
                    current_hp[target_id] = maximum
                continue
            assert isinstance(event, CombatEvent)
            if kind == 1:
                current_hp[target_id] = min(
                    maximum,
                    current_hp[target_id] + max(0, event.amount),
                )
                continue
            actual_loss = min(max(0, event.amount), current_hp[target_id])
            if actual_loss <= 0:
                continue
            current_hp[target_id] -= actual_loss
            normalized.append(
                replace(
                    event,
                    amount=actual_loss,
                    target_max_hp=max(
                        0, authoritative_max_hp.get(target_id, 0)
                    ),
                )
            )
        return tuple(
            sorted(
                (*state_loss_events, *legacy_exact_events, *normalized),
                key=lambda event: (
                    event.timestamp_ms,
                    event.target_id,
                    event.attribution_method,
                ),
            )
        )

    def _reconciled_healing(
        self,
        visible_source_ids: set[str],
    ) -> HealingAttributionResult:
        """Return the independent, HP-backed healing attribution ledger."""

        signature = frozenset(visible_source_ids)
        if (
            self._healing_attribution_revision != self._revision
            or self._healing_visible_signature != signature
        ):
            excluded_intervals = {
                target_id: tuple(
                    (
                        max(0, start_ms - _MAX_HP_BUFF_LEAD_PADDING_MS),
                        end_ms + _MAX_HP_BUFF_TAIL_PADDING_MS,
                    )
                    for start_ms, end_ms in self._max_hp_buff_intervals(
                        target_id
                    )
                )
                for target_id in signature
            }
            from .nightmare_sea_dragon import matches as sea_matches
            from .periodic_health_mechanism import sea_tick_ids
            tick_ids = sea_tick_ids(self._incoming_damage_events,
                                    sea_present=sea_matches(self._authoritative_boss_ids))
            tick_times = defaultdict(list)
            for event in self._incoming_damage_events:
                if id(event) in tick_ids:
                    tick_times[event.target_id].append(event.timestamp_ms)
            reset_times = defaultdict(list)
            for event in self._skill_parameter_events:
                if (str(event.raw_skill_id or event.skill_id) in {"34709", "32100", "325408"}
                        and event.effect_class_id == 16 and event.parameters.strip() == "1"):
                    reset_times[event.target_id].append(event.timestamp_ms)
            raw_heals = [replace(e, attribution_method="completion_recovery_unattributed")
                         if any(0 <= e.timestamp_ms-t <= 250 for t in reset_times[e.target_id])
                         else replace(e, attribution_method="mechanism_recovery_unattributed")
                         if (e.amount == 10000 and any(0 <= e.timestamp_ms-t <= 1200
                                                       for t in tick_times[e.target_id]))
                         else e for e in self._raw_healing_events]
            self._healing_attribution_result = (
                self._healing_attribution_ledger.reconcile(
                    raw_heals,
                    self._skill_cast_events,
                    self._player_health_events,
                    self._player_lifecycle_events,
                    party_ids=signature,
                    excluded_hp_rise_intervals_by_target=excluded_intervals,
                    actor_jobs_by_source={
                        source_id: self._actor_context.get(
                            source_id, ("", "")
                        )[1]
                        for source_id in signature
                    },
                )
            )
            self._healing_attribution_revision = self._revision
            self._healing_visible_signature = signature
        return self._healing_attribution_result

    def _inferred_player_death_events(
        self,
        visible_source_ids: set[str],
        boss_phase_windows: dict[object, tuple[tuple[float, float], ...]],
        current_ms: int,
        incoming_damage_events: tuple[CombatEvent, ...] | None = None,
    ) -> tuple[CombatEvent, ...]:
        """Recover deaths only from authoritative absolute HP-zero states.

        An ``08 6F`` state whose absolute HP is zero confirms one death.  The
        noisier ``05 11 active=0`` life-bar/visibility stream is deliberately
        not death evidence: it can stay false while a player is still casting.

        Repeated zero snapshots from the same dead period count once.  A later
        positive ``08 6F`` snapshot or a new direct skill cast proves that the
        player has revived and rearms the next HP-zero death.  The direct-cast
        fallback matters for captures where no positive HP snapshot was sent
        between two genuine deaths.
        """

        if not visible_source_ids:
            return ()

        # Explicit HP-zero death rows already present in the combat ledger
        # suppress a duplicate 08 6F row nearby (for example after replay
        # actor-id merging).
        explicit_by_target: dict[str, list[int]] = defaultdict(list)
        for event in self._combat_events:
            method = str(event.attribution_method or "").casefold()
            if method != "player_death_hp_state_086f":
                continue
            target_id = (
                event.target_id
                if event.target_id in visible_source_ids
                else event.source_id
            )
            if target_id in visible_source_ids:
                explicit_by_target[target_id].append(event.timestamp_ms)

        deaths: list[CombatEvent] = []
        direct_casts_by_target: dict[str, list[int]] = defaultdict(list)
        for event in self._skill_cast_events:
            if event.source_id not in visible_source_ids:
                continue
            if is_timeline_recovered_cast(event):
                continue
            direct_casts_by_target[event.source_id].append(event.timestamp_ms)
        for timestamps in direct_casts_by_target.values():
            timestamps.sort()

        state_by_target: dict[str, list[PlayerHealthEvent]] = defaultdict(list)
        for event in self._player_health_events:
            if (
                event.target_id in visible_source_ids
                and event.current_hp >= 0
                and event.packet_kind
                == "s2c-udp-player-hp-state-086f-v576"
            ):
                state_by_target[event.target_id].append(event)
        for target_id, target_states in state_by_target.items():
            armed = True
            last_zero_ms = -1
            direct_casts = direct_casts_by_target.get(target_id, ())
            for _index, row in sorted(
                enumerate(target_states),
                key=lambda item: (
                    item[1].timestamp_ms,
                    item[0],
                    item[1].record_offset,
                ),
            ):
                if row.current_hp > 0:
                    armed = True
                    continue
                if not armed and any(
                    last_zero_ms < timestamp_ms < row.timestamp_ms
                    for timestamp_ms in direct_casts
                ):
                    armed = True
                if not armed:
                    continue
                if any(
                    abs(row.timestamp_ms - value) <= 8_000
                    for value in explicit_by_target.get(target_id, ())
                ):
                    armed = False
                    last_zero_ms = row.timestamp_ms
                    continue
                target_name, target_job = self._actor_context.get(
                    target_id, (row.target_name, row.target_job)
                )
                deaths.append(
                    CombatEvent(
                        timestamp_ms=row.timestamp_ms,
                        source_id="__combat_state__",
                        source_name="战斗状态",
                        job="",
                        skill_id="player_death",
                        skill_name="死亡（实际HP归零）",
                        amount=0,
                        target_id=target_id,
                        target_name=target_name,
                        target_job=target_job,
                        attribution_method="player_death_hp_state_086f",
                    )
                )
                explicit_by_target[target_id].append(row.timestamp_ms)
                armed = False
                last_zero_ms = row.timestamp_ms

        return tuple(sorted(deaths, key=lambda event: event.timestamp_ms))

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def overlay_player_buff_state_revision(self) -> int:
        return self._overlay_player_buff_state_revision

    def _remember_actor_context(
        self, source_id: str, source_name: str, job: str
    ) -> None:
        if not source_id:
            return
        old_name, old_job = self._actor_context.get(source_id, ("", ""))
        if _is_anonymous_actor_name(old_name, source_id, old_job):
            old_name = ""
        incoming_name = str(source_name or "").strip()
        incoming_job = str(job or "").strip()
        if _is_anonymous_actor_name(incoming_name, source_id, incoming_job):
            incoming_name = ""
        self._actor_context[source_id] = (
            incoming_name or old_name,
            _prefer_specific_player_job(old_job, incoming_job),
        )
        if incoming_job in _player_job_stages() or incoming_job in {
            "战士", "牧师", "弓箭手", "魔法师", "学者", "卡莉", "刺客",
        }:
            self._confirm_player_status_identity(source_id)

    def _received_player_status(self, event: BossStatusEvent) -> BossStatusEvent | None:
        """Reclassify a captured response, without inventing a cast/recipient."""
        if (event.source_id not in self._confirmed_player_ids or is_inferred_aura_row(event)
            or _is_chakra_cure_channel_marker(event)):
            return None  # Enemy states on players are not player-origin gains.
        if event.skill_id in AURA_SKILL_KEYS and (
            event.effect_instance_id <= 0 or event.effect_code not in {"134700", "134600"}
        ):
            return None
        target_name, target_job = self._actor_context.get(event.target_id, ("", ""))
        parameters = event.parameters if event.raw_parameters is None else event.raw_parameters
        duration_ms = event.duration_ms if event.raw_duration_ms is None else event.raw_duration_ms
        try:
            value = float(parameters) * 100.0
        except (TypeError, ValueError):
            value = event.value if event.value_source == "packet" else None
        return replace(
            event, target_scope="player", target_name=target_name or event.target_id,
            target_job=target_job, target_max_hp=0,
            parameters=parameters, duration_ms=duration_ms,
            effect_key=(f"角色增益:{event.skill_id}:{event.effect_code}:{parameters}"
                        + (f":class{event.effect_class_id}" if event.skill_id in AURA_SKILL_KEYS else "")),
            effect_label=f"{event.skill_name}增益",
            status_kind="团队增益" if event.source_id != event.target_id else "个人增益",
            value=value, value_source="packet" if value is not None else "unavailable",
            duration_source="packet" if event.permanent or duration_ms > 0 else "unavailable",
            evidence_key="", stackable=False, stack_group="",
        )

    def _confirm_player_status_identity(self, actor_id: str) -> None:
        if (not actor_id or actor_id in self._confirmed_player_ids
            or actor_id in self._authoritative_boss_ids):
            return
        self._confirmed_player_ids.add(actor_id)
        indexes = self._boss_status_indexes_by_target.pop(actor_id, ())
        converted: dict[tuple[object, ...], BossStatusEvent] = {}
        for index in indexes:
            original = self._boss_status_events[index]
            track_key = (original.target_id, original.source_id, original.skill_id,
                         original.effect_key, _effect_value_signature(original.value, original.parameters))
            self._boss_effects.pop(track_key, None)
            # Leave a corrected tombstone in the append-only indexed list.
            # The Boss projection excludes it; only the Buff ledger exposes it.
            self._boss_status_events[index] = replace(original, target_scope="player")
            corrected = self._received_player_status(original)
            if corrected is None and original.source_id not in self._confirmed_player_ids:
                self._pending_player_statuses_by_source[original.source_id].append(original)
            if corrected is not None:
                identity = (corrected.timestamp_ms, corrected.source_id, corrected.skill_id,
                            corrected.effect_code, corrected.parameters, corrected.effect_instance_id,
                            corrected.operation)
                converted[identity] = corrected
        self._freeze_tracks_by_target.pop(actor_id, None)
        self._target_max_hp.pop(actor_id, None)
        for original in self._pending_player_statuses_by_source.pop(actor_id, ()):
            corrected = self._received_player_status(original)
            if corrected is not None:
                identity = (corrected.timestamp_ms, corrected.target_id, corrected.skill_id,
                            corrected.effect_key, corrected.operation, corrected.effect_instance_id)
                converted[identity] = corrected
        if not converted:
            return
        targets = set()
        for event in converted.values():
            targets.add(event.target_id)
            self._record_player_buff_event(event)
            skill_key = _overlay_skill_family_key(event.skill_name or event.skill_id)
            self._overlay_player_buff_skill_keys.add(skill_key)
            if event.source_id != event.target_id:
                self._overlay_team_buff_skill_keys.add(skill_key)
        # Identity may arrive after an end/death/new instance. Rebuild only the
        # affected recipients in packet time order, never append old applies to
        # the current state (which would resurrect an already-ended Buff).
        for target_id in targets:
            self._rebuild_player_buff_target(target_id)
        self._overlay_player_buff_state_revision += 1

    def _record_player_buff_event(self, event: BossStatusEvent) -> None:
        self._player_buff_indexes_by_target[event.target_id].append(len(self._player_buff_events))
        self._player_buff_events.append(event)

    def _rebuild_player_buff_target(self, target_id: str) -> None:
        for key, track in tuple(self._player_buffs.items()):
            if track.target_id == target_id:
                del self._player_buffs[key]
        rows = [(self._player_buff_events[i].timestamp_ms, 0, self._player_buff_events[i])
                for i in self._player_buff_indexes_by_target[target_id]]
        rows.extend((time, 1, reason) for time, reason
                    in self._player_buff_boundaries_by_target[target_id])
        for timestamp, _priority, event in sorted(rows, key=lambda row: row[:2]):
            if isinstance(event, BossStatusEvent):
                self._ingest_status(event, self._player_buffs)
            elif event == "clear":
                for track in self._player_buffs.values():
                    if (track.target_id != target_id or track.permanent
                        or _is_player_buff_clear_immune(track)):
                        continue
                    for interval in track.intervals:
                        if interval[0] <= timestamp < interval[1]:
                            interval[1] = timestamp
            else:
                self._close_player_buffs_at(target_id, timestamp, visibility_only=event == "visibility")

    def _remember_overlay_skill_cast(self, event: SkillCastEvent) -> None:
        skill_key = _overlay_skill_family_key(
            event.skill_name or event.skill_id
        )
        if not event.source_id or not skill_key:
            return
        cast_key = (event.source_id, skill_key)
        previous = self._overlay_latest_skill_casts.get(cast_key)
        if previous is None or event.timestamp_ms >= previous.timestamp_ms:
            self._overlay_latest_skill_casts[cast_key] = event
            # Casting the tracked skill starts a new cooldown cycle. A refund
            # from the previous cycle must never shorten the new countdown.
            self._overlay_cooldown_refunds.pop(cast_key, None)

    def _remember_overlay_cooldown_refund(
        self,
        event: BossStatusEvent,
    ) -> None:
        if event.operation != "apply":
            return
        status_skill_id = str(event.skill_id or "").strip()
        target = _LIVE_COOLDOWN_REFUND_STATUS_SKILLS.get(status_skill_id)
        if target is None:
            return
        target_skill_id, target_skill_name = target
        parameters = [
            part.strip()
            for part in str(event.parameters or "").replace(",", ";").split(";")
            if part.strip()
        ]
        if len(parameters) < 2 or parameters[0] != target_skill_id:
            return
        try:
            refund_seconds = float(parameters[1]) / 1000.0
        except ValueError:
            return
        if refund_seconds <= 0.0:
            return
        source_id = str(event.target_id or event.source_id or "").strip()
        target_skill_key = _overlay_skill_family_key(target_skill_name)
        cast = self._overlay_latest_skill_casts.get((source_id, target_skill_key))
        if cast is None or event.timestamp_ms < cast.timestamp_ms:
            return
        refund_key = (source_id, target_skill_key)
        previous = self._overlay_cooldown_refunds.get(refund_key, {})
        previous_cast_ms = int(previous.get("cycle_cast_ms") or -1)
        total_seconds = (
            float(previous.get("refund_seconds") or 0.0)
            if previous_cast_ms == cast.timestamp_ms
            else 0.0
        )
        event_count = (
            int(previous.get("event_count") or 0)
            if previous_cast_ms == cast.timestamp_ms
            else 0
        )
        self._overlay_cooldown_refunds[refund_key] = {
            "source_id": source_id,
            "target_skill_id": target_skill_id,
            "target_skill_name": target_skill_name,
            "cycle_cast_ms": cast.timestamp_ms,
            "refund_seconds": total_seconds + refund_seconds,
            "event_count": event_count + 1,
            "last_event_ms": event.timestamp_ms,
            "evidence": "server_status",
        }

    def _merge_party_member(
        self,
        event: PartyMemberEvent,
    ) -> PartyMemberEvent:
        """Merge a party row without allowing weaker identity evidence to win."""

        previous = self._party_members.get(event.actor_id)
        context_name, context_job = self._actor_context.get(
            event.actor_id, ("", "")
        )
        incoming_name = str(event.name or "").strip()
        previous_name = str(previous.name or "").strip() if previous else ""
        if _is_anonymous_actor_name(
            previous_name,
            event.actor_id,
            previous.job if previous else "",
        ):
            previous_name = ""
        if _is_anonymous_actor_name(incoming_name, event.actor_id, event.job):
            incoming_name = ""
        resolved_name = (
            context_name
            or incoming_name
            or previous_name
            or str(event.name or "").strip()
        )
        resolved_job = _prefer_specific_player_job(
            previous.job if previous else "",
            event.job,
        )
        resolved_job = _prefer_specific_player_job(resolved_job, context_job)
        return replace(
            event,
            name=resolved_name,
            job=resolved_job,
            level=event.level or (previous.level if previous else 0),
            party_position=(
                event.party_position
                or (previous.party_position if previous else 0)
            ),
            confidence=max(
                event.confidence,
                previous.confidence if previous else 0.0,
            ),
        )

    @staticmethod
    def _player_profile_quality(
        event: PlayerInspectEvent,
    ) -> tuple[int, int, int, int, int, int, int, int]:
        """Prefer decoded equipment/card bodies over marker-only events."""
        return (
            int(bool(event.equipment)),
            len(event.equipment),
            int(bool(event.cards)),
            len(event.cards),
            int(bool(norm(event.job))),
            len(event.stats),
            len(event.fields),
            event.timestamp_ms,
        )

    @classmethod
    def _merge_player_profiles(
        cls, previous: PlayerInspectEvent, current: PlayerInspectEvent
    ) -> PlayerInspectEvent:
        richer, supplement = (
            (current, previous)
            if cls._player_profile_quality(current) >= cls._player_profile_quality(previous)
            else (previous, current)
        )
        previous_card_update = previous.response_layout.startswith(
            "TCP:card_update:"
        ) or any(row and row[0].startswith("本次") for row in previous.cards)
        current_card_update = current.response_layout.startswith(
            "TCP:card_update:"
        ) or any(row and row[0].startswith("本次") for row in current.cards)
        cards = previous.cards
        if current.cards and (
            not previous.cards
            or previous_card_update
            or not current_card_update
        ):
            cards = current.cards
        archive_supplement = (
            previous.response_layout.startswith("TCP:engravings_archive:")
            or current.response_layout.startswith("TCP:engravings_archive:")
        )
        if archive_supplement:
            equipment_by_key = dict(previous.equipment)
            equipment_by_key.update(dict(current.equipment))
            equipment = tuple(equipment_by_key.items())
        else:
            equipment = richer.equipment or supplement.equipment
        fields_by_key = dict(previous.fields)
        fields_by_key.update(dict(current.fields))
        return replace(
            richer,
            timestamp_ms=max(previous.timestamp_ms, current.timestamp_ms),
            target_name=richer.target_name or supplement.target_name,
            job=(
                supplement.job
                if _player_job_stages().get(supplement.job, 0)
                > _player_job_stages().get(richer.job, 0)
                else richer.job or supplement.job
            ),
            level=richer.level or supplement.level,
            party_position=richer.party_position or supplement.party_position,
            # ``current`` is the newly received fragment.  Preserve its
            # explicit inspect-request key even when an older profile wins the
            # richness comparison, otherwise reopening the same player's panel
            # could look like the already dismissed request.
            inspection_key=(
                current.inspection_key or previous.inspection_key
            ),
            request_fingerprint=(
                richer.request_fingerprint or supplement.request_fingerprint
            ),
            response_layout=richer.response_layout or supplement.response_layout,
            # Login-time equipment, card and card-mastery archives can arrive
            # as separate profile events. Preserve every independently decoded
            # field instead of letting the visually richer event erase them.
            fields=tuple(fields_by_key.items()),
            equipment=equipment,
            # A single-card page notification is useful calibration evidence,
            # but it must never replace a complete card archive. Consecutive
            # partial notifications do follow their actual chronological order.
            cards=cards,
            stats=richer.stats or supplement.stats,
            raw_payload_hex=richer.raw_payload_hex or supplement.raw_payload_hex,
        )

    def ingest(
        self,
        event: CombatEvent
        | SettlementEvent
        | BossStatusEvent
        | BossSpawnEvent
        | BossHealthEvent
        | MonsterAppearanceEvent
        | BossActionEvent
        | SkillCastEvent
        | SkillParameterEvent
        | PartyMemberEvent
        | PlayerHealthEvent
        | PlayerLifecycleEvent
        | PlayerInspectEvent
        | SummonRemovalEvent,
    ) -> None:
        # A decoded replay may carry a precomputed immutable projection.  Any
        # later event makes that projection stale, so live/reconnect paths must
        # invalidate it before changing the factual ledgers.
        self._replay_snapshot = None
        if self._retain_source_events:
            self._source_events.append(event)
        self._revision += 1
        if isinstance(event, BossHealthEvent):
            self._boss_hp_ledger.ingest_health(event)
            return  # HP synchronization alone is not active combat evidence.
        if isinstance(event, MonsterAppearanceEvent):
            self._monster_appearance_sequence += 1
            self._monster_appearance_events.append((self._monster_appearance_sequence, event))
            return  # Appearance alone must not change damage, HP or combat clocks.
        if isinstance(event, SettlementEvent):
            self._settlement_ledger.observe(event)
            return  # Result timestamps must not change damage/Buff/HP clocks.
        if not isinstance(event, (PartyMemberEvent, PlayerInspectEvent)) and not (
            isinstance(event, PlayerHealthEvent)
            and bool(getattr(event, "session_evidence", False))
        ):
            timestamp_ms = getattr(event, "timestamp_ms", None)
            if timestamp_ms is not None:
                resolved_timestamp = max(0, int(timestamp_ms))
                self._state_first_ms = (
                    resolved_timestamp
                    if self._state_first_ms is None
                    else min(self._state_first_ms, resolved_timestamp)
                )
                self._state_last_ms = (
                    resolved_timestamp
                    if self._state_last_ms is None
                    else max(self._state_last_ms, resolved_timestamp)
                )
        if isinstance(event, PartyMemberEvent):
            if event.actor_id:
                if event.confidence >= _RELIABLE_PARTY_MEMBER_CONFIDENCE:
                    self._remember_actor_context(
                        event.actor_id,
                        event.name,
                        event.job,
                    )
                    self._confirm_player_status_identity(event.actor_id)
                self._party_members[event.actor_id] = self._merge_party_member(
                    event
                )
            return
        if isinstance(event, PlayerHealthEvent):
            self._remember_actor_context(
                event.target_id, event.target_name, event.target_job
            )
            if bool(getattr(event, "session_evidence", False)):
                # Appendix-only 21 0C evidence is deliberately excluded from
                # the live HP curve, deaths, Buff closure and elapsed time.
                # It contributes only the exact protocol maximum and identity
                # facts needed by scoring/reconnect normalization.
                evidence_rows = getattr(
                    self, "_player_session_evidence", None
                )
                if evidence_rows is None:
                    evidence_rows = []
                    self._player_session_evidence = evidence_rows
                evidence_rows.append(event)
                if event.max_hp > 0:
                    self._overlay_protocol_max_hp[event.target_id] = max(
                        self._overlay_protocol_max_hp[event.target_id],
                        int(event.max_hp),
                    )
                self._refresh_overlay_player_max_hp(event.target_id)
                return
            self._player_health_events.append(event)
            if (
                event.max_hp > 0
                or event.packet_kind
                in {
                    "s2c-udp-player-resource-sync-210c-v576",
                    "s2c-udp-player-hp-state-086f-v576",
                }
            ):
                previous_health = self._overlay_player_health.get(
                    event.target_id, {}
                )
                if event.max_hp > 0:
                    self._overlay_protocol_max_hp[event.target_id] = max(
                        self._overlay_protocol_max_hp[event.target_id],
                        int(event.max_hp),
                    )
                if (
                    event.current_hp > 0
                    and event.packet_kind
                    == "s2c-udp-player-hp-state-086f-v576"
                    and not self._is_transient_max_hp_buff_active(
                        event.target_id,
                        event.timestamp_ms,
                    )
                ):
                    self._overlay_unbuffed_hp_peak[event.target_id] = max(
                        self._overlay_unbuffed_hp_peak[event.target_id],
                        int(event.current_hp),
                    )
                protocol_maximum = self._overlay_protocol_max_hp[event.target_id]
                observed_peak = self._overlay_unbuffed_hp_peak[event.target_id]
                maximum = max(protocol_maximum, observed_peak)
                calibrated_by_086f = observed_peak > protocol_maximum
                current = max(0, int(event.current_hp))
                self._overlay_player_health[event.target_id] = {
                    "target_id": event.target_id,
                    "target_name": event.target_name or event.target_id,
                    "target_job": event.target_job,
                    "current_hp": current,
                    "max_hp": maximum,
                    "current_percent": (
                        current / maximum * 100.0 if maximum > 0 else None
                    ),
                    "exact_percent": bool(
                        protocol_maximum and not calibrated_by_086f
                    ),
                    "max_hp_source": (
                        "protocol_210c_calibrated_086f"
                        if protocol_maximum and calibrated_by_086f
                        else "protocol_210c"
                        if protocol_maximum
                        else "unbuffed_086f_peak"
                        if observed_peak
                        else "missing_protocol_and_unbuffed_086f"
                    ),
                    "timestamp_ms": event.timestamp_ms,
                }
            # Only an absolute zero-HP state is authoritative for the target's
            # current life. Delta-only ``1C 1B 00 04`` notifications use
            # ``current_hp = max_hp = 0`` as an unknown-value placeholder and
            # must not be mistaken for death, otherwise every HP change closes
            # the target's active buffs prematurely.
            if event.current_hp <= 0 and (
                event.packet_kind == "s2c-udp-player-hp-state-086f-v576"
                or event.max_hp > 0
            ):
                self._player_lifecycle_active_by_target[event.target_id] = False
                self._player_buff_boundaries_by_target[event.target_id].append((event.timestamp_ms, "death"))
                self._close_player_buffs_at(event.target_id, event.timestamp_ms)
                self._summon_presence.remove_owner(event.target_id)
            elif (
                event.packet_kind == "s2c-udp-player-hp-state-086f-v576"
                and event.current_hp > 0
                and event.target_id
                not in self._player_lifecycle_active_by_target
            ):
                self._player_lifecycle_active_by_target[event.target_id] = True
            return
        if isinstance(event, SummonRemovalEvent):
            self._remember_actor_context(
                event.owner_id, event.owner_name, event.owner_job
            )
            self._summon_removal_events.append(event)
            self._summon_presence.ingest_removal(event)
            return
        if isinstance(event, PlayerLifecycleEvent):
            self._remember_actor_context(
                event.target_id, event.target_name, event.target_job
            )
            previous_active = self._player_lifecycle_active_by_target.get(
                event.target_id,
                True,
            )
            if previous_active == bool(event.active):
                return
            self._player_lifecycle_active_by_target[event.target_id] = bool(
                event.active
            )
            self._player_lifecycle_events.append(event)
            # 05 11 controls party visibility/life-bar presentation. It is not
            # a Buff removal or an authoritative death: a hidden actor may
            # still have positive HP and retained buffs. Keep the factual flag
            # for diagnostics, but leave Buff/summon lifetimes to their own
            # removal events and the absolute HP-zero path above.
            return
        if isinstance(event, PlayerInspectEvent):
            if event.target_id:
                previous = self._player_profiles.get(event.target_id)
                self._player_profiles[event.target_id] = (
                    event
                    if previous is None
                    else self._merge_player_profiles(previous, event)
                )
            return
        if isinstance(event, BossSpawnEvent):
            self._authoritative_boss_ids.add(event.target_id)
            self._remember_actor_context(
                event.target_id, event.target_name, event.target_job
            )
            if event.target_id and event.target_max_hp > 0:
                self._target_max_hp[event.target_id] = event.target_max_hp
            self._boss_hp_ledger.ingest_spawn(event)
            return
        if isinstance(event, BossActionEvent):
            self._remember_actor_context(
                event.source_id, event.source_name, event.job
            )
            if event.packet_kind == "boss_completion_trigger":
                self._boss_hp_ledger.ingest_completion(
                    target_id=event.source_id,
                    timestamp_ms=event.timestamp_ms,
                    source=event.packet_kind,
                )
                # This is lifecycle evidence, not an attack animation and not
                # a damage row.  Keeping it out of the action timeline avoids
                # displaying a fake Boss skill named "mechanism settlement".
                return
            closed_buffs = self._ingest_player_buff_clear(event)
            is_player_buff_clear = self._is_player_buff_clear_event(event)
            if is_player_buff_clear:
                for target_id in event.affected_target_ids:
                    self._player_buff_boundaries_by_target[target_id].append((event.timestamp_ms, "clear"))
                identity = (
                    event.timestamp_ms,
                    event.source_id,
                    event.skill_index,
                    tuple(event.affected_target_ids),
                )
                previous_identity = (
                    (
                        self._player_buff_clear_events[-1].timestamp_ms,
                        self._player_buff_clear_events[-1].source_id,
                        self._player_buff_clear_events[-1].skill_index,
                        tuple(
                            self._player_buff_clear_events[-1].affected_target_ids
                        ),
                    )
                    if self._player_buff_clear_events
                    else None
                )
                if identity != previous_identity:
                    self._player_buff_clear_events.append(event)
            if event.packet_kind == "boss_buff_clear_status":
                if closed_buffs:
                    self._overlay_player_buff_clear_revision += 1
                    self._overlay_player_buff_clear_events.append(
                        {
                            "revision": self._overlay_player_buff_clear_revision,
                            "timestamp_ms": event.timestamp_ms,
                            "source_id": event.source_id,
                            "source_name": event.source_name,
                            "skill_id": event.skill_id,
                            "skill_name": event.skill_name,
                            "closed_buffs": [dict(row) for row in closed_buffs],
                        }
                    )
                    # Keep enough explicit events to bridge a temporarily slow
                    # overlay publisher without growing with raid duration.
                    if len(self._overlay_player_buff_clear_events) > 256:
                        del self._overlay_player_buff_clear_events[:-256]
                return
            self._boss_action_events.append(event)
            return
        if isinstance(event, BossStatusEvent):
            if is_inferred_aura_row(event):
                return
            self._remember_actor_context(
                event.source_id, event.source_name, event.job
            )
            self._remember_actor_context(
                event.target_id, event.target_name, event.target_job
            )
            if event.target_scope == "player":
                self._confirm_player_status_identity(event.target_id)
            elif event.target_id in self._confirmed_player_ids:
                original = event
                event = self._received_player_status(original)
                if event is None:
                    if original.source_id not in self._confirmed_player_ids:
                        self._pending_player_statuses_by_source[original.source_id].append(original)
                    return
            if event.target_id and event.target_max_hp > 0:
                self._target_max_hp[event.target_id] = event.target_max_hp
            if event.target_scope == "player":
                if _is_chakra_cure_channel_marker(event):
                    return
                self._record_player_buff_event(event)
                status_changed = self._ingest_status(
                    event, self._player_buffs
                )
                if status_changed:
                    self._remember_overlay_cooldown_refund(event)
                    self._overlay_player_buff_state_revision += 1
                skill_key = _overlay_skill_family_key(
                    event.skill_name or event.skill_id
                )
                if skill_key:
                    self._overlay_player_buff_skill_keys.add(skill_key)
                if event.status_kind == "团队增益":
                    if skill_key:
                        self._overlay_team_buff_skill_keys.add(skill_key)
                if any(
                    skill_name in " ".join(
                        (
                            str(event.skill_name or ""),
                            str(event.effect_key or ""),
                            str(event.effect_label or ""),
                        )
                    )
                    for skill_name in _TRANSIENT_MAX_HP_BUFF_SKILL_NAMES
                ):
                    # A status can arrive after the adjacent HP state. Rebuild
                    # this player's small calibration ledger so that the
                    # temporary raised value is removed from the live maximum.
                    self._refresh_overlay_player_max_hp(event.target_id)
            else:
                self._ingest_boss_status(event)
            return
        if isinstance(event, SkillCastEvent):
            if event.packet_kind == 'skill_form_confirmation':
                for i in range(len(self._skill_cast_events)-1, -1, -1):
                    previous = self._skill_cast_events[i]
                    if previous.source_id == event.source_id and previous.timestamp_ms == event.timestamp_ms and str(previous.skill_id) in {'407','505'}:
                        self._skill_cast_events[i] = replace(event, packet_kind=previous.packet_kind)
                        self._attribution_revision += 1
                        break
                return
            self._remember_actor_context(
                event.source_id, event.source_name, event.job
            )
            self._skill_cast_events.append(event)
            self._remember_overlay_skill_cast(event)
            self._summon_presence.ingest_cast(event)
            return
        if isinstance(event, SkillParameterEvent):
            self._remember_actor_context(
                event.source_id, event.source_name, event.job
            )
            self._skill_parameter_events.append(event)
            if is_shock_damage_identity_parameter(event):
                self._shock_damage_identity_parameters.append(event)
            return
        if event.attribution_method in {
            "incoming_damage",
            "incoming_hp_delta",
        }:
            self._remember_actor_context(
                event.target_id,
                event.target_name,
                event.target_job,
            )
            self._incoming_damage_events.append(event)
            if event.target_id:
                self._overlay_incoming_damage[event.target_id] += event.amount
            return
        self._boss_hp_ledger.ingest_combat(event)
        if (
            event.healing
            and event.target_job.strip().partition(" ")[0].casefold() == "boss"
        ):
            # Boss recovery belongs to the HP ledger, not to party healing.
            return
        if not event.source_id or event.amount <= 0:
            return
        self._remember_actor_context(
            event.source_id, event.source_name, event.job
        )
        self._remember_actor_context(
            event.target_id,
            event.target_name,
            event.target_job,
        )
        if event.target_id and event.target_max_hp > 0:
            self._target_max_hp[event.target_id] = event.target_max_hp

        display_event, transformed_cast = self._normalize_transformed_basic_attack(event)
        if transformed_cast is not None:
            self._remember_actor_context(
                transformed_cast.source_id,
                transformed_cast.source_name,
                transformed_cast.job,
            )
            self._skill_cast_events.append(transformed_cast)
            self._remember_overlay_skill_cast(transformed_cast)
        if event.healing:
            self._raw_healing_events.append(event)
        else:
            self._raw_damage_ledger.append(event)
            if event.target_job.strip().partition(" ")[0].casefold() == "boss":
                self._settlement_ledger.observe_boss_damage(event.timestamp_ms, event.source_id, event.target_id)
        self._display_combat_events.append(display_event)
        event = display_event
        player = self._players.setdefault(event.source_id, _PlayerTotals())
        player.name = event.source_name or player.name or event.source_id
        player.job = event.job or player.job
        player.first_ms = event.timestamp_ms if player.first_ms is None else min(player.first_ms, event.timestamp_ms)
        player.last_ms = event.timestamp_ms if player.last_ms is None else max(player.last_ms, event.timestamp_ms)
        self._first_ms = event.timestamp_ms if self._first_ms is None else min(self._first_ms, event.timestamp_ms)
        self._last_ms = event.timestamp_ms if self._last_ms is None else max(self._last_ms, event.timestamp_ms)

        skill_key = event.skill_id or event.skill_name or "unknown"
        skill = player.skills.setdefault(skill_key, _SkillTotals(name=event.skill_name or skill_key))
        skill.name = event.skill_name or skill.name
        skill.hits += 1
        skill.attribution[event.attribution_method or "recorded"] += 1
        if not event.healing:
            player.hits += 1
        if is_critical_rate_sample(event):
            skill.critical_samples += 1
            player.critical_samples += 1
            if event.critical:
                skill.critical_hits += 1
                player.critical_hits += 1
        if event.healing:
            skill.healing += event.amount
            player.healing += event.amount
        else:
            skill.damage += event.amount
            player.damage += event.amount
            origin = self._first_ms or 0
            second = max(0, (event.timestamp_ms - origin) // 1000)
            self._damage_by_second[second] += event.amount
            player.damage_by_second[second] += event.amount
            if event.target_id:
                self._overlay_target_damage[event.target_id] += event.amount
                if event.amount > 0:
                    self.observe_attack_target(
                        event.source_id,
                        event.target_id,
                        event.timestamp_ms,
                        amount=event.amount,
                    )
                self._overlay_target_hits[event.target_id] += 1
                bounds = self._overlay_target_bounds.setdefault(
                    event.target_id,
                    [event.timestamp_ms, event.timestamp_ms],
                )
                bounds[0] = min(bounds[0], event.timestamp_ms)
                bounds[1] = max(bounds[1], event.timestamp_ms)
            self._overlay_player_damage_by_second[event.source_id][
                max(0, event.timestamp_ms // 1000)
            ] += event.amount
            self._apply_freeze_hit(event)

    def _normalize_transformed_basic_attack(
        self, event: CombatEvent
    ) -> tuple[CombatEvent, SkillCastEvent | None]:
        """Split form-buff casts from normal attacks resolved as that skill.

        Holy Hammer casts are established exclusively by the real 3224
        ``skill_action`` branch above.  Its damage can share the attack's
        display bucket, but cannot create another cast.  Crippling Punisher
        remains status-gated until its transformed action packet is calibrated.
        Raw amounts and packet evidence are unchanged in both cases.
        """

        if event.healing or not event.source_id or not event.target_id:
            return event, None
        skill_id = str(event.skill_id or "")
        attack_name = _TRANSFORMED_BASIC_ATTACK_DAMAGE_NAMES.get(skill_id)
        if attack_name is not None:
            return (
                replace(
                    event,
                    skill_id=f"{skill_id}:basic_attack",
                    skill_name=attack_name,
                ),
                None,
            )

        definition = _DAMAGE_TRANSFORMED_BASIC_ATTACKS.get(skill_id)
        if definition is None or event.target_id == event.source_id:
            return event, None
        buff_name, attack_name, effect_code = definition
        active = False
        for track in self._player_buffs.values():
            if (
                track.target_id != event.source_id
                or str(track.skill_id) != str(event.skill_id)
                or track.effect_code != effect_code
                or track.skill_name not in {buff_name, f"{buff_name}EX"}
            ):
                continue
            if any(
                start <= event.timestamp_ms < end
                for start, end in track.intervals
            ):
                active = True
                break
        if not active:
            return event, None

        attack_id = f"{event.skill_id}:basic_attack"
        transformed = replace(
            event,
            skill_id=attack_id,
            skill_name=attack_name,
        )
        key = (event.source_id, attack_id)
        previous = self._last_transformed_attack_ms.get(key)
        cast: SkillCastEvent | None = None
        if previous is None or event.timestamp_ms - previous > _TRANSFORMED_ATTACK_DEDUPE_MS:
            cast = SkillCastEvent(
                timestamp_ms=event.timestamp_ms,
                source_id=event.source_id,
                source_name=event.source_name,
                job=event.job,
                skill_id=attack_id,
                skill_name=attack_name,
                packet_kind="transformed_basic_attack",
            )
            self._last_transformed_attack_ms[key] = event.timestamp_ms
        return transformed, cast

    def _apply_freeze_hit(self, event: CombatEvent) -> None:
        # Freeze durability is a game-facing Hit counter, not a damage-event
        # counter. DoT/status/mastery/additional-damage records remain valid
        # damage, but cannot shatter a layer without protocol-backed hit
        # evidence. ``direct_hit_overflow`` carries that evidence and therefore
        # passes this gate; it also remains native skill damage unless a
        # separate effect-result record is independently proven.
        if not event.target_id or not event.has_real_hit_evidence:
            return
        for track in tuple(self._freeze_tracks_by_target.get(event.target_id, ())):
            if (
                track.effect_key != "冰冻层"
                or track.target_id != event.target_id
                or not track.intervals
            ):
                continue
            start_ms, end_ms = track.intervals[-1]
            if not start_ms <= event.timestamp_ms < end_ms:
                continue
            hit_limit = max(
                1, track.break_hit_limit or _DEFAULT_FREEZE_BREAK_HITS
            )
            track.hits_since_refresh, shattered = advance_freeze_hit_count(
                event.timestamp_ms,
                start_ms,
                end_ms,
                track.hits_since_refresh,
                hit_limit,
            )
            if not shattered:
                continue
            shatter = BossStatusEvent(
                timestamp_ms=event.timestamp_ms,
                target_id=track.target_id,
                source_id=track.source_id,
                source_name=track.source_name,
                job=track.job,
                skill_id=track.skill_id,
                skill_name=track.skill_name,
                effect_key=track.effect_key,
                effect_label=f"叠冰状态（推断：{hit_limit} Hit 碎冰）",
                value=track.value,
                duration_ms=0,
                status_kind=track.status_kind,
                effect_code=track.effect_code,
                parameters=track.parameters,
                stackable=track.stackable,
                stack_group=track.stack_group,
                operation="remove",
            )
            self._boss_status_events.append(shatter)
            self._ingest_status(shatter, self._boss_effects)

    def _normalized_visible_events(
        self,
        visible_players_by_id: dict[str, PlayerSnapshot],
        combat_events: tuple[CombatEvent, ...],
        cast_events: tuple[SkillCastEvent, ...],
    ) -> tuple[tuple[CombatEvent, ...], tuple[SkillCastEvent, ...]]:
        visible_signature = tuple(
            sorted(
                (source_id, player.name, player.job)
                for source_id, player in visible_players_by_id.items()
            )
        )
        if (
            visible_signature != self._normalized_visible_signature
            or self._normalized_attribution_revision != self._attribution_revision
        ):
            same_visible_players = (
                visible_signature == self._normalized_visible_signature
            )
            self._normalized_visible_signature = visible_signature
            next_combat_rows: list[CombatEvent] = []
            for event in combat_events:
                player = visible_players_by_id.get(event.source_id)
                if player is None:
                    # Unknown/system recovery still belongs to a confirmed
                    # recipient's HP ledger. It is not a ninth party member
                    # and must not disappear before healing/rescue scoring.
                    if event.healing and event.target_id in visible_players_by_id:
                        next_combat_rows.append(event)
                    continue
                next_combat_rows.append(
                    event
                    if (
                        event.source_name == player.name
                        and event.job == player.job
                    )
                    else replace(
                        event,
                        source_name=player.name,
                        job=player.job,
                    )
                )
            next_combat_events = tuple(next_combat_rows)
            next_cast_rows: list[SkillCastEvent] = []
            for event in cast_events:
                player = visible_players_by_id.get(event.source_id)
                if player is None:
                    continue
                next_cast_rows.append(
                    event
                    if (
                        event.source_name == player.name
                        and event.job == player.job
                    )
                    else replace(
                        event,
                        source_name=player.name,
                        job=player.job,
                    )
                )
            next_cast_events = tuple(next_cast_rows)
            if same_visible_players:
                reusable_combat = min(
                    len(self._normalized_combat_events),
                    len(next_combat_events),
                )
                combat_prefix = 0
                while (
                    combat_prefix < reusable_combat
                    and self._normalized_combat_events[combat_prefix]
                    == next_combat_events[combat_prefix]
                ):
                    combat_prefix += 1
                next_combat_events = (
                    self._normalized_combat_events[:combat_prefix]
                    + next_combat_events[combat_prefix:]
                )

                reusable_casts = min(
                    len(self._normalized_skill_cast_events),
                    len(next_cast_events),
                )
                cast_prefix = 0
                while (
                    cast_prefix < reusable_casts
                    and self._normalized_skill_cast_events[cast_prefix]
                    == next_cast_events[cast_prefix]
                ):
                    cast_prefix += 1
                next_cast_events = (
                    self._normalized_skill_cast_events[:cast_prefix]
                    + next_cast_events[cast_prefix:]
                )
            self._normalized_combat_events = next_combat_events
            self._normalized_skill_cast_events = next_cast_events
            self._normalized_combat_event_count = len(combat_events)
            self._normalized_skill_cast_event_count = len(cast_events)
            self._normalized_attribution_revision = self._attribution_revision

        return self._normalized_combat_events, self._normalized_skill_cast_events

    def _reconciled_damage(self) -> AttributionLedgerResult:
        # Replay caches serialized before the EX response was recognized have
        # the raw parameter but not the compact identity index. Upgrade that
        # index once, without rescanning the large parameter stream per live
        # snapshot or forcing users to decode the capture again.
        if not getattr(self, "_shock_identity_ex_rehydrated", False):
            existing = {id(row) for row in self._shock_damage_identity_parameters}
            self._shock_damage_identity_parameters.extend(
                row for row in self._skill_parameter_events
                if id(row) not in existing
                and str(row.parameters or "").strip() == "Skill_LightningCharger_EX_Attack"
                and is_shock_damage_identity_parameter(row)
            )
            self._shock_identity_ex_rehydrated = True
        input_signature = (
            len(self._raw_damage_ledger.events),
            len(self._skill_cast_events),
            len(self._shock_damage_identity_parameters),
        )
        if any(
            str(row.skill_id or row.raw_skill_id or "").strip() == "3202"
            for row in self._shock_damage_identity_parameters
        ):
            # Older replay caches can contain the response but a previously
            # reconciled ledger without its direct37 pulse attribution.
            # Version only affected captures so unrelated records keep their
            # warm-cache path unchanged.
            input_signature += ("lightning-direct-pulse-v2",)
        if self._attribution_input_signature != input_signature:
            identity_reconciled_events = reconcile_shock_damage_identity(
                tuple(
                    event
                    for event in self._display_combat_events
                    if not event.healing
                ),
                self._shock_damage_identity_parameters,
            )
            self._attribution_result = self._skill_attribution_ledger.reconcile(
                identity_reconciled_events,
                self._skill_cast_events,
            )
            self._attribution_input_signature = input_signature
            self._attribution_revision += 1
        return self._attribution_result

    @staticmethod
    def _composition_skill_identity(event: CombatEvent) -> tuple[str, str]:
        """Return the one canonical UI bucket for a reconciled V2 event."""

        display_as_unresolved = (
            not event.healing and event.skill_id in {"", "-1"}
        )
        display_as_extra = (
            not event.healing
            and is_folded_additional_damage_event(event)
        )
        skill_id = (
            "unresolved:damage"
            if display_as_unresolved
            else "supplemental:status_damage"
            if display_as_extra
            else event.skill_id
        )
        skill_name = (
            "待识别伤害"
            if display_as_unresolved
            else "额外伤害"
            if display_as_extra
            else event.skill_name
        )
        if event.attribution_method == "status_damage_skill":
            # The server names these team-proc HP deltas authoritatively in
            # the record tail.  Keep one independent composition row under
            # the actual attacking player, while making it visually distinct
            # from the non-damaging buff cast bearing the same skill name.
            skill_name = additional_damage_composition_name(skill_id, skill_name)
        skill_key = skill_id or skill_name or "unknown"
        return skill_key, skill_name or skill_key

    @classmethod
    def _accumulate_composition_event(
        cls,
        composition_skills: dict[str, dict[str, _SkillTotals]],
        event: CombatEvent,
    ) -> None:
        skill_key, skill_name = cls._composition_skill_identity(event)
        skills = composition_skills.setdefault(event.source_id, {})
        skill = skills.get(skill_key)
        if skill is None:
            skill = _SkillTotals(name=skill_name)
            skills[skill_key] = skill
        skill.name = skill_name or skill.name
        skill.hits += 1
        skill.attribution[event.attribution_method or "recorded"] += 1
        if event.trigger_skill_id or event.trigger_skill_name:
            trigger_key = (
                event.trigger_skill_id or event.trigger_skill_name,
                event.trigger_skill_name or event.trigger_skill_id,
                event.trigger_attribution_method or "linked_trigger",
                (
                    "exact"
                    if event.trigger_attribution_confidence >= 0.95
                    else "candidate"
                ),
            )
            skill.trigger_hits[trigger_key] += 1
            if not event.healing:
                skill.trigger_damage[trigger_key] += event.amount
        if is_critical_rate_sample(event):
            skill.critical_samples += 1
            if event.critical:
                skill.critical_hits += 1
        if event.healing:
            skill.healing += event.amount
        else:
            skill.damage += event.amount

    def _v2_damage_composition(
        self,
    ) -> tuple[AttributionLedgerResult, dict[str, dict[str, _SkillTotals]]]:
        """Materialize evidence-V2 damage buckets for every live consumer."""

        attribution = self._reconciled_damage()
        if self._damage_composition_revision != self._attribution_revision:
            next_events = attribution.events
            previous_events = self._damage_composition_events
            reusable_prefix = len(previous_events)
            can_extend = reusable_prefix <= len(next_events) and all(
                previous is current or previous == current
                for previous, current in zip(
                    previous_events,
                    next_events[:reusable_prefix],
                )
            )
            if can_extend:
                composition = self._damage_composition_skills
                pending_events = next_events[reusable_prefix:]
            else:
                composition = {}
                pending_events = next_events
            for event in pending_events:
                self._accumulate_composition_event(composition, event)
            self._damage_composition_skills = composition
            self._damage_composition_events = next_events
            self._damage_composition_revision = self._attribution_revision
        return attribution, self._damage_composition_skills

    def _v2_live_composition(
        self,
    ) -> tuple[AttributionLedgerResult, dict[str, dict[str, _SkillTotals]]]:
        """Return copy-safe V2 damage buckets plus provisional live healing."""

        attribution, damage_skills = self._v2_damage_composition()
        composition: dict[str, dict[str, _SkillTotals]] = {
            source_id: {
                skill_id: _SkillTotals(
                    name=skill.name,
                    damage=skill.damage,
                    healing=skill.healing,
                    hits=skill.hits,
                    critical_hits=skill.critical_hits,
                    critical_samples=skill.critical_samples,
                    attribution=Counter(skill.attribution),
                    trigger_hits=Counter(skill.trigger_hits),
                    trigger_damage=Counter(skill.trigger_damage),
                )
                for skill_id, skill in skills.items()
            }
            for source_id, skills in damage_skills.items()
        }
        # Healing has a separate HP-backed final ledger.  During capture keep
        # its former provisional packet view, but never let those rows restore
        # the old damage identity that V2 just rejected.
        for event in self._display_combat_events:
            if event.healing:
                self._accumulate_composition_event(composition, event)
        return attribution, composition

    def _ingest_boss_status(self, event: BossStatusEvent) -> None:
        self._boss_status_indexes_by_target[event.target_id].append(len(self._boss_status_events))
        self._boss_status_events.append(event)
        self._ingest_status(event, self._boss_effects)

    def _ingest_status(
        self,
        event: BossStatusEvent,
        tracks: dict[tuple[object, ...], _BossEffectTrack],
    ) -> bool:
        if not event.target_id or not event.skill_id:
            return False
        self._first_ms = (
            event.timestamp_ms
            if self._first_ms is None
            else min(self._first_ms, event.timestamp_ms)
        )
        self._last_ms = (
            event.timestamp_ms
            if self._last_ms is None
            else max(self._last_ms, event.timestamp_ms)
        )
        track_key = (
            event.target_id,
            event.source_id,
            event.skill_id,
            event.effect_key,
            _effect_value_signature(event.value, event.parameters),
        )
        track = tracks.get(track_key)
        packet_aura = tracks is self._player_buffs and event.skill_id in AURA_SKILL_KEYS and event.effect_instance_id > 0
        if packet_aura and event.operation == "remove":
            if track is None or track.effect_instance_id != event.effect_instance_id:
                return False
        before = None
        if track is not None:
            before = (
                track.effect_key,
                track.effect_label,
                track.value,
                track.status_kind,
                track.effect_code,
                track.parameters,
                track.stackable,
                track.stack_group,
                track.target_id,
                track.source_id,
                track.source_name,
                track.job,
                track.skill_id,
                track.skill_name,
                track.permanent,
                track.value_source,
                track.duration_source,
                tuple(tuple(interval) for interval in track.intervals),
                track.last_refresh_ms,
            )
        if track is None:
            track = _BossEffectTrack(
                effect_key=event.effect_key,
                effect_label=event.effect_label,
                value=event.value,
                status_kind=event.status_kind,
                effect_code=event.effect_code,
                parameters=event.parameters,
                stackable=event.stackable,
                stack_group=event.stack_group,
                target_id=event.target_id,
                source_id=event.source_id,
                source_name=event.source_name or event.source_id,
                job=event.job,
                skill_id=event.skill_id,
                skill_name=event.skill_name,
                permanent=event.permanent,
                value_source=event.value_source,
                duration_source=event.duration_source,
            )
            tracks[track_key] = track
            if tracks is self._boss_effects and event.effect_key == "冰冻层":
                self._freeze_tracks_by_target[event.target_id].append(track)
        else:
            track.source_name = event.source_name or track.source_name
            track.job = event.job or track.job
            track.skill_name = event.skill_name or track.skill_name
            track.value = event.value if event.value is not None else track.value
            track.parameters = event.parameters or track.parameters
            track.status_kind = event.status_kind or track.status_kind
            track.permanent = track.permanent or event.permanent
            track.value_source = event.value_source or track.value_source
            track.duration_source = event.duration_source or track.duration_source

        if packet_aura:
            track.effect_instance_id = event.effect_instance_id

        if event.operation == "remove":
            if track.intervals:
                track.intervals[-1][1] = min(
                    track.intervals[-1][1], event.timestamp_ms
                )
            track.last_refresh_ms = event.timestamp_ms
            after = (
                track.effect_key,
                track.effect_label,
                track.value,
                track.status_kind,
                track.effect_code,
                track.parameters,
                track.stackable,
                track.stack_group,
                track.target_id,
                track.source_id,
                track.source_name,
                track.job,
                track.skill_id,
                track.skill_name,
                track.permanent,
                track.value_source,
                track.duration_source,
                tuple(tuple(interval) for interval in track.intervals),
                track.last_refresh_ms,
            )
            return before != after

        if event.effect_key == "冰冻层":
            track.break_hit_limit = _freeze_break_hit_limit(event)
            track.hits_since_refresh = 0

        duration_override_ms = (
            round(team_effect_duration_override(event.skill_name) * 1000)
            if event.permanent
            else 0
        )
        end_ms = (
            event.timestamp_ms + max(1, duration_override_ms)
            if duration_override_ms > 0
            else (
                _OPEN_ENDED_STATUS_MS
                if event.permanent
                else event.timestamp_ms + max(1, event.duration_ms)
            )
        )
        if track.intervals and event.timestamp_ms <= track.intervals[-1][1]:
            track.intervals[-1][1] = max(track.intervals[-1][1], end_ms)
        else:
            track.intervals.append([event.timestamp_ms, end_ms])
        track.last_refresh_ms = event.timestamp_ms
        track.refresh_count += 1
        after = (
            track.effect_key,
            track.effect_label,
            track.value,
            track.status_kind,
            track.effect_code,
            track.parameters,
            track.stackable,
            track.stack_group,
            track.target_id,
            track.source_id,
            track.source_name,
            track.job,
            track.skill_id,
            track.skill_name,
            track.permanent,
            track.value_source,
            track.duration_source,
            tuple(tuple(interval) for interval in track.intervals),
            track.last_refresh_ms,
        )
        return before != after

    def _close_player_buffs_at(self, target_id: str, timestamp_ms: int, *, visibility_only: bool = False) -> bool:
        """Close effects on confirmed death, never on visibility alone."""
        if visibility_only:
            # Also protect late-identity rebuilds of legacy visibility
            # boundaries; becoming visible again cannot restore a truncated
            # application that the server never retransmitted.
            return False
        if not target_id:
            return False
        changed = False
        for track in self._player_buffs.values():
            if track.target_id != target_id:
                continue
            for interval in track.intervals:
                if interval[0] <= timestamp_ms < interval[1]:
                    interval[1] = timestamp_ms
                    changed = True
        if changed:
            self._overlay_player_buff_state_revision += 1
        return changed

    def _ingest_player_buff_clear(
        self, event: BossActionEvent
    ) -> tuple[dict[str, str], ...]:
        """Close finite Buffs and return the applications actually removed."""
        if not self._is_player_buff_clear_event(event):
            return ()
        affected = set(event.affected_target_ids)
        closed: dict[tuple[str, str, str], dict[str, str]] = {}
        for track in self._player_buffs.values():
            if (
                track.target_id not in affected
                or track.permanent
                or _is_player_buff_clear_immune(track)
            ):
                continue
            removed = False
            for interval in track.intervals:
                if interval[0] <= event.timestamp_ms < interval[1]:
                    interval[1] = event.timestamp_ms
                    removed = True
            if removed:
                key = (track.target_id, track.skill_id, track.skill_name)
                closed[key] = {
                    "target_id": track.target_id,
                    "skill_id": track.skill_id,
                    "skill_name": track.skill_name,
                }
        if closed:
            self._overlay_player_buff_state_revision += 1
        return tuple(closed.values())

    @staticmethod
    def _is_player_buff_clear_event(event: BossActionEvent) -> bool:
        return bool(event.affected_target_ids) and (
            event.packet_kind == "boss_buff_clear_status"
            or event.skill_index in _PLAYER_BUFF_CLEAR_BOSS_SKILL_IDS
        )

    @staticmethod
    def _summarize_active_boss_effects(
        active: list[_BossEffectTrack],
    ) -> tuple[tuple[str, float], ...]:
        inputs = [
            EffectInput(
                key=track.effect_key,
                value=track.value or 0.0,
                skill_id=track.skill_id,
                skill_name=track.skill_name,
                job=track.job,
                source_id=track.source_id,
                target_id=track.target_id,
                stack_group=track.stack_group,
                stackable=track.stackable,
                data_source=track.value_source,
            )
            for track in active
            if track.value is not None
        ]
        return CombatAggregator._effect_resolution_summary(
            EFFECT_STACKING_ENGINE.resolve_active(inputs, 0.0).values
        )

    @staticmethod
    def _effect_resolution_summary(
        values: Mapping[str, float],
    ) -> tuple[tuple[str, float], ...]:
        keys = (
            "减火抗%",
            "减水抗%",
            "减光抗%",
            "减暗抗%",
            "减全抗%",
            "增伤%",
            "物理增伤合计%",
            "魔法增伤合计%",
            "减防御%",
            "减物理防御%",
            "减魔法防御%",
            "减致命抵抗%",
            "减攻击%",
            "冰冻层",
        )
        return tuple((key, float(values.get(key, 0.0))) for key in keys)

    @staticmethod
    def _boss_effect_inputs(
        tracks: list[_BossEffectTrack],
    ) -> tuple[EffectInput, ...]:
        rows: list[EffectInput] = []
        for track_index, track in enumerate(tracks):
            if track.value is None:
                continue
            for interval_index, (start_ms, end_ms) in enumerate(track.intervals):
                rows.append(
                    EffectInput(
                        key=track.effect_key,
                        value=track.value,
                        skill_id=track.skill_id,
                        skill_name=track.skill_name,
                        job=track.job,
                        source_id=track.source_id,
                        target_id=track.target_id,
                        instance_id=f"{track_index}:{interval_index}",
                        start=float(start_ms),
                        end=float(end_ms),
                        stack_group=track.stack_group,
                        stackable=track.stackable,
                        data_source=track.value_source or "packet",
                    )
                )
        return tuple(rows)

    @classmethod
    def _boss_effect_summary(
        cls, tracks: list[_BossEffectTrack], current_ms: int
    ) -> tuple[tuple[str, float], ...]:
        resolution = EFFECT_STACKING_ENGINE.resolve_active(
            cls._boss_effect_inputs(tracks), float(current_ms)
        )
        return cls._effect_resolution_summary(resolution.values)

    @classmethod
    def _boss_effect_series(
        cls, tracks: list[_BossEffectTrack], current_ms: int
    ) -> tuple[tuple[str, tuple[tuple[float, float], ...]], ...]:
        keys = tuple(key for key, _value in cls._effect_resolution_summary({}))
        inputs = cls._boss_effect_inputs(tracks)
        boundaries = {0, current_ms}
        for row in inputs:
            if 0 <= row.start <= current_ms:
                boundaries.add(int(row.start))
            if 0 <= row.end <= current_ms:
                boundaries.add(int(row.end))
        points: dict[str, list[tuple[float, float]]] = {key: [] for key in keys}
        previous: dict[str, float] = {}
        for timestamp_ms, values in EFFECT_STACKING_ENGINE.resolve_value_series(
            inputs,
            sorted(boundaries),
        ):
            seconds = timestamp_ms / 1000.0
            for key in keys:
                value = float(values.get(key, 0.0))
                if key not in previous or previous[key] != value:
                    points[key].append((seconds, value))
                    previous[key] = value

        return tuple((key, tuple(points[key])) for key in keys)

    def _player_buff_series_snapshots(
        self, current_ms: int
    ) -> tuple[PlayerBuffSeriesSnapshot, ...]:
        """Expose complete buff intervals without the detail-table event cap."""
        rows: list[PlayerBuffSeriesSnapshot] = []
        for track in self._player_buffs.values():
            intervals = self._merge_intervals(
                [(start, end) for start, end in track.intervals], current_ms
            )
            if not intervals:
                continue
            target_name, target_job = self._actor_context.get(
                track.target_id, ("", "")
            )
            source_name, source_job = self._actor_context.get(
                track.source_id, ("", "")
            )
            rows.append(
                PlayerBuffSeriesSnapshot(
                    target_id=track.target_id,
                    target_name=target_name or track.target_id,
                    target_job=target_job,
                    source_id=track.source_id,
                    source_name=source_name or track.source_name or track.source_id,
                    job=source_job or track.job,
                    skill_id=track.skill_id,
                    skill_name=track.skill_name,
                    key=track.effect_key,
                    label=track.effect_label,
                    value=track.value,
                    status_kind=track.status_kind,
                    intervals=tuple(
                        (start / 1000.0, end / 1000.0)
                        for start, end in intervals
                    ),
                    open_ended=any(
                        start <= current_ms < end
                        and end >= _OPEN_ENDED_STATUS_MS
                        for start, end in track.intervals
                    ),
                    value_source=track.value_source,
                    duration_source=track.duration_source,
                    effect_code=track.effect_code,
                    effect_instance_id=track.effect_instance_id,
                    state_source=("packet_instance" if track.skill_id in AURA_SKILL_KEYS
                                  and track.effect_instance_id > 0 else "received_status"),
                )
            )
        rows.sort(
            key=lambda row: (
                row.target_name,
                row.status_kind,
                row.label or row.key,
                row.skill_name,
                row.source_name,
            )
        )
        return tuple(rows)

    @staticmethod
    def _merge_intervals(
        intervals: list[tuple[int, int]], current_ms: int
    ) -> list[tuple[int, int]]:
        clipped = sorted(
            (
                max(0, start),
                (
                    current_ms + 1
                    if end >= _OPEN_ENDED_STATUS_MS
                    else max(0, end)
                ),
            )
            for start, end in intervals
            if start <= current_ms and end > 0
        )
        merged: list[list[int]] = []
        for start, end in clipped:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    def _effect_snapshots(
        self,
        tracks: dict[tuple[object, ...], _BossEffectTrack],
        current_ms: int,
        *,
        preserve_parameters: bool,
        group_by_target: bool,
        target_ids: frozenset[str] | None = None,
        target_identity_keys: frozenset[tuple[str, str]] | None = None,
        coverage_windows_by_identity: dict[
            tuple[str, str], list[tuple[int, int]]
        ]
        | None = None,
        group_by_runtime_target: bool = False,
    ) -> tuple[BossEffectSnapshot, ...]:
        groups: dict[tuple[object, ...], list[_BossEffectTrack]] = defaultdict(list)
        for track in tracks.values():
            if not track.intervals:
                continue
            if target_ids is not None and track.target_id not in target_ids:
                continue
            normalized_skill = "".join(track.skill_name.upper().split())
            if normalized_skill.endswith("EX"):
                normalized_skill = normalized_skill[:-2]
            key: tuple[object, ...] = (
                normalized_skill or track.skill_id,
                track.effect_key,
                track.status_kind,
                _effect_value_signature(track.value, track.parameters),
            )
            if group_by_target:
                target_name = self._actor_context.get(
                    track.target_id, ("", "")
                )[0]
                canonical_target_identity = self._target_identity_key(
                    track.target_id,
                    target_name,
                    self._target_max_hp.get(track.target_id, 0),
                )
                if (
                    target_identity_keys is not None
                    and canonical_target_identity not in target_identity_keys
                ):
                    continue
                target_identity = (
                    ("actor", track.target_id)
                    if group_by_runtime_target
                    else canonical_target_identity
                )
                key = (
                    target_identity,
                    *key,
                )
            if preserve_parameters:
                key += (track.effect_code, track.parameters)
                if track.skill_id in AURA_SKILL_KEYS:
                    key += (track.source_id,)
            groups[key].append(track)

        rows: list[BossEffectSnapshot] = []
        for grouped_tracks in groups.values():
            representative = max(
                grouped_tracks,
                key=lambda track: (
                    track.last_refresh_ms,
                    track.value if track.value is not None else float("-inf"),
                ),
            )
            intervals = self._merge_intervals(
                [
                    (start, end)
                    for track in grouped_tracks
                    for start, end in track.intervals
                ],
                current_ms,
            )
            if not intervals:
                continue
            # Open-ended intervals are intentionally clipped to current_ms by
            # _merge_intervals. Preserve their identity for consumers that
            # need to display a live status beyond the snapshot boundary.
            open_ended = any(
                any(
                    start <= current_ms < end
                    and end >= _OPEN_ENDED_STATUS_MS
                    for start, end in track.intervals
                )
                for track in grouped_tracks
            )
            target_name, target_job = self._actor_context.get(
                representative.target_id, ("", "")
            )
            target_identity = (
                ("actor", representative.target_id)
                if group_by_runtime_target
                else self._target_identity_key(
                    representative.target_id,
                    target_name,
                    self._target_max_hp.get(representative.target_id, 0),
                )
            )
            coverage_windows = (
                coverage_windows_by_identity.get(target_identity, [])
                if coverage_windows_by_identity
                else []
            )
            if coverage_windows:
                measured_intervals: list[tuple[int, int]] = []
                for start, end in intervals:
                    for window_start, window_end in coverage_windows:
                        overlap_start = max(start, window_start)
                        overlap_end = min(end, window_end, current_ms)
                        if overlap_end > overlap_start:
                            measured_intervals.append(
                                (overlap_start, overlap_end)
                            )
                measured_intervals = self._merge_intervals(
                    measured_intervals, current_ms
                )
                coverage_denominator = max(
                    1,
                    sum(
                        max(0, min(end, current_ms) - start)
                        for start, end in coverage_windows
                        if start < current_ms
                    ),
                )
            else:
                measured_intervals = intervals
                coverage_denominator = max(1, current_ms)
            uptime_ms = sum(
                max(0, min(end, current_ms) - min(start, current_ms))
                for start, end in measured_intervals
            )
            active_intervals = [
                (start, end) for start, end in intervals if start <= current_ms < end
            ]
            start_ms = min(start for start, _end in intervals)
            end_ms = (
                max(end for _start, end in active_intervals)
                if active_intervals
                else max(end for _start, end in intervals)
            )
            source_ids = {track.source_id for track in grouped_tracks}
            source_contexts = [
                self._actor_context.get(track.source_id, ("", ""))
                for track in grouped_tracks
            ]
            jobs = {
                context_job or track.job
                for track, (_context_name, context_job) in zip(
                    grouped_tracks, source_contexts
                )
                if context_job or track.job
            }
            source_names = {
                context_name or track.source_name
                for track, (context_name, _context_job) in zip(
                    grouped_tracks, source_contexts
                )
                if context_name or track.source_name
            }
            values = [
                track.value for track in grouped_tracks if track.value is not None
            ]
            value_sources = {
                track.value_source for track in grouped_tracks if track.value_source
            }
            duration_sources = {
                track.duration_source for track in grouped_tracks if track.duration_source
            }
            value_source = (
                "packet"
                if value_sources == {"packet"}
                else "preset_fallback" if value_sources else ""
            )
            duration_source = (
                "packet"
                if duration_sources == {"packet"}
                else "preset_fallback" if duration_sources else ""
            )
            rows.append(
                BossEffectSnapshot(
                    key=representative.effect_key,
                    label=representative.effect_label,
                    source_id=(next(iter(source_ids)) if len(source_ids) == 1 else ""),
                    source_name=(
                        next(iter(source_names))
                        if len(source_names) == 1
                        else (next(iter(jobs)) if len(jobs) == 1 else "多个来源")
                    ),
                    job=(
                        next(iter(jobs))
                        if len(jobs) == 1
                        else " / ".join(sorted(jobs))
                    ),
                    skill_id=representative.skill_id,
                    skill_name=representative.skill_name,
                    value=max(values) if values else None,
                    status_kind=representative.status_kind,
                    effect_code=representative.effect_code,
                    parameters=representative.parameters,
                    data_source=(
                        "抓包数据"
                        if value_source == "packet" and duration_source == "packet"
                        else "抓包优先 / 预设兜底"
                    ),
                    start_seconds=start_ms / 1000.0,
                    end_seconds=end_ms / 1000.0,
                    last_refresh_seconds=max(
                        track.last_refresh_ms for track in grouped_tracks
                    )
                    / 1000.0,
                    refresh_count=sum(track.refresh_count for track in grouped_tracks),
                    active=bool(active_intervals),
                    uptime_seconds=uptime_ms / 1000.0,
                    coverage=uptime_ms / coverage_denominator,
                    target_id=representative.target_id,
                    target_name=target_name or representative.target_id,
                    target_job=target_job,
                    intervals=tuple(
                        (start / 1000.0, end / 1000.0)
                        for start, end in intervals
                    ),
                    value_source=value_source,
                    duration_source=duration_source,
                    stackable=representative.stackable,
                    stack_group=representative.stack_group,
                )
            )

        if tracks is not self._boss_effects:
            rows.sort(
                key=lambda row: (
                    not row.active,
                    row.target_job,
                    row.job,
                    row.skill_name,
                    row.key,
                )
            )
            return tuple(rows)

        interval_inputs: list[EffectInput] = []
        interval_owner: dict[str, int] = {}
        for row_index, row in enumerate(rows):
            if row.value is None:
                continue
            for interval_index, (start, end) in enumerate(row.intervals):
                instance_id = f"{row_index}:{interval_index}"
                interval_owner[instance_id] = row_index
                interval_inputs.append(
                    EffectInput(
                        key=row.key,
                        value=row.value,
                        skill_id=row.skill_id,
                        skill_name=row.skill_name,
                        job=row.job,
                        source_id=row.source_id,
                        target_id=row.target_id,
                        instance_id=instance_id,
                        start=start,
                        end=end,
                        stack_group=row.stack_group,
                        stackable=row.stackable,
                        data_source=row.value_source,
                    )
                )
        interval_resolution = EFFECT_STACKING_ENGINE.resolve_intervals(
            interval_inputs,
            end_seconds=current_ms / 1000.0,
            include_tied_max=True,
        )
        effective_by_row: dict[int, list[tuple[float, float]]] = defaultdict(list)
        rejected_by_row: dict[int, list[str]] = defaultdict(list)
        suppressed_by_row: dict[int, list[str]] = defaultdict(list)
        for instance_id, effective_intervals in (
            interval_resolution.effective_intervals.items()
        ):
            row_index = interval_owner.get(instance_id)
            if row_index is not None:
                effective_by_row[row_index].extend(effective_intervals)
        for instance_id, reason in interval_resolution.rejected_reasons.items():
            row_index = interval_owner.get(instance_id)
            if row_index is not None:
                rejected_by_row[row_index].append(reason)
        for instance_id, reason in interval_resolution.suppressed_reasons.items():
            row_index = interval_owner.get(instance_id)
            if row_index is not None:
                suppressed_by_row[row_index].append(reason)

        resolved_rows: list[BossEffectSnapshot] = []
        for row_index, row in enumerate(rows):
            if row.value is None:
                resolved_rows.append(
                    replace(
                        row,
                        effective_intervals=row.intervals,
                        effective_coverage=row.coverage,
                        effective_state="仅观察状态",
                        effective_reason="该状态没有可参与数值结算的百分比",
                    )
                )
                continue
            effective = tuple(
                (start, end)
                for start, end in self._merge_intervals(
                    [
                        (round(start * 1000), round(end * 1000))
                        for start, end in effective_by_row.get(row_index, ())
                    ],
                    current_ms,
                )
            )
            effective = tuple(
                (start / 1000.0, end / 1000.0) for start, end in effective
            )
            observed_seconds = sum(
                max(0.0, end - start) for start, end in row.intervals
            )
            effective_seconds = sum(
                max(0.0, end - start) for start, end in effective
            )
            if effective_seconds <= 1e-9:
                if rejected_by_row.get(row_index):
                    state = "申请被拒绝"
                    reason = rejected_by_row[row_index][0]
                else:
                    state = "同组受压制"
                    reason = suppressed_by_row.get(
                        row_index, ["同组存在更高有效值"]
                    )[0]
            elif effective_seconds + 1e-9 < observed_seconds:
                state = "部分生效"
                reason = "观察区间中仅未被同组更高值压制的部分实际生效"
            else:
                state = "有效"
                reason = "包内状态实际参与结算"

            target_identity = (
                ("actor", row.target_id)
                if group_by_runtime_target
                else self._target_identity_key(
                    row.target_id,
                    row.target_name,
                    self._target_max_hp.get(row.target_id, 0),
                )
            )
            coverage_windows = (
                coverage_windows_by_identity.get(target_identity, [])
                if coverage_windows_by_identity
                else []
            )
            if coverage_windows:
                denominator = sum(
                    max(0, min(end, current_ms) - start)
                    for start, end in coverage_windows
                    if start < current_ms
                ) / 1000.0
                covered = 0.0
                for start, end in effective:
                    for window_start_ms, window_end_ms in coverage_windows:
                        covered += max(
                            0.0,
                            min(end, window_end_ms / 1000.0, current_ms / 1000.0)
                            - max(start, window_start_ms / 1000.0),
                        )
            else:
                denominator = max(0.001, current_ms / 1000.0)
                covered = effective_seconds
            resolved_rows.append(
                replace(
                    row,
                    effective_intervals=effective,
                    effective_coverage=(
                        covered / denominator if denominator > 1e-9 else 0.0
                    ),
                    effective_state=state,
                    effective_reason=reason,
                )
            )
        rows = resolved_rows
        rows.sort(
            key=lambda row: (
                not row.active,
                row.target_job,
                row.job,
                row.skill_name,
                row.key,
            )
        )
        return tuple(rows)

    def _boss_phase_windows(
        self,
        target_identity_keys: frozenset[tuple[str, str]],
        current_ms: int,
        *,
        target_ids: frozenset[str] | None = None,
        group_by_runtime_target: bool = False,
    ) -> dict[tuple[str, str], list[tuple[int, int]]]:
        """Return phase bounds, optionally keeping same-name actors separate."""
        actor_bounds: dict[str, list[int]] = {}
        actor_identities: dict[str, tuple[str, str]] = {}
        for event in self._combat_events:
            if event.healing or not event.target_id:
                continue
            if target_ids is not None and event.target_id not in target_ids:
                continue
            target_name = (
                event.target_name
                or self._actor_context.get(event.target_id, ("", ""))[0]
            )
            identity = self._target_identity_key(
                event.target_id,
                target_name,
                event.target_max_hp
                or self._target_max_hp.get(event.target_id, 0),
            )
            if identity not in target_identity_keys:
                continue
            actor_identities[event.target_id] = identity
            bounds = actor_bounds.setdefault(
                event.target_id, [event.timestamp_ms, event.timestamp_ms]
            )
            bounds[0] = min(bounds[0], event.timestamp_ms)
            bounds[1] = max(bounds[1], event.timestamp_ms)

        # A phase can emit status packets before its first readable damage
        # packet (or while invulnerable). Keep that runtime entity in the Boss
        # phase map instead of silently dropping all of its status details.
        for event in self._boss_status_events:
            if not event.target_id:
                continue
            if target_ids is not None and event.target_id not in target_ids:
                continue
            target_name = (
                event.target_name
                or self._actor_context.get(event.target_id, ("", ""))[0]
            )
            identity = self._target_identity_key(
                event.target_id,
                target_name,
                event.target_max_hp
                or self._target_max_hp.get(event.target_id, 0),
            )
            if identity not in target_identity_keys:
                continue
            actor_identities[event.target_id] = identity
            event_end = event.timestamp_ms + max(1, event.duration_ms)
            bounds = actor_bounds.setdefault(
                event.target_id, [event.timestamp_ms, event_end]
            )
            bounds[0] = min(bounds[0], event.timestamp_ms)
            bounds[1] = max(bounds[1], event_end)

        grouped: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        for target_id, (start, end) in actor_bounds.items():
            identity = (
                ("actor", target_id)
                if group_by_runtime_target
                else actor_identities[target_id]
            )
            grouped[identity].append(
                (start, max(start + 1, end))
            )
        return {
            identity: self._merge_intervals(windows, current_ms)
            for identity, windows in grouped.items()
        }

    def _boss_output_windows(
        self,
        target_identity_keys: frozenset[tuple[str, str]],
        current_ms: int,
        *,
        target_ids: frozenset[str] | None = None,
    ) -> dict[tuple[str, str], list[tuple[int, int]]]:
        """Return score-accurate Boss output windows without copying events.

        Lightweight live snapshots intentionally omit the append-only combat
        ledger.  The old fallback used each runtime target's first and last
        packet (and even status duration), which bridged long mechanic/idle
        gaps when output resumed.  Reuse the exact scoring resolver directly
        against the aggregator-owned rows instead.
        """
        del current_ms  # The score resolver owns its one-second end grace.
        from .scoring import TeamScoreEngine

        signature = (
            tuple(sorted(target_identity_keys)),
            tuple(sorted(target_ids or ())),
            len(self._raw_damage_ledger.events),
            len(self._skill_cast_events),
            tuple(sorted(self._players)),
        )
        if signature == getattr(
            self, "_boss_output_window_cache_signature", ()
        ):
            return {
                identity: list(windows)
                for identity, windows in getattr(
                    self, "_boss_output_window_cache", {}
                ).items()
            }

        attribution = self._reconciled_damage()
        grouped: dict[tuple[str, str], list[CombatEvent]] = defaultdict(list)
        for event in attribution.events:
            if event.healing or event.amount <= 0 or not event.target_id:
                continue
            if target_ids is not None and event.target_id not in target_ids:
                continue
            target_name = (
                event.target_name
                or self._actor_context.get(event.target_id, ("", ""))[0]
            )
            identity = self._target_identity_key(
                event.target_id,
                target_name,
                event.target_max_hp
                or self._target_max_hp.get(event.target_id, 0),
            )
            if identity in target_identity_keys:
                grouped[identity].append(event)

        roster_source_ids = tuple(self._players)
        cast_events = attribution.cast_events
        resolved: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for identity in target_identity_keys:
            windows = TeamScoreEngine._output_windows_from_events(
                grouped.get(identity, ()),
                cast_events,
                roster_source_ids,
                selected_target=True,
            )
            resolved[identity] = [
                (round(start * 1000.0), round(end * 1000.0))
                for start, end in windows
                if end > start
            ]
        self._boss_output_window_cache_signature = signature
        self._boss_output_window_cache = {
            identity: tuple(windows)
            for identity, windows in resolved.items()
        }
        return resolved

    def _player_output_windows(
        self,
        visible_source_ids: set[str],
        current_ms: int,
    ) -> dict[tuple[str, str], list[tuple[int, int]]]:
        """Return one-second windows in which each player dealt damage.

        Buff coverage is intended to describe the player's output, so idle,
        transition and mechanism time must not dilute the denominator.  One
        second buckets keep the calculation stable for sparse single-hit
        skills while remaining inexpensive for long captures.
        """
        active_seconds: dict[str, set[int]] = defaultdict(set)
        for event in self._combat_events:
            if (
                event.healing
                or event.amount <= 0
                or event.source_id not in visible_source_ids
            ):
                continue
            active_seconds[event.source_id].add(max(0, event.timestamp_ms // 1000))

        grouped: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        for source_id, seconds in active_seconds.items():
            if not seconds:
                continue
            name = self._actor_context.get(source_id, ("", ""))[0]
            identity = self._target_identity_key(source_id, name)
            ordered = sorted(seconds)
            start = previous = ordered[0]
            for second in ordered[1:]:
                if second == previous + 1:
                    previous = second
                    continue
                grouped[identity].append((start * 1000, (previous + 1) * 1000))
                start = previous = second
            grouped[identity].append((start * 1000, (previous + 1) * 1000))

        return {
            identity: self._merge_intervals(windows, current_ms)
            for identity, windows in grouped.items()
        }

    def _status_event_snapshots(
        self,
        events: list[BossStatusEvent],
        current_ms: int,
        *,
        target_ids: frozenset[str] | None = None,
        max_events: int | None = _MAX_STATUS_DETAIL_EVENTS,
    ) -> tuple[StatusEventSnapshot, ...]:
        rows: list[StatusEventSnapshot] = []
        # Filter before applying the UI bound. Otherwise refresh packets from
        # adds, relics and summons can evict every detail row from an earlier
        # raid phase even though the underlying aggregate tracks are complete.
        visible_events = (
            [event for event in events
             if (not target_ids or event.target_id in target_ids)
             and (events is not self._boss_status_events or event.target_scope != "player")]
        )
        if max_events is not None and max_events > 0 and len(visible_events) > max_events:
            if target_ids:
                events_by_target: dict[
                    tuple[str, str], list[BossStatusEvent]
                ] = defaultdict(list)
                for event in visible_events:
                    target_name = (
                        event.target_name
                        or self._actor_context.get(event.target_id, ("", ""))[0]
                    )
                    events_by_target[
                        self._target_identity_key(
                            event.target_id,
                            target_name,
                            event.target_max_hp
                            or self._target_max_hp.get(event.target_id, 0),
                        )
                    ].append(event)
                per_target_limit = max(1, max_events // len(events_by_target))
                bounded_events = [
                    event
                    for target_events in events_by_target.values()
                    for event in target_events[-per_target_limit:]
                ]
                selected_event_ids = {id(event) for event in bounded_events}
                remaining_capacity = max_events - len(bounded_events)
                if remaining_capacity > 0:
                    omitted_events = [
                        event
                        for event in visible_events
                        if id(event) not in selected_event_ids
                    ]
                    bounded_events.extend(omitted_events[-remaining_capacity:])
                visible_events = sorted(
                    bounded_events, key=lambda event: event.timestamp_ms
                )
            else:
                visible_events = visible_events[-max_events:]
        for event in visible_events:
            source_name, source_job = self._actor_context.get(
                event.source_id, ("", "")
            )
            target_name, target_job = self._actor_context.get(
                event.target_id, ("", "")
            )
            duration_override_ms = (
                round(team_effect_duration_override(event.skill_name) * 1000)
                if event.permanent and event.operation != "remove"
                else 0
            )
            end_ms = (
                event.timestamp_ms + max(1, duration_override_ms)
                if duration_override_ms > 0
                else (
                    current_ms + 1
                    if event.permanent and event.operation != "remove"
                    else event.timestamp_ms + max(1, event.duration_ms)
                )
            )
            rows.append(
                StatusEventSnapshot(
                    timestamp_seconds=event.timestamp_ms / 1000.0,
                    target_id=event.target_id,
                    target_name=target_name or event.target_name or event.target_id,
                    target_job=target_job or event.target_job,
                    source_id=event.source_id,
                    source_name=source_name or event.source_name or event.source_id,
                    job=source_job or event.job,
                    skill_id=event.skill_id,
                    skill_name=event.skill_name,
                    key=event.effect_key,
                    label=event.effect_label,
                    value=event.value,
                    status_kind=event.status_kind,
                    effect_code=event.effect_code,
                    parameters=event.parameters,
                    duration_seconds=(
                        duration_override_ms / 1000.0
                        if duration_override_ms > 0
                        else max(0, event.duration_ms) / 1000.0
                    ),
                    end_seconds=end_ms / 1000.0,
                    active=event.operation != "remove" and end_ms > current_ms,
                    operation=event.operation,
                    value_source=event.value_source,
                    duration_source=event.duration_source,
                )
            )
        rows.sort(key=lambda row: row.timestamp_seconds)
        return tuple(rows)

    @staticmethod
    def _annotate_effective_status_events(
        events: tuple[StatusEventSnapshot, ...],
        effects: tuple[BossEffectSnapshot, ...],
    ) -> tuple[StatusEventSnapshot, ...]:
        """Attach unified-settlement state without discarding wire evidence."""

        def skill_key(value: str) -> str:
            key = "".join(str(value or "").upper().split())
            return key[:-2] if key.endswith("EX") else key

        rows: list[StatusEventSnapshot] = []
        for event in events:
            if event.operation == "remove":
                rows.append(
                    replace(
                        event,
                        effective_state="移除",
                        effective_reason="服务端状态移除记录",
                    )
                )
                continue
            candidates = [
                effect
                for effect in effects
                if effect.target_id == event.target_id
                and effect.key == event.key
                and skill_key(effect.skill_name) == skill_key(event.skill_name)
                and (not effect.source_id or effect.source_id == event.source_id)
            ]
            if not candidates:
                rows.append(event)
                continue
            effective_now = any(
                start - 1e-6 <= event.timestamp_seconds < end + 1e-6
                for effect in candidates
                for start, end in effect.effective_intervals
            )
            if effective_now:
                state = (
                    "部分生效"
                    if any(effect.effective_state == "部分生效" for effect in candidates)
                    else "有效"
                )
                reason = next(
                    (
                        effect.effective_reason
                        for effect in candidates
                        if effect.effective_reason
                    ),
                    "包内状态实际参与结算",
                )
            else:
                representative = candidates[0]
                state = representative.effective_state or "仅观察状态"
                reason = representative.effective_reason
            rows.append(
                replace(
                    event,
                    effective_state=state,
                    effective_reason=reason,
                )
            )
        return tuple(rows)

    @staticmethod
    def _target_identity_key(
        target_id: str,
        target_name: str,
        target_max_hp: int = 0,
    ) -> tuple[str, str]:
        return target_identity_key(target_id, target_name, target_max_hp)

    @staticmethod
    def _boss_target_group_key(
        row: DamageTargetSnapshot,
    ) -> tuple[str, str]:
        return CombatAggregator._target_identity_key(
            row.target_id, row.target_name, row.target_max_hp
        )

    @staticmethod
    def _primary_boss_target_ids(
        target_rows: list[DamageTargetSnapshot],
    ) -> frozenset[str]:
        return BossRecognizer().primary_target_ids(target_rows)

    def boss_effect_view(
        self,
        target_ids: frozenset[str],
        current_ms: int | None = None,
    ) -> BossEffectViewSnapshot:
        """Return exact status totals and curves for selected Boss actors."""
        current_ms = max(
            0,
            int(
                current_ms
                if current_ms is not None
                else (self._last_ms or 0)
            ),
        )
        if not target_ids:
            return BossEffectViewSnapshot()
        identities = frozenset(
            self._target_identity_key(
                target_id,
                self._actor_context.get(target_id, ("", ""))[0],
                self._target_max_hp.get(target_id, 0),
            )
            for target_id in target_ids
        )
        selected_tracks = [
            track
            for track in self._boss_effects.values()
            if track.target_id in target_ids
        ]
        phase_windows = self._boss_phase_windows(
            identities,
            current_ms,
            target_ids=target_ids,
            group_by_runtime_target=True,
        )
        effect_rows = self._effect_snapshots(
            self._boss_effects,
            current_ms,
            preserve_parameters=False,
            group_by_target=True,
            target_ids=target_ids,
            target_identity_keys=identities,
            coverage_windows_by_identity=phase_windows,
            group_by_runtime_target=True,
        )
        event_rows = self._status_event_snapshots(
            self._boss_status_events,
            current_ms,
            target_ids=target_ids,
            max_events=_MAX_BOSS_STATUS_DETAIL_EVENTS,
        )
        return BossEffectViewSnapshot(
            effects=effect_rows,
            summary=self._boss_effect_summary(selected_tracks, current_ms),
            series=self._boss_effect_series(selected_tracks, current_ms),
            events=self._annotate_effective_status_events(
                event_rows, effect_rows
            ),
        )

    def boss_effect_summary_at(
        self,
        target_ids: frozenset[str],
        current_ms: int,
    ) -> tuple[tuple[str, float], ...]:
        """Return only the stacked Boss effects active at one timestamp."""
        if not target_ids:
            return ()
        selected_tracks = [
            track
            for track in self._boss_effects.values()
            if track.target_id in target_ids
        ]
        return self._boss_effect_summary(selected_tracks, max(0, int(current_ms)))

    def _overlay_effect_rows(
        self,
        tracks: dict[tuple[object, ...], _BossEffectTrack],
        current_ms: int,
        *,
        include_expired_latest: bool,
        target_ids: frozenset[str] | None = None,
        preserve_parameters: bool = False,
    ) -> list[dict[str, object]]:
        """Project one latest interval per status track for the live overlay.

        Unlike ``_effect_snapshots`` this method never merges or measures the
        complete interval history.  The overlay needs the current application
        and the most recent team-Buff cast for cooldowns, both of which are
        bounded by the number of known status tracks rather than fight length.
        """

        groups: dict[tuple[object, ...], list[tuple[_BossEffectTrack, int, int]]] = (
            defaultdict(list)
        )
        for track in tracks.values():
            if target_ids is not None and track.target_id not in target_ids:
                continue
            latest: tuple[int, int] | None = None
            for start_ms, end_ms in reversed(track.intervals):
                if start_ms <= current_ms:
                    latest = (start_ms, end_ms)
                    break
            if latest is None:
                continue
            start_ms, end_ms = latest
            if not include_expired_latest and not (
                start_ms <= current_ms < end_ms
            ):
                continue
            normalized_skill = "".join(track.skill_name.upper().split())
            if normalized_skill.endswith("EX"):
                normalized_skill = normalized_skill[:-2]
            key: tuple[object, ...] = (
                track.target_id,
                normalized_skill or track.skill_id,
                track.effect_key,
                track.status_kind,
                _effect_value_signature(track.value, track.parameters),
            )
            if preserve_parameters:
                key += (track.effect_code, track.parameters)
                if track.skill_id in AURA_SKILL_KEYS:
                    key += (track.source_id,)
            groups[key].append((track, start_ms, end_ms))

        rows: list[dict[str, object]] = []
        for grouped in groups.values():
            representative, _rep_start, _rep_end = max(
                grouped,
                key=lambda row: (
                    row[0].last_refresh_ms,
                    row[0].value
                    if row[0].value is not None
                    else float("-inf"),
                ),
            )
            active = [
                row for row in grouped if row[1] <= current_ms < row[2]
            ]
            visible = active or grouped
            intervals = self._merge_intervals(
                [(start_ms, end_ms) for _track, start_ms, end_ms in visible],
                current_ms,
            )
            if not intervals:
                continue
            start_ms = min(start for start, _end in intervals)
            end_ms = max(end for _start, end in intervals)
            source_ids = {row[0].source_id for row in visible if row[0].source_id}
            source_names: set[str] = set()
            jobs: set[str] = set()
            values: list[float] = []
            for track, _start, _end in visible:
                context_name, context_job = self._actor_context.get(
                    track.source_id, ("", "")
                )
                if context_name or track.source_name:
                    source_names.add(context_name or track.source_name)
                if context_job or track.job:
                    jobs.add(context_job or track.job)
                if track.value is not None:
                    values.append(float(track.value))
            target_name, target_job = self._actor_context.get(
                representative.target_id, ("", "")
            )
            open_ended = any(
                end_ms >= _OPEN_ENDED_STATUS_MS
                for _track, _start_ms, end_ms in active
            )
            serialized_end_ms = current_ms + 1 if open_ended else end_ms
            rows.append(
                {
                    "key": representative.effect_key,
                    "label": representative.effect_label,
                    "source_id": (
                        next(iter(source_ids)) if len(source_ids) == 1 else ""
                    ),
                    "source_name": (
                        next(iter(source_names))
                        if len(source_names) == 1
                        else (next(iter(jobs)) if len(jobs) == 1 else "多个来源")
                    ),
                    "job": (
                        next(iter(jobs))
                        if len(jobs) == 1
                        else " / ".join(sorted(jobs))
                    ),
                    "skill_id": representative.skill_id,
                    "skill_name": representative.skill_name,
                    "stack_group": representative.stack_group,
                    "stackable": representative.stackable,
                    "value": max(values) if values else None,
                    "status_kind": representative.status_kind,
                    "effect_code": representative.effect_code,
                    "parameters": representative.parameters,
                    "data_source": (
                        "抓包数据"
                        if representative.value_source == "packet"
                        and representative.duration_source == "packet"
                        else "抓包优先 / 预设兜底"
                    ),
                    "value_source": representative.value_source,
                    "duration_source": representative.duration_source,
                    "start_seconds": start_ms / 1000.0,
                    "end_seconds": serialized_end_ms / 1000.0,
                    "last_refresh_seconds": max(
                        row[0].last_refresh_ms for row in visible
                    )
                    / 1000.0,
                    "refresh_count": sum(row[0].refresh_count for row in visible),
                    "active": bool(active),
                    "uptime_seconds": max(0, min(current_ms, end_ms) - start_ms)
                    / 1000.0,
                    "coverage": 0.0,
                    "target_id": representative.target_id,
                    "target_name": target_name or representative.target_id,
                    "target_job": target_job,
                    "intervals": [[start_ms / 1000.0, serialized_end_ms / 1000.0]],
                    "open_ended": open_ended,
                }
            )
        rows.sort(
            key=lambda row: (
                str(row.get("target_id", "")),
                str(row.get("status_kind", "")),
                str(row.get("skill_name", "")),
                str(row.get("key", "")),
            )
        )
        return rows

    def freeze_live_overlay(self, effect_current_ms: int) -> CombatAggregator:
        """Copy only live projection inputs while the caller holds its lock.

        No event ledger, skill reconciliation or historical interval arrays are
        copied. The detached reader must only call overlay_snapshot with
        refresh_composition=False, at this exact timestamp.
        """
        frozen = object.__new__(CombatAggregator)
        for name in (
            "_actor_context", "_party_members", "_overlay_target_damage",
            "_overlay_target_hits", "_target_max_hp", "_overlay_incoming_damage",
            "_overlay_latest_skill_casts", "_overlay_last_attack_targets",
            "_overlay_attack_players", "_overlay_attack_scores",
            "_overlay_target_names",
            "_damage_by_second",
        ):
            setattr(frozen, name, dict(getattr(self, name)))
        for name in (
            "_revision", "_overlay_player_buff_clear_revision",
            "_overlay_player_buff_state_revision",
        ):
            setattr(frozen, name, getattr(self, name))
        for name in ("_overlay_player_buff_skill_keys", "_overlay_team_buff_skill_keys"):
            setattr(frozen, name, frozenset(getattr(self, name)))
        frozen._players = {
            key: replace(value, skills={}, damage_by_second={})
            for key, value in self._players.items()
        }
        frozen._overlay_target_bounds = {
            key: list(value) for key, value in self._overlay_target_bounds.items()
        }
        first_second = max(0, effect_current_ms // 1000 - 180 + 1)
        frozen._overlay_player_damage_by_second = {
            key: {sec: value for sec, value in series.items() if sec >= first_second}
            for key, series in self._overlay_player_damage_by_second.items()
        }
        for name in ("_player_buffs", "_boss_effects"):
            tracks = {}
            for key, track in getattr(self, name).items():
                latest = next(
                    (list(interval) for interval in reversed(track.intervals)
                     if interval[0] <= effect_current_ms), None,
                )
                tracks[key] = replace(track, intervals=[latest] if latest else [])
            setattr(frozen, name, tracks)
        # These dictionaries contain scalar protocol facts, not event objects.
        # Copy their exact mutable containers instead of recursively walking
        # thousands of immutable strings/numbers on every refresh.
        for name in ("_overlay_player_health", "_overlay_cooldown_refunds"):
            setattr(frozen, name, {
                key: dict(value) for key, value in getattr(self, name).items()
            })
        frozen._overlay_player_buff_clear_events = [
            {**event, "closed_buffs": [dict(row) for row in event.get("closed_buffs", [])]}
            for event in self._overlay_player_buff_clear_events
        ]
        # These readers are bounded by live actors/instances, not HP history.
        summons = self._summon_presence.snapshot(effect_current_ms, frozen._actor_context)
        hp_rows = self._boss_hp_ledger.overlay_rows()
        ended = self._boss_hp_ledger.ended_target_ids()
        frozen._summon_presence = SimpleNamespace(snapshot=lambda *_args: summons)
        frozen._boss_hp_ledger = SimpleNamespace(
            overlay_rows=lambda: hp_rows, ended_target_ids=lambda: ended,
        )
        frozen._raw_damage_ledger = SimpleNamespace(total_damage=self._raw_damage_ledger.total_damage)
        return frozen

    def overlay_snapshot(
        self,
        effect_current_ms: int,
        *,
        recent_damage_seconds: int = 180,
        refresh_composition: bool = True,
    ) -> dict[str, object]:
        """Return a bounded, history-free snapshot for desktop overlays."""

        current_ms = max(0, int(effect_current_ms))
        current_second = current_ms // 1000
        first_second = max(0, current_second - max(30, recent_damage_seconds) + 1)
        active_summons = self._summon_presence.snapshot(
            current_ms,
            self._actor_context,
        )
        if refresh_composition:
            attribution, composition_skills = self._v2_live_composition()
            composition_revision = self._revision
        else:
            attribution, composition_skills, composition_revision = None, {}, -1

        target_ids = set(self._overlay_target_damage)
        target_ids.update(
            track.target_id
            for track in self._boss_effects.values()
            if track.target_id
        )
        target_rows: list[DamageTargetSnapshot] = []
        total_target_damage = sum(self._overlay_target_damage.values())
        for target_id in sorted(target_ids):
            target_name, target_job = self._actor_context.get(
                target_id, ("", "")
            )
            bounds = self._overlay_target_bounds.get(target_id, [0, 0])
            damage = int(self._overlay_target_damage.get(target_id, 0))
            target_rows.append(
                DamageTargetSnapshot(
                    target_id=target_id,
                    damage=damage,
                    hits=int(self._overlay_target_hits.get(target_id, 0)),
                    share=(damage / total_target_damage if total_target_damage else 0.0),
                    target_name=target_name,
                    target_job=target_job,
                    target_max_hp=int(self._target_max_hp.get(target_id, 0)),
                    first_ms=int(bounds[0]),
                    last_ms=int(bounds[1]),
                )
            )
        primary_target_ids = self._primary_boss_target_ids(target_rows)
        runtime_targets = [
            row for row in target_rows if row.target_id in primary_target_ids
        ]

        player_ids = set(self._players)
        # Buffs, HP rows and cached names describe an actor, not membership.
        # Keep zero-damage teammates via independently observed party/action
        # identities rather than expanding the roster with every Buff target.
        player_ids.update(
            actor_id for actor_id, member in self._party_members.items()
            if member.confidence >= _RELIABLE_PARTY_MEMBER_CONFIDENCE
        )
        players: list[dict[str, object]] = []
        for source_id in player_ids:
            totals = self._players.get(source_id)
            party = self._party_members.get(source_id)
            context_name, context_job = self._actor_context.get(
                source_id, ("", "")
            )
            name = context_name or (party.name if party else "") or (
                totals.name if totals else ""
            ) or source_id
            job = context_job or (party.job if party else "") or (
                totals.job if totals else ""
            )
            if not _is_recognized_player_job(job):
                continue
            damage = int(totals.damage if totals else 0)
            series = self._overlay_player_damage_by_second.get(source_id, {})
            skill_rows = [
                {
                    "skill_id": skill_id,
                    "name": skill.name or skill_id,
                    "damage": int(skill.damage),
                    "healing": int(skill.healing),
                    "hits": int(skill.hits),
                    "critical_hits": int(skill.critical_hits),
                    "critical_samples": int(skill.critical_samples),
                    "share": (
                        skill.damage / totals.damage
                        if totals and totals.damage
                        else 0.0
                    ),
                    "attribution": list(skill.attribution.most_common()),
                }
                for skill_id, skill in sorted(
                    composition_skills.get(source_id, {}).items(),
                    key=lambda item: (
                        item[1].damage,
                        item[1].healing,
                    ),
                    reverse=True,
                )
            ]
            players.append(
                {
                    "source_id": source_id,
                    "name": name,
                    "job": job,
                    "damage": damage,
                    "healing": int(totals.healing if totals else 0),
                    "dps": damage / max(0.001, current_ms / 1000.0),
                    "hits": int(totals.hits if totals else 0),
                    "critical_hits": int(totals.critical_hits if totals else 0),
                    "critical_samples": int(
                        totals.critical_samples if totals else 0
                    ),
                    "critical_rate": (
                        totals.critical_hits / totals.critical_samples
                        if totals and totals.critical_samples
                        else 0.0
                    ),
                    "active_seconds": max(
                        0.0,
                        (
                            (totals.last_ms or 0) - (totals.first_ms or 0)
                        )
                        / 1000.0,
                    )
                    if totals
                    else 0.0,
                    "skills": skill_rows,
                    "damage_series": [
                        [second, amount]
                        for second, amount in sorted(series.items())
                        if second >= first_second
                    ],
                    "incoming_damage": int(
                        self._overlay_incoming_damage.get(source_id, 0)
                    ),
                }
            )
        players.sort(
            key=lambda row: (
                int(row.get("damage", 0)),
                str(row.get("job", "")),
            ),
            reverse=True,
        )
        players = players[:_MAX_PARTY_PLAYERS]

        player_buffs = self._overlay_effect_rows(
            self._player_buffs,
            current_ms,
            include_expired_latest=True,
            preserve_parameters=True,
        )
        boss_effects = self._overlay_effect_rows(
            self._boss_effects,
            current_ms,
            # Keep one latest historical interval per status track. The
            # overlay's shared stacking engine needs the former holder of an
            # exclusive-admission lane to remember that an overlapping weak
            # application was rejected. This remains bounded by track count,
            # not combat duration; the overlay filters inactive contributors.
            include_expired_latest=True,
            target_ids=primary_target_ids or None,
        )
        # Health-shaped protocol rows are not sufficient proof of party
        # membership. Keep the overlay on actors independently confirmed by a
        # party/action event or by actual combat totals. This second gate keeps
        # future non-player resource layouts from surfacing as anonymous low-HP
        # alerts even if a decoder admits one before its exact shape is known.
        confirmed_health_ids = {str(row["source_id"]) for row in players}
        health_rows = [
            dict(row)
            for target_id, row in self._overlay_player_health.items()
            if target_id in confirmed_health_ids
        ]
        health_rows.sort(
            key=lambda row: (
                str(row.get("target_job", "")),
                str(row.get("target_id", "")),
            )
        )
        total_damage = sum(int(row.get("damage", 0)) for row in players)
        total_healing = sum(int(row.get("healing", 0)) for row in players)
        buff_casts: list[dict[str, object]] = []
        team_buff_casts: list[dict[str, object]] = []
        for (_source_id, skill_key), cast in self._overlay_latest_skill_casts.items():
            if skill_key not in self._overlay_player_buff_skill_keys:
                continue
            context_name, context_job = self._actor_context.get(
                cast.source_id, ("", "")
            )
            cast_row = {
                "source_id": cast.source_id,
                "source_name": context_name or cast.source_name,
                "source_job": context_job or cast.job,
                "skill_id": cast.skill_id,
                "skill_name": cast.skill_name,
                "cast_seconds": cast.timestamp_ms / 1000.0,
            }
            buff_casts.append(cast_row)
            if skill_key in self._overlay_team_buff_skill_keys:
                team_buff_casts.append(dict(cast_row))
        buff_casts.sort(
            key=lambda row: (
                str(row.get("skill_name", "")),
                str(row.get("source_id", "")),
            )
        )
        team_buff_casts.sort(
            key=lambda row: (
                str(row.get("skill_name", "")),
                str(row.get("source_id", "")),
            )
        )
        return {
            "elapsed_seconds": current_ms / 1000.0,
            "dps_elapsed_seconds": current_ms / 1000.0,
            "total_damage": total_damage,
            "total_healing": total_healing,
            "raw_damage_total": self._raw_damage_ledger.total_damage,
            "attributed_damage_total": attribution.attributed_damage if attribution else 0,
            "unresolved_damage_total": sum(
                event.amount
                for event in attribution.events
                if not event.healing and event.skill_id in {"", "-1"}
            ) if attribution else 0,
            "inferred_cast_count": attribution.inferred_casts if attribution else 0,
            "reassigned_hit_count": attribution.reassigned_hits if attribution else 0,
            "composition_revision": composition_revision,
            "composition_pending": composition_revision != self._revision,
            # Lightweight live projection is intentionally provisional.  The
            # final snapshot replaces these values with the independent HP-
            # backed healing ledger without scanning history every 0.2 s.
            "bossHealth": self._boss_hp_ledger.overlay_rows(),
            "endedBossIds": self._boss_hp_ledger.ended_target_ids(),
            "party_members": [
                {"actor_id": member.actor_id, "job": member.job,
                 "name": member.name, "confidence": member.confidence}
                for member in self._party_members.values()
            ],
            "lastAttackTargets": dict(self._overlay_last_attack_targets),
            # 带时效的「当前目标」视图：只保留最近 15 秒真正打过的目标。
            # 业务判定（Boss 行身份、分边分组、队友目标行）用这张表；
            # 永久表 ``lastAttackTargets`` 只留作名字反查与历史诊断。
            "recentAttackTargets": {
                source: dict(entry)
                for source, entry in self._overlay_last_attack_targets.items()
                if isinstance(entry, dict)
                and current_ms - float(entry.get("timestampMs") or 0)
                <= _ATTACK_TARGET_TTL_MS
            },
            # 悬浮窗 Boss 行的「本机目标」视图：30 秒时效。停手超过 30 秒就不再
            # 出现在这里，Boss 行据此留空，而不是跳到队友正在打的那只。
            "displayAttackTargets": {
                source: dict(entry)
                for source, entry in self._overlay_last_attack_targets.items()
                if isinstance(entry, dict)
                and current_ms - float(entry.get("timestampMs") or 0)
                <= _DISPLAY_TARGET_TTL_MS
            },
            "targetNames": dict(self._overlay_target_names),
            "attackTargetsByTemplate": {
                template: sorted(players)
                for template, players in self._overlay_attack_players.items()
                if players
            },
            # 短窗口版（4 秒）：分边判断要的是「此刻谁还在打」，不是「最近 10 秒
            # 谁碰过」。梦魇海龙里 AOE 每 10 秒蹭到对侧一次，用 10 秒表会把
            # 「我自己两边都打」误判成成立，从而完全放弃分边过滤。
            "recentAttackers": {
                template: sorted(
                    player
                    for player, stamp in players.items()
                    if float(stamp) >= current_ms - _SPLIT_ATTACK_WINDOW_MS
                )
                for template, players in self._overlay_attack_players.items()
                if players
            },
            # 「本机 15 秒内主要在打谁」的加权表：玩家 -> 目标 -> 窗口内伤害。
            # 播报白名单（focus）用它选主目标，避免 AOE 溅射一命中就换侧。
            "attackScores": self.attack_scores(current_ms),
            "raw_healing_total": total_healing,
            "factual_healing_total": total_healing,
            "attributed_healing_total": total_healing,
            "unattributed_healing_total": 0,
            "suppressed_overheal_total": 0,
            "healing_matched_event_count": 0,
            "healing_unattributed_event_count": 0,
            "healing_ambiguous_event_count": 0,
            "healing_attribution_methods": [],
            "damage_series": [
                [second, amount]
                for second, amount in sorted(self._damage_by_second.items())
            ],
            "boss_effect_current_seconds": current_ms / 1000.0,
            "players": players,
            "damage_targets": [
                {
                    field: getattr(row, field)
                    for field in row.__dataclass_fields__
                }
                for row in target_rows
            ],
            "boss_runtime_targets": [
                {
                    field: getattr(row, field)
                    for field in row.__dataclass_fields__
                }
                for row in runtime_targets
            ],
            "boss_effects": boss_effects,
            "boss_runtime_effects": boss_effects,
            "player_buffs": player_buffs,
            "buff_casts": buff_casts,
            "team_buff_casts": team_buff_casts,
            "cooldown_refunds": [
                {
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in {"cycle_cast_ms", "last_event_ms"}
                    },
                    "cycle_cast_seconds": int(row.get("cycle_cast_ms") or 0)
                    / 1000.0,
                    "last_event_seconds": int(row.get("last_event_ms") or 0)
                    / 1000.0,
                }
                for row in self._overlay_cooldown_refunds.values()
            ],
            "summons": active_summons,
            "summon_options": summon_overlay_options(),
            "player_buff_clear_revision": self._overlay_player_buff_clear_revision,
            "player_buff_state_revision": self._overlay_player_buff_state_revision,
            "player_buff_clear_events": [
                {
                    **{
                        key: value
                        for key, value in event.items()
                        if key != "closed_buffs"
                    },
                    "timestamp_seconds": int(event.get("timestamp_ms", 0))
                    / 1000.0,
                    "closed_buffs": [
                        {
                            **dict(row),
                            "target_name": self._actor_context.get(
                                str(row.get("target_id", "")), ("", "")
                            )[0]
                            or str(row.get("target_id", "")),
                            "target_job": self._actor_context.get(
                                str(row.get("target_id", "")), ("", "")
                            )[1],
                        }
                        for row in event.get("closed_buffs", [])
                        if isinstance(row, dict)
                    ],
                }
                for event in self._overlay_player_buff_clear_events
            ],
            "player_health": health_rows,
            "revision": self._revision,
        }

    def snapshot(
        self,
        effect_current_ms: int | None = None,
        *,
        include_events: bool = True,
        live_clock: bool = False,
    ) -> CombatSnapshot:
        attribution, composition_skills = self._v2_live_composition()
        cast_counts_by_id: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        cast_counts_by_name: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        for cast in attribution.cast_events:
            if not cast.source_id:
                continue
            evidence = (
                "inferred"
                if _is_inferred_cast_evidence(cast.packet_kind)
                else "direct"
            )
            skill_id = str(cast.skill_id or "").strip()
            name_key = _composition_cast_name_key(cast.skill_name)
            if skill_id:
                cast_counts_by_id[(cast.source_id, skill_id)][evidence] += 1
            if name_key:
                cast_counts_by_name[(cast.source_id, name_key)][evidence] += 1

        def composition_cast_counts(
            source_id: str,
            skill_id: str,
            skill_name: str,
        ) -> tuple[int, int, int]:
            # Exact IDs win.  Name matching is the compatibility path for the
            # common protocol shape where the action packet names/IDs the EX
            # version while a damage record retains the base skill identity.
            counts = cast_counts_by_id.get((source_id, str(skill_id or "")))
            if not counts:
                counts = cast_counts_by_name.get(
                    (source_id, _composition_cast_name_key(skill_name))
                )
            direct = int((counts or {}).get("direct", 0))
            inferred = int((counts or {}).get("inferred", 0))
            return direct + inferred, direct, inferred
        # One reconciled damage ledger now feeds the visible skill composition,
        # event timeline and scoring. Previously the page used
        # ``_display_combat_events`` while timeline/scoring used
        # ``attribution.events``; a deterministic Projectile/object hit could
        # therefore appear under different skills in different pages.
        # Healing remains independent and is replaced by the dedicated
        # HealingAttributionLedger later in this method.
        # A live capture continues to exist after the last decoded attack.  In
        # that case the display clock must keep moving even when the player is
        # dead, idle, or waiting for the next phase.  Replay snapshots keep the
        # old event-time semantics so selecting a historical time cannot alter
        # the recorded damage duration.
        combat_clock = resolve_combat_clock(
            first_ms=self._state_first_ms,
            last_ms=self._state_last_ms,
            requested_current_ms=effect_current_ms,
            live_clock=live_clock,
        )
        first = self._first_ms or combat_clock.origin_ms
        last = self._last_ms or first
        current_ms = combat_clock.current_ms
        elapsed_last = max(last, current_ms) if live_clock else last
        elapsed = max(0.001, (elapsed_last - first) / 1000.0)
        player_rows: list[PlayerSnapshot] = []
        player_source_ids = set(self._players)
        healing_cast_bounds: dict[str, list[int]] = {}
        for cast in self._skill_cast_events:
            if (
                not cast.source_id
                or self._healing_attribution_ledger.model_for(cast.skill_name)
                is None
            ):
                continue
            player_source_ids.add(cast.source_id)
            bounds = healing_cast_bounds.setdefault(
                cast.source_id,
                [cast.timestamp_ms, cast.timestamp_ms],
            )
            bounds[0] = min(bounds[0], cast.timestamp_ms)
            bounds[1] = max(bounds[1], cast.timestamp_ms)
        for source_id in sorted(player_source_ids):
            context_name, context_job = self._actor_context.get(
                source_id, ("", "")
            )
            fallback_bounds = healing_cast_bounds.get(source_id, [0, 0])
            player = self._players.get(source_id)
            if player is None:
                player = _PlayerTotals(
                    name=context_name or source_id,
                    job=context_job,
                    first_ms=fallback_bounds[0],
                    last_ms=fallback_bounds[1],
                )
            active = max(0.001, ((player.last_ms or 0) - (player.first_ms or 0)) / 1000.0)
            skill_totals = composition_skills.get(source_id, player.skills)
            skill_rows_list: list[SkillSnapshot] = []
            for skill_id, skill in sorted(
                skill_totals.items(),
                key=lambda item: (item[1].damage, item[1].healing),
                reverse=True,
            ):
                cast_count, direct_cast_count, inferred_cast_count = (
                    composition_cast_counts(source_id, skill_id, skill.name)
                )
                skill_rows_list.append(SkillSnapshot(
                    skill_id=skill_id,
                    name=skill.name,
                    damage=skill.damage,
                    healing=skill.healing,
                    hits=skill.hits,
                    critical_hits=skill.critical_hits,
                    share=(skill.damage / player.damage) if player.damage else 0.0,
                    attribution=tuple(skill.attribution.most_common()),
                    critical_samples=skill.critical_samples,
                    trigger_skills=tuple(
                        (
                            trigger_id,
                            trigger_name,
                            skill.trigger_hits[
                                (trigger_id, trigger_name, method, grade)
                            ],
                            damage,
                            method,
                            grade,
                        )
                        for (
                            trigger_id,
                            trigger_name,
                            method,
                            grade,
                        ), damage in sorted(
                            skill.trigger_damage.items(),
                            key=lambda item: (
                                item[1],
                                skill.trigger_hits[item[0]],
                                item[0][1],
                            ),
                            reverse=True,
                        )
                    ),
                    cast_count=cast_count,
                    direct_cast_count=direct_cast_count,
                    inferred_cast_count=inferred_cast_count,
                    average_hits=(
                        skill.hits / cast_count if cast_count > 0 else None
                    ),
                ))
            skill_rows = tuple(skill_rows_list)
            player_rows.append(
                PlayerSnapshot(
                    source_id=source_id,
                    name=context_name or player.name or source_id,
                    job=context_job or player.job,
                    damage=player.damage,
                    healing=player.healing,
                    dps=player.damage / elapsed,
                    hits=player.hits,
                    critical_hits=player.critical_hits,
                    critical_rate=(
                        player.critical_hits / player.critical_samples
                        if player.critical_samples
                        else 0.0
                    ),
                    active_seconds=active,
                    skills=skill_rows,
                    damage_series=tuple(sorted(player.damage_by_second.items())),
                    critical_samples=player.critical_samples,
                )
            )
        player_rows = [
            row
            for row in player_rows
            if _is_recognized_player_job(row.job)
        ]
        player_rows.sort(
            key=lambda row: (row.damage, row.healing, row.hits), reverse=True
        )
        player_rows = player_rows[:_MAX_PARTY_PLAYERS]
        visible_source_ids = {row.source_id for row in player_rows}
        healing_attribution = self._reconciled_healing(visible_source_ids)
        effective_healing_events = healing_attribution.events
        raw_healing_counts: Counter[tuple[str, str]] = Counter()
        raw_healing_attribution: dict[
            tuple[str, str], Counter[str]
        ] = defaultdict(Counter)
        for event in self._raw_healing_events:
            if event.healing and event.source_id in visible_source_ids:
                key = (
                    event.source_id,
                    event.skill_id or event.skill_name or "unknown",
                )
                raw_healing_counts[key] += 1
                raw_healing_attribution[key][
                    event.attribution_method or "recorded"
                ] += 1
        effective_healing_amounts: Counter[tuple[str, str]] = Counter()
        effective_healing_counts: Counter[tuple[str, str]] = Counter()
        effective_healing_attribution: dict[
            tuple[str, str], Counter[str]
        ] = defaultdict(Counter)
        for event in effective_healing_events:
            key = (
                event.source_id,
                event.skill_id or event.skill_name or "unknown",
            )
            effective_healing_amounts[key] += event.amount
            effective_healing_counts[key] += 1
            effective_healing_attribution[key][
                event.attribution_method or "healing_reconciled"
            ] += 1
        normalized_player_rows: list[PlayerSnapshot] = []
        for row in player_rows:
            normalized_skills: list[SkillSnapshot] = []
            seen_skill_ids: set[str] = set()
            for skill in row.skills:
                key = (row.source_id, skill.skill_id)
                seen_skill_ids.add(skill.skill_id)
                raw_heal_hits = raw_healing_counts.get(key, 0)
                effective_heal_hits = effective_healing_counts.get(key, 0)
                healing = effective_healing_amounts.get(key, 0)
                hits = max(0, skill.hits - raw_heal_hits) + effective_heal_hits
                if skill.damage <= 0 and healing <= 0 and hits <= 0:
                    continue
                attribution_rows = Counter(dict(skill.attribution))
                for method, count in raw_healing_attribution.get(
                    key, Counter()
                ).items():
                    attribution_rows[method] -= count
                    if attribution_rows[method] <= 0:
                        del attribution_rows[method]
                for method, count in effective_healing_attribution.get(
                    key, Counter()
                ).items():
                    attribution_rows[method] += count
                normalized_skills.append(
                    replace(
                        skill,
                        healing=healing,
                        hits=hits,
                        average_hits=(
                            hits / skill.cast_count
                            if skill.cast_count > 0
                            else None
                        ),
                        attribution=tuple(attribution_rows.most_common()),
                    )
                )
            # Defensive path for a healing event whose canonical skill key was
            # not present in the display composition.
            for event in effective_healing_events:
                skill_id = event.skill_id or event.skill_name or "unknown"
                if event.source_id != row.source_id or skill_id in seen_skill_ids:
                    continue
                key = (row.source_id, skill_id)
                (
                    cast_count,
                    direct_cast_count,
                    inferred_cast_count,
                ) = composition_cast_counts(
                    row.source_id,
                    skill_id,
                    event.skill_name or skill_id,
                )
                normalized_skills.append(
                    SkillSnapshot(
                        skill_id=skill_id,
                        name=event.skill_name or skill_id,
                        damage=0,
                        healing=effective_healing_amounts[key],
                        hits=effective_healing_counts[key],
                        critical_hits=0,
                        share=0.0,
                        attribution=tuple(
                            effective_healing_attribution[key].most_common()
                        ),
                        cast_count=cast_count,
                        direct_cast_count=direct_cast_count,
                        inferred_cast_count=inferred_cast_count,
                        average_hits=(
                            effective_healing_counts[key] / cast_count
                            if cast_count > 0
                            else None
                        ),
                    )
                )
                seen_skill_ids.add(skill_id)
            normalized_skills.sort(
                key=lambda skill: (skill.damage, skill.healing),
                reverse=True,
            )
            normalized_player_rows.append(
                replace(
                    row,
                    healing=sum(
                        event.amount
                        for event in effective_healing_events
                        if event.source_id == row.source_id
                    ),
                    skills=tuple(normalized_skills),
                )
            )
        player_rows = normalized_player_rows
        visible_players_by_id = {row.source_id: row for row in player_rows}
        effective_incoming_damage_events = (
            self._effective_incoming_damage_events(visible_source_ids)
        )
        incoming_damage_by_target: Counter[str] = Counter()
        for event in effective_incoming_damage_events:
            if (
                event.target_id in visible_players_by_id
                and not (
                    str(event.source_id)
                    and str(event.source_id) == str(event.target_id)
                )
            ):
                incoming_damage_by_target[event.target_id] += max(0, event.amount)
        player_health_tracks = self._player_health_tracks_snapshot(
            visible_source_ids
        )
        baseline_hp_by_target = {
            track.target_id: track.max_hp
            for track in player_health_tracks
            if track.max_hp > 0
            and track.max_hp_source
            in {"protocol_210c", "unbuffed_086f_peak"}
        }
        effective_incoming_damage_events = tuple(
            replace(
                event,
                target_max_hp=baseline_hp_by_target.get(event.target_id, 0),
            )
            if event.target_max_hp <= 0
            and baseline_hp_by_target.get(event.target_id, 0) > 0
            else event
            for event in effective_incoming_damage_events
        )
        player_rows = [
            replace(
                row,
                incoming_damage=incoming_damage_by_target[row.source_id],
            )
            for row in player_rows
        ]
        visible_players_by_id = {row.source_id: row for row in player_rows}
        def visible_bound(source_id: str, index: int) -> int | None:
            player = self._players.get(source_id)
            if player is not None:
                value = player.first_ms if index == 0 else player.last_ms
                if value is not None:
                    return value
            cast_bounds = healing_cast_bounds.get(source_id)
            return cast_bounds[index] if cast_bounds else None

        visible_first = min(
            (
                value
                for source_id in visible_source_ids
                if (value := visible_bound(source_id, 0)) is not None
            ),
            default=None,
        )
        visible_last = max(
            (
                value
                for source_id in visible_source_ids
                if (value := visible_bound(source_id, 1)) is not None
            ),
            default=visible_first,
        )
        if live_clock and visible_first is not None:
            visible_last = max(visible_last or visible_first, current_ms)
        visible_elapsed = (
            max(0.001, ((visible_last or 0) - (visible_first or 0)) / 1000.0)
            if visible_first is not None
            else 0.0
        )
        visible_damage_by_second: dict[int, int] = defaultdict(int)
        visible_player_damage_by_second: dict[str, dict[int, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        dps_active_ms = 0
        previous_damage_ms: int | None = None
        target_damage: Counter[str] = Counter()
        target_hits: Counter[str] = Counter()
        target_bounds: dict[str, list[int]] = {}
        for event in self._combat_events:
            if event.source_id not in visible_source_ids or event.healing:
                continue
            if not is_mechanic_damage_skill(event.skill_id):
                if previous_damage_ms is not None:
                    dps_active_ms += min(
                        max(0, event.timestamp_ms - previous_damage_ms),
                        _DPS_IDLE_GAP_MS,
                    )
                previous_damage_ms = max(
                    event.timestamp_ms,
                    previous_damage_ms
                    if previous_damage_ms is not None
                    else event.timestamp_ms,
                )
            # Full-session views share the decoder's capture clock.  Do not
            # rebase the damage curve to the first visible hit: skill casts,
            # boss statuses and player buffs all retain capture-relative
            # timestamps, and using a second origin here shifts the curve away
            # from those timelines.
            second = max(0, event.timestamp_ms // 1000)
            visible_damage_by_second[second] += event.amount
            visible_player_damage_by_second[event.source_id][second] += event.amount
            if (
                event.target_id
                and event.target_id != "0x00000000"
                and event.target_id not in visible_source_ids
            ):
                target_damage[event.target_id] += event.amount
                target_hits[event.target_id] += 1
                bounds = target_bounds.setdefault(
                    event.target_id,
                    [event.timestamp_ms, event.timestamp_ms],
                )
                bounds[0] = min(bounds[0], event.timestamp_ms)
                bounds[1] = max(bounds[1], event.timestamp_ms)
        if live_clock and previous_damage_ms is not None:
            # Match the existing active-DPS rule for an idle gap: a live
            # session contributes at most five seconds after its last damage
            # event.  This makes DPS visibly respond to a death/idle period
            # without turning a long menu or phase transition into artificial
            # active combat time.
            dps_active_ms += min(
                max(0, current_ms - previous_damage_ms),
                _DPS_IDLE_GAP_MS,
            )
        dps_elapsed = max(0.001, dps_active_ms / 1000.0)
        player_rows = [
            replace(
                row,
                dps=row.damage / dps_elapsed,
                damage_series=tuple(
                    sorted(visible_player_damage_by_second[row.source_id].items())
                ),
            )
            for row in player_rows
        ]
        normalized_combat_events: tuple[CombatEvent, ...] = ()
        normalized_skill_cast_events: tuple[SkillCastEvent, ...] = ()
        if include_events:
            (
                normalized_combat_events,
                normalized_skill_cast_events,
            ) = self._normalized_visible_events(
                visible_players_by_id,
                tuple(
                    sorted(
                        (
                            *(
                                event
                                for event in attribution.events
                                if not event.healing
                            ),
                            *effective_healing_events,
                        ),
                        key=lambda event: (
                            event.timestamp_ms,
                            event.source_id,
                            event.target_id,
                        ),
                    )
                ),
                attribution.cast_events,
            )
        visible_total_damage = sum(row.damage for row in player_rows)
        visible_total_hits = sum(row.hits for row in player_rows)
        minimum_target_hits = max(20, round(visible_total_hits * 0.01))
        target_rows: list[DamageTargetSnapshot] = []
        other_damage = 0
        other_hits = 0
        for target_id, damage in target_damage.most_common():
            hits = target_hits[target_id]
            share = (
                damage / visible_total_damage if visible_total_damage else 0.0
            )
            if share >= 0.01 or hits >= minimum_target_hits:
                target_rows.append(
                    DamageTargetSnapshot(
                        target_id,
                        damage,
                        hits,
                        share,
                        self._actor_context.get(target_id, ("", ""))[0],
                        self._actor_context.get(target_id, ("", ""))[1],
                        self._target_max_hp.get(target_id, 0),
                        target_bounds.get(target_id, [0, 0])[0],
                        target_bounds.get(target_id, [0, 0])[1],
                    )
                )
            else:
                other_damage += damage
                other_hits += hits
        if other_damage:
            target_rows.append(
                DamageTargetSnapshot(
                    "__other_targets__",
                    other_damage,
                    other_hits,
                    other_damage / visible_total_damage,
                )
            )
        unresolved_damage = max(
            0, visible_total_damage - sum(target_damage.values())
        )
        if unresolved_damage:
            target_rows.append(
                DamageTargetSnapshot(
                    "__unresolved_target__",
                    unresolved_damage,
                    0,
                    unresolved_damage / visible_total_damage,
                )
            )
        damage_targets = tuple(target_rows)
        boss_selection_target_ids = set(target_damage)
        boss_selection_target_ids.update(
            event.target_id
            for event in self._boss_status_events
            if event.target_id
            and event.target_scope != "player"
            and (
                event.target_job.strip().partition(" ")[0].casefold()
                == "boss"
                or self._actor_context.get(event.target_id, ("", ""))[1]
                .strip()
                .partition(" ")[0]
                .casefold()
                == "boss"
            )
        )
        boss_selection_rows = [
            DamageTargetSnapshot(
                target_id,
                target_damage[target_id],
                target_hits[target_id],
                (
                    target_damage[target_id] / visible_total_damage
                    if visible_total_damage
                    else 0.0
                ),
                self._actor_context.get(target_id, ("", ""))[0],
                self._actor_context.get(target_id, ("", ""))[1],
                self._target_max_hp.get(target_id, 0),
                target_bounds.get(target_id, [0, 0])[0],
                target_bounds.get(target_id, [0, 0])[1],
            )
            for target_id in boss_selection_target_ids
        ]
        primary_boss_target_ids = self._primary_boss_target_ids(
            boss_selection_rows
        )
        primary_boss_identity_keys = frozenset(
            self._boss_target_group_key(row)
            for row in boss_selection_rows
            if row.target_id in primary_boss_target_ids
        )
        boss_phase_windows = self._boss_phase_windows(
            primary_boss_identity_keys,
            current_ms,
            target_ids=primary_boss_target_ids or None,
            group_by_runtime_target=True,
        )
        settlement_boss_target_id = max(
            primary_boss_target_ids,
            key=lambda target_id: target_bounds.get(target_id, [0, 0])[1],
            default="",
        )
        settlement_boss_damage_ms = (
            target_bounds.get(settlement_boss_target_id, [0, 0])[1]
            if settlement_boss_target_id
            else None
        )
        if include_events:
            inferred_deaths = self._inferred_player_death_events(
                visible_source_ids,
                boss_phase_windows,
                current_ms,
                effective_incoming_damage_events,
            )
            if inferred_deaths:
                normalized_combat_events = tuple(
                    sorted(
                        (*normalized_combat_events, *inferred_deaths),
                        key=lambda event: (
                            event.timestamp_ms,
                            event.source_id,
                            event.target_id,
                            event.skill_id,
                        ),
                    )
                )
        effect_rows = self._effect_snapshots(
            self._boss_effects,
            current_ms,
            preserve_parameters=False,
            group_by_target=True,
            target_ids=primary_boss_target_ids or None,
            target_identity_keys=(primary_boss_identity_keys or None),
            coverage_windows_by_identity=boss_phase_windows,
            # Encounter names may group replacement phases or simultaneous
            # identical monsters. Status ownership and stacking never do:
            # the table, curve and detail ledger all refer to runtime actors.
            group_by_runtime_target=True,
        )
        runtime_effect_rows = effect_rows if live_clock else ()
        player_buff_rows = self._effect_snapshots(
            self._player_buffs,
            current_ms,
            preserve_parameters=True,
            group_by_target=True,
            # Real-time gains show the complete receive lifetime. The score
            # intersects this same ledger with Boss output windows separately.
        )
        incoming_damage_events = tuple(
            replace(
                event,
                target_name=visible_players_by_id[event.target_id].name,
                target_job=visible_players_by_id[event.target_id].job,
            )
            for event in effective_incoming_damage_events
            if event.target_id in visible_players_by_id
        )
        boss_effect_event_rows = self._status_event_snapshots(
            self._boss_status_events,
            current_ms,
            target_ids=primary_boss_target_ids or None,
            max_events=_MAX_BOSS_STATUS_DETAIL_EVENTS,
        )
        boss_effect_event_rows = self._annotate_effective_status_events(
            boss_effect_event_rows, effect_rows
        )
        return CombatSnapshot(
            elapsed_seconds=visible_elapsed,
            instance_completion=self._settlement_ledger.snapshot(
                set(self._party_members) | visible_source_ids,
                encounter_boss_damage_ms=settlement_boss_damage_ms,
                encounter_boss_target_id=settlement_boss_target_id,
                current_timestamp_ms=current_ms,
            ),
            total_damage=visible_total_damage,
            total_healing=sum(row.healing for row in player_rows),
            players=tuple(player_rows),
            damage_series=tuple(sorted(visible_damage_by_second.items())),
            damage_targets=damage_targets,
            boss_effects=effect_rows,
            boss_effect_summary=self._boss_effect_summary(
                list(self._boss_effects.values()), current_ms
            ),
            boss_effect_series=self._boss_effect_series(
                list(self._boss_effects.values()), current_ms
            ),
            boss_effect_current_seconds=current_ms / 1000.0,
            boss_effect_events=boss_effect_event_rows,
            player_buffs=player_buff_rows,
            player_buff_events=self._status_event_snapshots(
                self._player_buff_events, current_ms
            ),
            player_buff_series=self._player_buff_series_snapshots(current_ms),
            combat_events=(
                normalized_combat_events
                if include_events
                else ()
            ),
            incoming_damage_events=(
                incoming_damage_events if include_events else ()
            ),
            skill_cast_events=(
                normalized_skill_cast_events
                if include_events
                else ()
            ),
            skill_parameter_events=(
                tuple(self._skill_parameter_events) if include_events else ()
            ),
            boss_action_events=(
                tuple(self._boss_action_events) if include_events else ()
            ),
            player_buff_clear_events=tuple(
                getattr(self, "_player_buff_clear_events", ())
            ),
            summon_removal_events=(
                tuple(getattr(self, "_summon_removal_events", ()))
                if include_events
                else ()
            ),
            dps_elapsed_seconds=dps_elapsed,
            raw_damage_total=attribution.raw_damage,
            attributed_damage_total=attribution.attributed_damage,
            unresolved_damage_total=sum(
                event.amount
                for event in attribution.events
                if not event.healing and event.skill_id in {"", "-1"}
            ),
            inferred_cast_count=attribution.inferred_casts,
            reassigned_hit_count=attribution.reassigned_hits,
            raw_healing_total=healing_attribution.raw_packet_total,
            factual_healing_total=healing_attribution.factual_healing_total,
            attributed_healing_total=(
                healing_attribution.attributed_healing_total
            ),
            unattributed_healing_total=(
                healing_attribution.unattributed_healing_total
            ),
            suppressed_overheal_total=(
                healing_attribution.suppressed_overheal_total
            ),
            healing_matched_event_count=healing_attribution.matched_events,
            healing_unattributed_event_count=(
                healing_attribution.unattributed_events
            ),
            healing_ambiguous_event_count=(
                healing_attribution.ambiguous_events
            ),
            healing_attribution_methods=healing_attribution.method_counts,
            boss_hp_tracks=self._boss_hp_ledger.snapshots(),
            party_members=tuple(
                sorted(
                    self._party_members.values(),
                    key=lambda row: (
                        row.party_position if row.party_position > 0 else 99,
                        row.name or row.actor_id,
                    ),
                )
            ),
            player_profiles=tuple(
                sorted(
                    self._player_profiles.values(),
                    key=lambda row: (
                        row.target_name or row.target_id,
                        row.timestamp_ms,
                    ),
                )
            ),
            player_health_tracks=player_health_tracks,
            player_lifecycle_events=(
                tuple(
                    event
                    for event in self._player_lifecycle_events
                    if event.target_id in visible_source_ids
                )
                if include_events
                else ()
            ),
            boss_runtime_targets=(
                tuple(
                    row
                    for row in boss_selection_rows
                    if row.target_id in primary_boss_target_ids
                )
                if live_clock
                else ()
            ),
            boss_runtime_effects=runtime_effect_rows,
        )


# One confirmed Punisher action is one four-hit round.
from engine.live.crippling_punisher_actions import install_aggregator_hook
install_aggregator_hook(CombatAggregator)
