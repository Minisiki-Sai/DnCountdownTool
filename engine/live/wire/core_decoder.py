from __future__ import annotations

from dataclasses import replace
from ..models import MonsterAppearanceEvent
from math import isfinite
from types import SimpleNamespace

from ..dungeon_map_catalog import is_battle_map, resolve_dungeon_map_id
from ..nest_probe import classic_nest_hint, get_probe
from ..boss_hp import load_fixed_boss_hp_profiles, normalize_boss_hp_name
from ..damage_object_origins import (
    DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS,
    DAMAGE_OBJECT_SKILL_IDS,
    DamageObjectOriginLedger,
)
from ..damage_attribution import (
    AttributionLayer,
    attribution_layer,
    should_prefer_damage_object_skill,
    should_replace_attribution,
)
from ..entity_owner_evidence import (
    EntityOwnerEvidenceLedger,
    EntityOwnerRecord,
    decode_entity_owner_records,
)
from ..compact_entity_object_evidence import CompactEntityObjectLedger
from ..pve_scene_evidence import observe_scene_boss
from ..damage_wire_evidence import damage_wire_metadata, has_inferred_status_object_hit
from ..native_damage_skill_keys import resolve_native_damage_skill_key
from ..summon_attack_runtime import (
    prewarm_permanent_summon_lives,
    recover_missing_summon_life,
)
from ..player_root_action_objects import (
    PlayerRootActionObjectLedger,
    packet_may_contain_player_root,
)
from ..character_identity_sessions import observe_local_character_identity
from ..late_field_hit_evidence import annotate_late_field_hits
from ..udp_damage_identity import canonicalize_udp_damage_identity
from ..damage_object_shaman_illusion import install_decoder_adapter
from ..field_effect_instances import FieldEffectInstanceLedger
from ..instant_action_damage import InstantActionDamageLedger
from ..compound_skill_responses import attribute_compound_skill_responses
from ..prebattle_state import inherited_player_buff_events
from ..aura_state import AuraInstanceLedger, AURA_SKILL_IDS, is_aura_application, decode_aura_instance_ends
from ..derived_combo_actions import load_derived_combo_action_rules
from hashlib import sha256
from ..nest_recognizer import canonical_nest_name, resolve_nest_template
from ..protocol import (
    decode_v576_damage_object_registration,
    decode_v576_damage_object_registrations,
    decode_v576_server_damage_object_registrations,
    decode_v576_gateway_route_catalog,
    decode_v576_lobby_return,
    decode_v576_map_identity,
    decode_v576_scene_teardown,
    decode_v576_town_scene_load,
    V576_PACKET_STATUS_END,
)
from ..scene_transition import (
    V576SceneReturnTracker,
    V576TcpWorldReturnTracker,
    VerifiedV576HubReturn,
    inferred_hub_history_row,
    promote_hub_row_to_time_space_garden,
)
from ..settlement import (
    decode_clear_trigger,
    decode_clear_reward_trigger,
    decode_settlement,
    decode_terminal_boss_end,
    may_contain_settlement,
)
from ..boss_completion_trigger import match_boss_completion_trigger


# Only summon families independently verified to survive a scene transition
# may retain their exact pre-transition generation for the automatic-capture
# warm-up snapshot.  This is deliberately not the ordinary summon catalog:
# short runtime IDs are reused aggressively, so broad cross-scene inheritance
# would turn stale ownership into false damage attribution.
_CROSS_SCENE_PREBATTLE_SUMMON_SKILLS = {
    33601: 6318,  # 阿兹纳之约
    33603: 6316,  # 阿兹纳之息：常驻召唤物，周期刷新有限 Buff
}
_CROSS_SCENE_PREBATTLE_MAX_GAP_SECONDS = 600.0


def bind_host_globals(namespace: dict[str, object]) -> None:
    globals().update(namespace)


class V576DecoderCoreMixin:
    def _preobserve_damage_object_actions(self, packet) -> None:
        """Expose every typed player action before bundled object records.

        A v576 datagram can contain more than the one action selected by the
        legacy primary decoder.  It can also contain an alternate 09/xx
        object registration in the same message.  Processing registrations
        first used to reject shared child templates (for example Projectile
        3021) before their exact 2205/2207 action was visible.  This pass is
        evidence-only: normal cast emission/job state remains in the existing
        path, and ``observe_action`` already de-duplicates the later replay.
        """
        if str(packet.transport).upper() == "TCP":
            return
        known_action_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
        )
        for action in decode_v576_structured_actions(
            packet.payload,
            packet.inferred_direction(),
            self.action_skill_ids,
            known_action_actors,
            allow_unknown_actor_ids=True,
        ):
            actor_id = int(action.source_actor_id or 0)
            skill_id = _canonical_live_skill_id(action.skill_id)
            if (
                actor_id <= _SUMMON_ACTOR_ID_MAX
                or actor_id in self.monster_entities
                or actor_id in self.summon_owners
                or skill_id not in self.action_skill_ids
                or self._action_conflicts_with_confirmed_job(actor_id, skill_id)
            ):
                continue
            self.damage_object_origins.observe_action(
                actor_id,
                skill_id,
                packet.timestamp_seconds,
            )

    def _finalize_player_root_action_objects(self, events, packet):
        originals = list(events)
        # Active status inference formerly marked these physical results as
        # non-Hits, preventing even exact current object generations from
        # being checked. The temporary flag survives only a proven object
        # route; without that certificate the original row is returned.
        candidates = [
            replace(event, has_real_hit_evidence=True)
            if (isinstance(event, CombatEvent)
                and str(packet.transport).upper() == "UDP"
                and has_inferred_status_object_hit(
                    event, packet.payload, packet.inferred_direction()))
            else event
            for event in originals
        ]
        annotated = [
            self.player_root_action_objects.annotate(
                self.player_root_action_objects.annotate_client(
                    event,
                    timestamp=packet.timestamp_seconds,
                    packet_serial=self._damage_object_packet_serial,
                    job_conflicts=self._action_conflicts_with_confirmed_job,
                    skill_catalog=self.skill_catalog,
                ),
                packet_serial=self._damage_object_packet_serial,
                timestamp=packet.timestamp_seconds,
                job_conflicts=self._action_conflicts_with_confirmed_job,
                skill_catalog=self.skill_catalog,
                require_current_lifetime=event is not original,
            )
            if isinstance(event, CombatEvent)
            else event
            for original, event in zip(originals, candidates)
        ]
        annotated = [
            result if (candidate is original
                       or (result.attribution_method in {
                               "player_root_action_signal_object", "player_child_config_object",
                           }
                           and not str(result.skill_id).startswith("periodic:")))
            else original
            for original, candidate, result in zip(originals, candidates, annotated)
        ]
        # Apply action-response ownership last, but only when this physical
        # result also carries the reviewed explosion signature. An ordinary
        # triggering Hit may arrive first; ordering is not ownership proof.
        attributed = self.instant_action_damage.attribute(
            annotated,
            timestamp=packet.timestamp_seconds,
            packet_serial=self._damage_object_packet_serial,
            payload=packet.payload,
            direction=packet.inferred_direction(),
        )
        return attribute_compound_skill_responses(
            attributed, packet,
            job_conflicts=self._action_conflicts_with_confirmed_job,
        )

    def _finalize_late_field_hits(self, events, packet):
        return annotate_late_field_hits(events, packet, self.damage_object_origins,
            self._damage_object_packet_serial, skill_catalog=self.skill_catalog,
            job_conflicts=self._action_conflicts_with_confirmed_job)

    def __init__(
        self,
        data_root: Path,
        known_players: dict[str, dict[str, str]] | None = None,
        learned_profile_path: Path | None = None,
        action_parser_mode: str | None = None,
        known_actor_names: dict[int, str] | None = None,
    ) -> None:
        requested_mode = (
            action_parser_mode or configured_action_parser_mode()
        ).strip().casefold()
        self.action_parser_mode = (
            requested_mode if requested_mode in {"legacy", "structured"} else "structured"
        )
        # 观测日志：默认关闭（DRAGON_NEST_NEST_PROBE=1 才记录），未开启时是空操作。
        # 它只写事实，不参与任何判定，见 engine/live/nest_probe.py。
        probe_root = data_root.resolve()
        if len(probe_root.parents) > 1:
            probe_root = probe_root.parents[1]
        self.probe = get_probe(
            probe_root,
            header={
                "dataRoot": str(data_root.resolve()),
                "actionParserMode": self.action_parser_mode,
            },
        )
        self.structured_action_candidates = 0
        self.structured_action_emitted = 0
        self.structured_action_rejected = 0
        self.monster_templates = _load_monster_templates(
            str(data_root.resolve())
        )
        self.inspect_item_catalog = load_inspect_item_catalog(
            str(data_root.resolve())
        )
        self.inspect_enchant_catalog = load_inspect_enchant_catalog(
            str(data_root.resolve())
        )
        self.inspect_potential_catalog = load_inspect_potential_catalog(
            str(data_root.resolve())
        )
        self.inspect_inherent_potential_catalog = (
            load_inspect_inherent_potential_catalog(str(data_root.resolve()))
        )
        self.inspect_glyph_potential_catalog = (
            load_inspect_glyph_potential_catalog(str(data_root.resolve()))
        )
        self.inspect_suffix_potential_catalog = (
            load_inspect_suffix_potential_catalog(str(data_root.resolve()))
        )
        self.inspect_set_catalog = load_inspect_set_catalog(
            str(data_root.resolve())
        )
        self.inspect_card_mastery_catalog = load_inspect_card_mastery_catalog(
            str(data_root.resolve())
        )
        self.inspect_core_rune_catalog = load_inspect_core_rune_catalog(
            str(data_root.resolve())
        )
        self.inspect_engraving_potence_catalog = (
            load_inspect_engraving_potence_catalog(str(data_root.resolve()))
        )
        self.inspect_job_codes = _load_inspect_job_codes(str(data_root.resolve()))
        self.monster_template_ids = frozenset(self.monster_templates)
        self.monster_entities: dict[int, _MonsterTemplate] = {}
        self._monster_birth_phase: dict[int, int] = {}
        self._boss_phase_serial = 0
        self._boss_phase_name = ""
        self._fixed_boss_phase_names = frozenset(
            normalize_boss_hp_name(value)
            for profile in load_fixed_boss_hp_profiles()
            for name, aliases, _values in profile.bosses
            for value in (name, *aliases)
        )
        self.boss_actor_ids: set[int] = set()
        # 被「事件生成门」丢掉的 Boss 技能，按原因计数。
        #
        # 这类丢弃以前全是裸 ``continue``，抓包回放只能看到「没产出」，看不到
        # 「解出来了但被谁吃掉」。红龙佩达 P1/P2 那场事故就是这么藏了三周：
        # 34848 解出 4 条、发布 0 条，而日志里一片安静。
        # 回放测试靠 ``skill_drop_counts`` 做守恒断言：必须为空。
        self.skill_drop_counts: dict[str, int] = {}
        self.skill_drop_samples: list[dict[str, object]] = []
        self.boss_action_catalog = _load_boss_action_catalog(
            str(data_root.resolve())
        )
        self.boss_skill_catalog = _load_boss_skill_catalog(
            str(data_root.resolve())
        )
        self.player_buff_clear_boss_skill_ids = (
            _load_player_buff_clear_boss_skill_ids(str(data_root.resolve()))
        )
        self.recent_direct_player_buff_clears: dict[
            tuple[int, int, int], float
        ] = {}
        self.recent_holy_hammer_attacks: dict[tuple[int, int], float] = {}
        self.boss_action_parser = BossStructuredEventParser(
            self.boss_action_catalog
        )
        self.skill_catalog = _load_skill_catalog(str(data_root.resolve()))
        self.player_root_action_objects = PlayerRootActionObjectLedger()
        alternate_action_ids = {
            raw_skill_id
            for raw_skill_id, canonical_skill_id in _LIVE_DAMAGE_SKILL_ALIASES.items()
            if canonical_skill_id in self.skill_catalog
        }
        dungeon_special_skill_ids = frozenset(
            _DUNGEON_SPECIAL_SKILL_NAMES
        )
        self.action_skill_ids = (
            frozenset(self.skill_catalog)
            .union(alternate_action_ids)
            .union(dungeon_special_skill_ids)
        )
        base_damage_skill_ids = _load_damage_skill_ids(str(data_root.resolve()))
        alternate_damage_ids = {
            raw_skill_id
            for raw_skill_id, canonical_skill_id in _LIVE_DAMAGE_SKILL_ALIASES.items()
            if canonical_skill_id in base_damage_skill_ids
        }
        self.damage_skill_ids = (
            base_damage_skill_ids
            .union(alternate_damage_ids)
        )
        summon_names = {
            _summon_skill_key(name) for name in _SUMMON_CREATION_SKILL_NAMES
        }
        self.summon_creation_skill_ids = frozenset(
            skill_id
            for skill_id, (name, _job, _index) in self.skill_catalog.items()
            if _summon_skill_key(name) in summon_names
        )
        healing_names = {_summon_skill_key(name) for name in _HEALING_SKILL_NAMES}
        self.healing_skill_ids = frozenset(
            skill_id
            for skill_id, (name, _job, _index) in self.skill_catalog.items()
            if _summon_skill_key(name) in healing_names
        )
        self.status_skill_catalog = _load_status_skill_catalog(
            str(data_root.resolve())
        )
        self.status_known_skill_ids = (
            frozenset(self.skill_catalog)
            .union(self.status_skill_catalog)
            .union(alternate_action_ids)
            .union((34709, 32100, 325408))
        )
        self.team_buff_skill_names = _load_team_buff_skill_names(
            str(data_root.resolve())
        )
        self.status_effect_catalog = _load_status_effect_catalog(
            str(data_root.resolve())
        )
        self.debuff_evidence_registry: DebuffEvidenceRegistry[
            _ResolvedBossDebuff
        ] = DebuffEvidenceRegistry(
            enabled=os.environ.get(
                "DN_DEBUFF_EVIDENCE_MODE", "structured"
            ).strip().casefold()
            != "legacy"
        )
        self.periodic_damage_catalog = _load_periodic_damage_catalog(
            str(data_root.resolve())
        )
        periodic_source_kinds = {
            (rule.source_skill_id, rule.kind)
            for rules in self.periodic_damage_catalog.values()
            for rule in rules
        }
        periodic_source_kinds.update(
            (
                int(_canonical_live_skill_id(rule.source_skill_id) or 0),
                rule.kind,
            )
            for rules in self.periodic_damage_catalog.values()
            for rule in rules
        )
        self.periodic_source_kinds = frozenset(periodic_source_kinds)
        self.skill_profiles = _load_skill_profiles(str(data_root.resolve()))
        self.profile_jobs = _load_profile_jobs(str(data_root.resolve()))
        self.profile_lineages = _load_profile_lineages(str(data_root.resolve()))
        self.derived_combo_action_rules = load_derived_combo_action_rules(data_root)
        self.job_name_indexes: dict[str, int] = {}
        for lineage in self.profile_lineages.values():
            for job_name, job_index in lineage:
                previous = self.job_name_indexes.get(job_name, 99)
                self.job_name_indexes[job_name] = min(previous, job_index)
        self.historical_profile_jobs: dict[int, str] = {}
        self.known_players = known_players or {}
        # ``known_players`` is a legacy local hint collected across earlier
        # sessions. Runtime actor IDs are reusable, so neither an old exact ID
        # nor a unique historical profession can prove the current actor's
        # name. Keep the registry only for augmenting a job *after* a real
        # name has been observed; never seed the session identity map from it.
        self.known_actor_names: dict[int, str] = {}
        # S2C 22 10 repeatedly exposes every current party member's exact
        # character name and second-job code, but not their runtime actor ID.
        # Keep the two sides separate until actor/job evidence makes a unique
        # join possible; duplicate jobs deliberately remain unresolved.
        self.tcp_party_roster_names_by_job: dict[str, set[str]] = {}
        self.tcp_party_roster_last_seen: dict[tuple[str, str], float] = {}
        # The disk-free TCP listener can observe the scene identity packet
        # before recording starts.  Seed those profile/runtime aliases so the
        # 21 0C session trim does not also discard the real character names.
        for actor_id, player_name in (known_actor_names or {}).items():
            try:
                parsed_actor_id = int(actor_id)
            except (TypeError, ValueError):
                continue
            parsed_name = str(player_name or "").strip()
            if parsed_actor_id > 0 and parsed_name:
                self.known_actor_names[parsed_actor_id] = parsed_name
        self.player_spawn_identities: deque[
            DecodedTcpPlayerSpawnIdentity
        ] = deque()
        self.first_timestamp: float | None = None
        # ``21 0C`` is a scene resource initialization, not a Garden map ID.
        # The formal dungeon is identified from an exact map-ID packet when
        # available, with a unique 06 27 55 00 monster template retained only
        # as the compatibility fallback.
        self.detected_nest_key = ""
        self.detected_nest_label = ""
        self.detected_nest_name = ""
        self.detected_nest_confidence = ""
        self.detected_nest_template_id = 0
        self.detected_nest_observed_at = 0.0
        self.detected_map_id = 0
        self.detected_map_name = ""
        self.detected_map_layout = ""
        self.detected_map_active = False
        # Client-originated map records are destination selections, not proof
        # that loading completed. Keep the latest one for diagnostics and
        # accelerator flow recovery without letting it move battle zero,
        # overwrite the uploaded nest, or stop/start capture.
        self.detected_map_candidate_id = 0
        self.detected_map_candidate_name = ""
        self.detected_map_candidate_layout = ""
        self.detected_map_candidate_observed_at = 0.0
        self.detected_map_history: list[dict[str, object]] = []
        self.detected_unconfigured_map_ids: set[int] = set()
        self._scene_return_tracker = V576SceneReturnTracker()
        self._tcp_world_return_tracker = V576TcpWorldReturnTracker()
        self._latest_inferred_hub_row: dict[str, object] | None = None
        # The short UDP acknowledgement is generic and also occurs between
        # rooms.  Publish it separately for diagnostics; the legacy lobby
        # revision below advances only after the full verified scene chain.
        self.detected_scene_transition_ack_revision = 0
        self.detected_scene_transition_ack_observed_at = 0.0
        self.detected_scene_transition_ack_layout = ""
        self.detected_scene_transition_ack_route_code = 0
        self.detected_hub_return_revision = 0
        self.detected_lobby_return_revision = 0
        self.detected_lobby_return_observed_at = 0.0
        self.detected_lobby_return_layout = ""
        self.detected_lobby_return_route_code = 0
        self.detected_town_scene_load_revision = 0
        self.detected_town_scene_load_observed_at = 0.0
        self.local_name = ""
        self.local_actor_ids: set[int] = set()
        self.current_self_actor_id: int | None = None
        self.actor_jobs: dict[int, tuple[str, int]] = {}
        self.party_member_last_emit: dict[int, tuple[str, str, float]] = {}
        self.profile_probe_requests: deque[
            tuple[float, str, PacketRecord]
        ] = deque(maxlen=32)
        # One explicit inspect operation starts at 17 01, while the equipment
        # body is requested later by 03 15.  Keep the user-visible key stable
        # across both requests and the eventual server response so the two
        # overlay windows can open immediately and then upgrade in place.
        self.profile_inspection_sessions: dict[int, tuple[float, str]] = {}
        self.profile_probe_inspection_keys: dict[str, str] = {}
        self.profile_probe_responses: dict[tuple[str, str], float] = {}
        self.profile_identity_confirmations: dict[int, tuple[int, int]] = {}
        self.pending_self_profile: PlayerInspectEvent | None = None
        self.latest_self_profile: PlayerInspectEvent | None = None
        self.pending_self_item_archive: PlayerInspectEvent | None = None
        self.pending_glyph_page_request: int | None = None
        self.current_login_glyph_page: int | None = None
        self.current_login_glyph_actor_id: int | None = None
        self.active_glyph_page_by_actor: dict[int, int] = {}
        self.self_login_profile_packet: PacketRecord | None = None
        self.current_login_identity_marker: int | None = None
        self.current_login_job = ""
        self.current_login_job_codes: tuple[int, ...] = ()
        self.actor_skill_profiles: dict[int, set[str]] = {}
        self.latest_actor_skills: dict[int, tuple[int, float]] = {}
        # A party member receives a fresh runtime actor ID after reconnecting.
        # Remote reconnects do not reliably resend the TCP 03 03 identity row,
        # so keep a conservative, session-local alias ledger driven by the
        # exact 21 0C HP/MP fingerprint and later class/skill evidence.  The
        # replay normalizer remains as a final compatibility fallback, while
        # this ledger also keeps the live snapshot from splitting one player.
        self.player_runtime_aliases: dict[int, int] = {}
        self.player_resource_observations: dict[
            tuple[int, int], dict[int, float]
        ] = defaultdict(dict)
        self.player_actor_last_observed: dict[int, float] = {}
        self.player_actor_zero_hp_at: dict[int, float] = {}
        self.player_actor_observed_skills: dict[int, set[int]] = defaultdict(set)
        self.pending_runtime_alias_candidates: dict[
            int, tuple[int, tuple[int, int], float]
        ] = {}
        self.runtime_alias_evidence: list[dict[str, object]] = []
        self.recent_actor_casts: dict[int, list[tuple[int, float]]] = defaultdict(list)
        self.recent_damage_actor_casts: dict[
            int, list[tuple[int, float]]
        ] = {}
        self.skill_actor_ids: set[int] = set()
        self.recent_skill_casters: dict[int, list[tuple[int, float]]] = defaultdict(list)
        # Some party Buff status rows put the recipient in both the source
        # and target fields.  Keep the packet-proven caster for the lifetime
        # of that Buff so a later refresh (or one lost action packet) cannot
        # make every recipient appear to be the provider profession.
        self.active_recipient_mirror_buff_sources: dict[
            int, tuple[int, float, float]
        ] = {}
        self.summon_owners: dict[int, tuple[int, int]] = {}
        self.entity_owner_evidence = EntityOwnerEvidenceLedger()
        self.compact_entity_objects = CompactEntityObjectLedger()
        self._pve_scene_witnesses = {}
        self._pve_scene_seen_births = {}
        # A direct37 result carries the exact short-lived damage-object ID,
        # while a preceding client registration binds that ID to an unpacked
        # projectile template. Actions and 13 4A/4E responses can corroborate
        # that template, but neither creates a synthetic field owner. This
        # authoritative path runs before all cast-window inference.
        self.damage_object_origins = DamageObjectOriginLedger()
        self.field_effect_instances = FieldEffectInstanceLedger()
        self.instant_action_damage = InstantActionDamageLedger()
        install_decoder_adapter(self)
        self._damage_object_packet_serial = 0
        self.damage_object_skill_conflicts = 0
        # Absolute signed HP last observed for each short-lived summon actor.
        # This is intentionally separate from player HP calibration: summons
        # have no 21 0C maximum-resource row and their lethal 08 6F state may
        # be negative on the wire.
        self.summon_current_hp: dict[int, int] = {}
        self.summon_lethal_actor_ids: set[int] = set()
        self._cross_scene_prebattle_summons: dict[
            int, dict[str, object]
        ] = {}
        self._prebattle_inherited_events: list[BossStatusEvent] = []
        self.recent_skill_object_owners: dict[
            tuple[int, int], tuple[int, int, float]
        ] = {}
        self.damage_target_counts: Counter[int] = Counter()
        self.recent_damage_targets: deque[tuple[float, int]] = deque()
        self.recent_damage_target_counts: Counter[int] = Counter()
        self.confirmed_damage_target_ids: set[int] = set()
        # Targets independently established by a direct player-origin hit.
        # Unlike ``confirmed_damage_target_ids`` this excludes hits whose wire
        # source is a player-owned entity, so it is safe evidence for the later
        # entity-owner finalizer and cannot certify its own unknown target.
        self.direct_player_confirmed_damage_target_ids: set[int] = set()
        self.recent_confirmed_damage_targets: list[tuple[float, int, int]] = []
        self.boss_actor_id: int | None = None
        self.recent_status_events: dict[tuple[object, ...], float] = {}
        self.latest_skill_damage: dict[tuple[int, int], float] = {}
        self.recent_damage_amounts: dict[int, deque[int]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self.global_damage_amounts: deque[int] = deque(maxlen=512)
        self.filtered_damage_anomalies = 0
        self.active_periodic_damage: dict[
            tuple[int, int, str, int], _ActivePeriodicDamage
        ] = {}
        self.active_refreshable_auras: dict[
            tuple[int, int], _ActivePulsingAura
        ] = {}
        self.refreshable_aura_last_hits: dict[
            tuple[int, int, int], tuple[tuple[int, float], float]
        ] = {}
        self.continuation_profile_usage: dict[
            tuple[int, int, float], set[int]
        ] = {}
        self.direct_hit_profile_usage: Counter[
            tuple[int, int, float, int]
        ] = Counter()
        self.recent_emitted_casts: dict[tuple[int, int], float] = {}
        self.recent_status_backed_casts: dict[tuple[int, int], float] = {}
        self.active_permanent_player_buffs: dict[
            tuple[int, int], tuple[int, str, str]
        ] = {}
        self.prebattle_permanent_player_buffs: set[tuple[int, int]] = set()
        self.permanent_player_buff_targets: set[tuple[int, int, int]] = set()
        self.permanent_player_buff_known_actors: set[int] = set()
        self.aura_instances = AuraInstanceLedger()
        self.packet_aura_player_ids: set[int] = set()
        # Packet-proven template 33603 periodically refreshes the finite
        # Breath Buff.  This is separate from permanent aura state.
        self.active_azuna_breath_refresh_sources: dict[
            int, dict[str, object]
        ] = {}
        self.last_decoded_message = None
        self.last_boss_spawn_event: BossSpawnEvent | None = None
        self.last_monster_appearance_event: MonsterAppearanceEvent | None = None
        self.learned_profile_path = learned_profile_path
        self.learned_damage_profiles: dict[
            int, _LearnedDamageTimingProfile
        ] = {}
        self.learned_profiles_dirty = False
        self._load_learned_damage_profiles()

    def capture_boundary_evidence(self) -> dict[str, object]:
        """Export the formal-map fact accepted by the memory-only probe."""

        if not self.detected_nest_key or not self.detected_nest_name:
            return {}
        battle_row = next(
            (
                row
                for row in reversed(self.detected_map_history)
                if is_battle_map(
                    resolve_dungeon_map_id(int(row.get("map_id") or 0))
                )
            ),
            None,
        )
        return {
            "nest_key": self.detected_nest_key,
            "nest_label": self.detected_nest_label,
            "nest_name": self.detected_nest_name,
            "confidence": self.detected_nest_confidence,
            "template_id": int(self.detected_nest_template_id or 0),
            "map_id": int((battle_row or {}).get("map_id") or 0),
            "map_name": str((battle_row or {}).get("map_name") or ""),
            "map_layout": str(
                (battle_row or {}).get("layout")
                or self.detected_map_layout
                or ""
            ),
        }

    def _proven_current_prebattle_summons(
        self, boundary: float
    ) -> list[dict[str, object]]:
        """Return exact owner/template generations still live in this scene."""

        rows: list[dict[str, object]] = []
        ledger = self.entity_owner_evidence
        for entity_id, binding in tuple(ledger.bindings.items()):
            template = self.monster_entities.get(entity_id)
            if (
                template is None
                or template.boss
                or entity_id in self.summon_lethal_actor_ids
                or ledger.resolve(entity_id, template.template_id, boundary) is None
            ):
                continue
            owner_skill = self.summon_owners.get(entity_id)
            if owner_skill is None or int(owner_skill[0]) != int(
                binding.record.owner_id
            ):
                continue
            owner_id, skill_id = (int(owner_skill[0]), int(owner_skill[1]))
            if skill_id not in self.summon_creation_skill_ids:
                continue
            owner_name, owner_job = self._actor_display_context(owner_id)
            record = binding.record
            rows.append(
                {
                    "entity_id": int(entity_id),
                    "template_id": int(template.template_id),
                    "owner_id": owner_id,
                    "skill_id": skill_id,
                    "born_at": float(binding.born_at),
                    "current_hp": max(
                        0, int(self.summon_current_hp.get(entity_id, 0) or 0)
                    ),
                    "owner_name": str(owner_name or ""),
                    "owner_job": str(owner_job or ""),
                    "record": {
                        "observer_id": int(record.observer_id),
                        "record_offset": int(record.record_offset),
                        "xor_phase": int(record.xor_phase),
                        "fingerprint": str(record.fingerprint or "")[:4096],
                        "message_fingerprint": str(
                            record.message_fingerprint or ""
                        )[:128],
                    },
                }
            )
            if len(rows) >= 1024:
                break
        return rows

    def _forget_cross_scene_prebattle_summon(self, entity_id: int) -> None:
        self._cross_scene_prebattle_summons.pop(int(entity_id or 0), None)

    def _forget_cross_scene_prebattle_summons_for_owner(
        self, owner_id: int
    ) -> None:
        canonical_owner = self._canonical_player_actor_id(owner_id)
        self.active_azuna_breath_refresh_sources.pop(canonical_owner, None)
        for entity_id, row in tuple(
            self._cross_scene_prebattle_summons.items()
        ):
            row_owner = self._canonical_player_actor_id(
                int(row.get("owner_id") or 0)
            )
            if row_owner == canonical_owner:
                self._cross_scene_prebattle_summons.pop(entity_id, None)

    def _remember_cross_scene_prebattle_summons(
        self, transition_timestamp: float
    ) -> None:
        """Preserve reviewed persistent summons before scene-scoped reset."""

        try:
            transition = float(transition_timestamp)
        except (TypeError, ValueError):
            return
        if transition <= 0.0:
            return
        for entity_id, row in tuple(
            self._cross_scene_prebattle_summons.items()
        ):
            try:
                carried_at = float(row.get("carried_at") or 0.0)
            except (TypeError, ValueError):
                carried_at = 0.0
            if not 0.0 <= transition - carried_at <= (
                _CROSS_SCENE_PREBATTLE_MAX_GAP_SECONDS
            ):
                self._cross_scene_prebattle_summons.pop(entity_id, None)

        for row in self._proven_current_prebattle_summons(transition):
            template_id = int(row.get("template_id") or 0)
            skill_id = int(row.get("skill_id") or 0)
            if (
                _CROSS_SCENE_PREBATTLE_SUMMON_SKILLS.get(template_id)
                != skill_id
            ):
                continue
            inherited = dict(row)
            inherited["cross_scene_inherited"] = True
            inherited["carried_at"] = transition
            self._cross_scene_prebattle_summons[
                int(row["entity_id"])
            ] = inherited

    def capture_prebattle_summon_state(
        self, boundary_timestamp: float
    ) -> tuple[dict[str, object], ...]:
        """Export current plus reviewed cross-scene summon generations."""

        try:
            boundary = float(boundary_timestamp)
        except (TypeError, ValueError):
            return ()
        if boundary <= 0.0:
            return ()
        # Prefer a proactive boundary snapshot over waiting for this
        # permanent companion's first attack in the formal capture.  The
        # prewarmer still requires a real current spawn/template generation
        # and exactly one compatible player profile; job presence alone is
        # never enough.
        prewarm_permanent_summon_lives(self, boundary)
        rows = self._proven_current_prebattle_summons(boundary)
        current_ids = {int(row["entity_id"]) for row in rows}
        for entity_id, stored in tuple(
            self._cross_scene_prebattle_summons.items()
        ):
            try:
                template_id = int(stored.get("template_id") or 0)
                skill_id = int(stored.get("skill_id") or 0)
                carried_at = float(stored.get("carried_at") or 0.0)
            except (TypeError, ValueError):
                self._cross_scene_prebattle_summons.pop(entity_id, None)
                continue
            current_template = self.monster_entities.get(entity_id)
            valid = bool(
                entity_id not in current_ids
                and entity_id not in self.summon_lethal_actor_ids
                and current_template is not None
                and int(current_template.template_id) == template_id
                and _CROSS_SCENE_PREBATTLE_SUMMON_SKILLS.get(template_id)
                == skill_id
                and 0.0 <= boundary - carried_at
                <= _CROSS_SCENE_PREBATTLE_MAX_GAP_SECONDS
            )
            if not valid:
                self._cross_scene_prebattle_summons.pop(entity_id, None)
                continue
            rows.append(dict(stored))
            if len(rows) >= 1024:
                break
        rows.sort(key=lambda row: int(row["entity_id"]))
        return tuple(rows)

    def _seed_prebattle_summons(self, state: object) -> int:
        if not isinstance(state, dict) or int(state.get("version") or 0) != 1:
            return 0
        try:
            boundary = float(state.get("boundary_timestamp") or 0.0)
        except (TypeError, ValueError):
            return 0
        values = state.get("summons")
        if boundary <= 0.0 or not isinstance(values, list):
            return 0
        seeded = 0
        for row in values[:1024]:
            if not isinstance(row, dict):
                continue
            try:
                entity_id = int(row.get("entity_id") or 0)
                template_id = int(row.get("template_id") or 0)
                owner_id = int(row.get("owner_id") or 0)
                skill_id = int(row.get("skill_id") or 0)
                born_at = float(row.get("born_at") or boundary)
                current_hp = max(0, int(row.get("current_hp") or 0))
                carried_at = float(row.get("carried_at") or boundary)
            except (TypeError, ValueError):
                continue
            template = self.monster_templates.get(template_id)
            cross_scene_inherited = bool(
                row.get("cross_scene_inherited", False)
            )
            if (
                not 0 < entity_id < 0x10000
                or not 0x01000000 <= owner_id <= 0xFFFFFFFF
                or template is None
                or template.boss
                or skill_id not in self.summon_creation_skill_ids
                or not 0.0 <= boundary - born_at <= 3600.0
                or (
                    cross_scene_inherited
                    and (
                        _CROSS_SCENE_PREBATTLE_SUMMON_SKILLS.get(template_id)
                        != skill_id
                        or not 0.0 <= boundary - carried_at
                        <= _CROSS_SCENE_PREBATTLE_MAX_GAP_SECONDS
                    )
                )
            ):
                continue
            record_data = row.get("record")
            if not isinstance(record_data, dict):
                continue
            try:
                record = EntityOwnerRecord(
                    entity_id=entity_id,
                    owner_id=owner_id,
                    observer_id=int(record_data.get("observer_id") or owner_id),
                    record_offset=max(
                        1, int(record_data.get("record_offset") or 1)
                    ),
                    xor_phase=max(0, int(record_data.get("xor_phase") or 0)),
                    fingerprint=str(record_data.get("fingerprint") or "")[:4096],
                    message_fingerprint=str(
                        record_data.get("message_fingerprint") or ""
                    )[:128],
                )
            except (TypeError, ValueError):
                continue
            proof = (
                f"prebattle:{entity_id}:{template_id}:"
                f"{record.message_fingerprint or record.fingerprint}"
            )
            seed_born_at = min(boundary, max(born_at, boundary - 3599.0))
            if not self.entity_owner_evidence.observe_birth(
                entity_id,
                template_id,
                seed_born_at,
                proof,
                1,
                birth_source="prebattle_snapshot",
            ):
                continue
            self.entity_owner_evidence.observe_record(
                record,
                min(boundary, seed_born_at + 0.001),
                2,
            )
            binding = self.entity_owner_evidence.resolve(
                entity_id, template_id, boundary
            )
            if binding is None or binding.record.owner_id != owner_id:
                self.entity_owner_evidence.retire(
                    entity_id, reason="invalid_prebattle_snapshot"
                )
                continue
            self.monster_entities[entity_id] = template
            self.summon_owners[entity_id] = (owner_id, skill_id)
            if current_hp > 0:
                self.summon_current_hp[entity_id] = current_hp
            owner_name = str(row.get("owner_name") or "").strip()
            if owner_name:
                self.known_actor_names[owner_id] = owner_name
            owner_job = str(row.get("owner_job") or "").strip()
            if owner_job:
                stage = int(self.job_name_indexes.get(owner_job, 99))
                self.actor_jobs[owner_id] = (owner_job, stage)
            seeded += 1
        return seeded

    def drain_prebattle_inherited_events(self) -> list[BossStatusEvent]:
        events = list(self._prebattle_inherited_events)
        self._prebattle_inherited_events.clear()
        return events

    def seed_monster_entities(self, seeds: dict[int, int] | None) -> int:
        """把待机探针/技能号反查到的 Boss 实体档案注入本解码器。

        进本后才点开始统计时，Boss 的出场包已经过去；没有这张表，血量包和
        技能包都会因为「不认识这个 actor」被丢掉，表现为悬浮窗「未识别」、
        语音/倒计时全空。返回成功注入的条数。

        「是不是 Boss」不只看模板自带的 ``boss`` 标记：沙龙的「流沙之下的沙虫」
        (4150051) 这类关卡 Boss 在模板表里是 ``boss=False``，但在
        ``boss_hp_profiles.json`` 的副本 Boss 模板集合里。两处任一命中就算
        Boss，否则注入之后技能包仍会被 ``boss_actor_ids`` 过滤掉。
        """

        if not isinstance(seeds, dict):
            return 0
        try:
            from engine.native.nest_inference import (
                template_nest_index,
            )

            nest_templates = template_nest_index()
        except Exception:
            nest_templates = {}
        applied = 0
        for raw_actor, raw_template in seeds.items():
            try:
                actor_id = int(raw_actor)
                template_id = int(raw_template)
            except (TypeError, ValueError):
                continue
            template = self.monster_templates.get(template_id)
            if template is None:
                continue
            self.monster_entities[actor_id] = template
            if getattr(template, "boss", False) or template_id in nest_templates:
                self.boss_actor_ids.add(actor_id)
            applied += 1
        return applied

    def seed_capture_boundary_evidence(
        self,
        evidence: dict[str, object] | None,
    ) -> list[BossStatusEvent]:
        """Restore probe-confirmed entry evidence at combat-relative 0s."""

        row = dict(evidence or {})
        try:
            boundary = float(row.get("boundary_timestamp") or 0.0)
        except (TypeError, ValueError):
            boundary = 0.0
        if isfinite(boundary) and boundary > 0 and self.first_timestamp is None:
            self.first_timestamp = boundary
        nest_key = str(row.get("nest_key") or "").strip()
        nest_name = str(row.get("nest_name") or "").strip()
        confidence = str(row.get("confidence") or "").strip()
        valid_nest = bool(nest_key and nest_name and confidence in {
            "exact_map_id",
            "entry_template",
            "template",
            "template_set",
            "dominant_template_set",
        })
        try:
            map_id = int(row.get("map_id") or 0)
        except (TypeError, ValueError):
            map_id = 0
        entry = resolve_dungeon_map_id(map_id)
        if confidence == "exact_map_id" and not is_battle_map(entry):
            valid_nest = False
        # Manual recording may begin in town with no dungeon evidence. Buff
        # inheritance is independent of map recognition, and must not invent
        # a battle map merely to restore a packet-backed player state.
        if valid_nest:
            self.detected_nest_key = nest_key
            self.detected_nest_label = str(row.get("nest_label") or nest_name).strip()
            self.detected_nest_name = nest_name
            self.detected_nest_confidence = confidence
            self.detected_nest_template_id = int(row.get("template_id") or 0)
            self.detected_nest_observed_at = 0.0
            self._scene_return_tracker.observe_battle_context(
                observed_at=0.0, context_key=nest_key,
            )
        state = row.get("prebattle_state")
        self._seed_prebattle_summons(state)
        inherited_by_key: dict[tuple[object, ...], BossStatusEvent] = {}
        # Summon lifetime and active Buff state are inherited independently.
        # A real 2619xx application preserves its exact remaining interval;
        # when it predates ACT startup, a proven 33603 generation may seed the
        # reviewed finite 30-second / five-second-refresh client-AI state.
        packet_inherited = list(inherited_player_buff_events(state))
        from engine.live.holy_hammer_actions import observe_buffs
        observe_buffs(self, packet_inherited)
        for event in packet_inherited:
            if event.skill_id in {str(skill) for skill in AURA_SKILL_IDS}:
                self.aura_instances.observe(event)
                self.packet_aura_player_ids.update((int(event.source_id, 0), int(event.target_id, 0)))
        # Inherit only verified recipients, keeping their response instances
        # so later UDP ends still match after the decoder handoff. A late
        # roster identity never establishes receipt of a range-limited aura.
        for event in packet_inherited:
            for value in (event.source_id, event.target_id):
                try:
                    actor_id = int(str(value or "").strip(), 0)
                except (TypeError, ValueError):
                    continue
                if actor_id > _SUMMON_ACTOR_ID_MAX:
                    self.skill_actor_ids.add(actor_id)
            try:
                inherited_skill_id = int(event.skill_id)
            except (TypeError, ValueError):
                inherited_skill_id = 0
            try:
                source_actor_id = int(event.source_id, 0)
            except (TypeError, ValueError):
                source_actor_id = 0
            if (
                event.permanent
                and inherited_skill_id in _PERMANENT_PLAYER_BUFF_SKILLS
                and source_actor_id > _SUMMON_ACTOR_ID_MAX
            ):
                for maintained in self._activate_permanent_player_buff(
                    source_actor_id=source_actor_id,
                    skill_id=inherited_skill_id,
                    timestamp_ms=0,
                    prebattle_inherited=True,
                ):
                    inherited_by_key[
                        (
                            maintained.target_id,
                            maintained.source_id,
                            maintained.skill_id,
                            maintained.effect_key,
                        )
                    ] = maintained
        for event in packet_inherited:
            inherited_by_key[
                (
                    event.target_id,
                    event.source_id,
                    event.skill_id,
                    event.effect_key,
                )
            ] = event
        inherited = list(inherited_by_key.values())
        self._restore_inherited_azuna_breath_refresh(
            inherited,
            float(state.get("boundary_timestamp") or 0.0)
            if isinstance(state, dict)
            else 0.0,
        )
        for maintained in self._seed_azuna_breath_companion_refresh(
            float(state.get("boundary_timestamp") or 0.0)
            if isinstance(state, dict)
            else 0.0,
        ):
            inherited_by_key.setdefault(
                (
                    maintained.target_id,
                    maintained.source_id,
                    maintained.skill_id,
                    maintained.effect_key,
                ),
                maintained,
            )
        inherited = list(inherited_by_key.values())
        self._prebattle_inherited_events.extend(inherited)
        if not valid_nest or not is_battle_map(entry):
            return inherited

        map_name = str(row.get("map_name") or entry.display_name).strip()
        map_layout = str(
            row.get("map_layout") or "probe-handoff-exact-map-id"
        ).strip()
        self.detected_map_id = map_id
        self.detected_map_name = map_name
        self.detected_map_layout = map_layout
        self.detected_map_active = True
        self._scene_return_tracker.observe_map(
            map_id,
            active=True,
            observed_at=0.0,
        )
        if not any(
            int(candidate.get("map_id") or 0) == map_id
            and candidate.get("left_at") is None
            for candidate in self.detected_map_history
        ):
            self.detected_map_history.append(
                {
                    "map_id": map_id,
                    "map_name": map_name,
                    "nest_name": canonical_nest_name(map_name),
                    "official_name": entry.official_name,
                    "map_type": entry.map_type,
                    "content_kind": entry.content_kind,
                    "observed_at": 0.0,
                    "layout": map_layout,
                    "confidence": "exact_map_id",
                    "inferred": False,
                    "probe_handoff": True,
                }
            )
        return inherited

    def _observe_map_identity(self, packet: PacketRecord) -> None:
        """Promote an exact map packet above ambiguous monster templates."""

        if str(packet.transport or "").upper() not in {"UDP", "TCP"}:
            return
        decoded = decode_v576_map_identity(
            packet.payload,
            packet.inferred_direction(),
            connection_context=packet,
        )
        if decoded is None:
            self.probe.note_map_attempt(
                hit=False,
                length=len(packet.payload),
                transport=packet.transport,
                direction=packet.inferred_direction(),
            )
            return
        origin = self.first_timestamp or packet.timestamp_seconds
        observed_at = max(0.0, packet.timestamp_seconds - origin)
        entry = resolve_dungeon_map_id(decoded.map_id)
        self.probe.note_map_attempt(
            hit=True,
            map_id=int(decoded.map_id),
            layout=str(getattr(decoded, "layout", "")),
            active=bool(getattr(decoded, "active", False)),
            authoritative=bool(getattr(decoded, "authoritative", True)),
            known=entry is not None and bool(entry.display_name),
            name=(entry.display_name if entry is not None else ""),
            battle=bool(entry is not None and is_battle_map(entry)),
        )
        if not bool(getattr(decoded, "authoritative", True)):
            self.detected_map_candidate_id = int(decoded.map_id)
            self.detected_map_candidate_name = (
                entry.display_name
                if entry is not None and entry.display_name
                else ""
            )
            self.detected_map_candidate_layout = decoded.layout
            self.detected_map_candidate_observed_at = observed_at
            if entry is None or not entry.display_name:
                self.detected_unconfigured_map_ids.add(int(decoded.map_id))
            if entry is not None and is_battle_map(entry):
                self._scene_return_tracker.observe_map_candidate(
                    int(decoded.map_id),
                    observed_at=observed_at,
                )
                if self._latest_inferred_hub_row is not None:
                    promote_hub_row_to_time_space_garden(
                        self._latest_inferred_hub_row
                    )
                    self._latest_inferred_hub_row = None
            return
        scene_return = self._scene_return_tracker.observe_map(
            int(decoded.map_id),
            active=bool(decoded.active),
            observed_at=observed_at,
        )
        if (
            not decoded.active
            and decoded.layout
            == "s2c-tcp-scene-map-release-322-v576"
        ):
            # This large authoritative record explicitly releases the whole
            # scene.  Treat it as independent teardown evidence as well as a
            # map leave; some accelerator/guest captures do not contain the
            # shorter 12 24 teardown envelope.
            self._scene_return_tracker.observe_scene_teardown(
                observed_at=observed_at,
            )
        if scene_return is not None:
            self._publish_verified_hub_return(scene_return)
        if entry is None or not entry.display_name:
            self.detected_unconfigured_map_ids.add(int(decoded.map_id))
            return
        if (
            decoded.active
            and is_battle_map(entry)
            and self._latest_inferred_hub_row is not None
        ):
            promote_hub_row_to_time_space_garden(
                self._latest_inferred_hub_row
            )
            self._latest_inferred_hub_row = None
        map_row = {
            "map_id": decoded.map_id,
            "map_name": entry.display_name,
            "nest_name": (
                canonical_nest_name(entry.display_name)
                if is_battle_map(entry)
                else ""
            ),
            "official_name": entry.official_name,
            "map_type": entry.map_type,
            "content_kind": entry.content_kind,
            "observed_at": observed_at,
            "layout": decoded.layout,
            "confidence": "exact_map_id",
            "inferred": False,
        }
        identity_row = map_row
        if decoded.active:
            if (
                not self.detected_map_history
                or int(self.detected_map_history[-1].get("map_id") or 0)
                != decoded.map_id
                or self.detected_map_history[-1].get("left_at") is not None
            ):
                self.detected_map_history.append(map_row)
            else:
                identity_row = self.detected_map_history[-1]
        else:
            matching_row = next(
                (
                    row
                    for row in reversed(self.detected_map_history)
                    if int(row.get("map_id") or 0) == decoded.map_id
                    and row.get("left_at") is None
                ),
                None,
            )
            if matching_row is None:
                matching_row = next(
                    (
                        row
                        for row in reversed(self.detected_map_history)
                        if int(row.get("map_id") or 0) == decoded.map_id
                        and row.get("left_at") is not None
                        and 0.0
                        <= observed_at - float(row.get("left_at") or 0.0)
                        <= 2.0
                    ),
                    None,
                )
            if matching_row is None:
                map_row["observed_at"] = (
                    0.0 if not self.detected_map_history else observed_at
                )
                map_row["entry_inferred_from_exit"] = True
                self.detected_map_history.append(map_row)
                matching_row = map_row
            matching_row["left_at"] = observed_at
            matching_row["exit_layout"] = decoded.layout
            identity_row = matching_row
        self.detected_map_id = decoded.map_id
        self.detected_map_name = entry.display_name
        self.detected_map_layout = decoded.layout
        self.detected_map_active = bool(decoded.active)
        # Towns and world-zone entrances belong in the location history, but
        # must never replace the dungeon identity used by scoring/upload.
        if not is_battle_map(entry):
            return
        # Maps that form one dungeon share an entry condition.  Use it as the
        # stable key while retaining the exact current map ID separately.
        root_id = entry.enter_condition_table_id or decoded.map_id
        self.detected_nest_key = f"map:{root_id}"
        canonical_name = canonical_nest_name(entry.display_name)
        self.detected_nest_label = canonical_name
        self.detected_nest_name = canonical_name
        self.detected_nest_confidence = "exact_map_id"
        self.detected_nest_template_id = 0
        self.detected_nest_observed_at = max(
            0.0,
            float(identity_row.get("observed_at") or 0.0),
        )
        self.probe.record(
            "nest_identity",
            route="map",
            map_id=int(decoded.map_id),
            root_id=int(root_id),
            nest_key=self.detected_nest_key,
            nest_name=self.detected_nest_name,
            classic_regex_match=bool(classic_nest_hint(self.detected_nest_name)),
        )

    def _observe_town_scene_load(self, packet: PacketRecord) -> None:
        if not decode_v576_town_scene_load(
            packet.payload,
            packet.inferred_direction(),
            connection_context=packet,
        ):
            return
        origin = self.first_timestamp or packet.timestamp_seconds
        self.detected_town_scene_load_revision += 1
        self.detected_town_scene_load_observed_at = max(
            0.0, packet.timestamp_seconds - origin
        )

    def _observe_tcp_world_return(self, packet: PacketRecord) -> None:
        """Recover a verified return when the new world omits town-map TCP."""

        tracker = getattr(self, "_tcp_world_return_tracker", None)
        if tracker is None:
            return
        active_map_id = (
            int(getattr(self, "detected_map_id", 0) or 0)
            if bool(getattr(self, "detected_map_active", False)) else 0
        )
        origin = self.first_timestamp or packet.timestamp_seconds
        observation = tracker.observe(
            packet,
            observed_at=max(0.0, packet.timestamp_seconds - origin),
            active_battle_map_id=active_map_id,
        )
        if observation is not None:
            self._publish_verified_hub_return(observation)
            self.detected_map_active = False

    def _observe_scene_teardown(self, packet: PacketRecord) -> None:
        """Feed the verified TCP scene-close record to the boundary tracker."""

        if str(packet.transport or "").upper() != "TCP":
            return
        decoded = decode_v576_scene_teardown(
            packet.payload,
            packet.inferred_direction(),
            connection_context=packet,
        )
        if decoded is None:
            return
        origin = self.first_timestamp or packet.timestamp_seconds
        observed_at = max(0.0, packet.timestamp_seconds - origin)
        self._scene_return_tracker.observe_scene_teardown(
            observed_at=observed_at,
        )
        # The teardown is also the best available end marker when a
        # non-leader never receives the short 82 45 map-leave packet. Closing
        # the active row lets a later re-entry into the same map become a new
        # run instead of being deduplicated into the old one.
        for row in reversed(self.detected_map_history):
            if int(row.get("map_type") or 0) != 3:
                continue
            if row.get("left_at") is None:
                row["left_at"] = observed_at
                row["exit_layout"] = decoded.layout
            break
        # Runtime object IDs and local generations are scene scoped. Nothing
        # created in a completed battle map may own damage after teardown.
        # A very small reviewed set of persistent companions is copied into a
        # quarantine snapshot first; it remains unusable for live attribution
        # until the formal-map decoder is recreated and validates the handoff.
        self._remember_cross_scene_prebattle_summons(
            packet.timestamp_seconds
        )
        self.damage_object_origins.reset()
        self.player_root_action_objects.reset_scene()
        self.field_effect_instances.reset()
        self.active_periodic_damage.clear()
        self.instant_action_damage.reset()
        self.entity_owner_evidence.reset()
        self.compact_entity_objects.reset()
        self._pve_scene_witnesses.clear()
        self.summon_owners.clear()
        self.active_azuna_breath_refresh_sources.clear()
        self.recent_skill_object_owners.clear()
        self._boss_phase_name = ""
        current_entry = resolve_dungeon_map_id(self.detected_map_id)
        if self.detected_map_active and is_battle_map(current_entry):
            self.detected_map_active = False

    def _publish_verified_hub_return(
        self,
        observation: VerifiedV576HubReturn,
    ) -> None:
        """Publish one correlated hub boundary to history and auto-stop."""

        self.detected_hub_return_revision += 1
        # Preserve the legacy field for callers in earlier UI/service code,
        # but give it the corrected, fully verified semantics.
        self.detected_lobby_return_revision = self.detected_hub_return_revision
        self.detected_lobby_return_observed_at = observation.observed_at
        self.detected_lobby_return_layout = observation.evidence_layout
        self.detected_lobby_return_route_code = observation.route_code
        row = inferred_hub_history_row(observation)
        if observation.next_battle_map_id is not None:
            promote_hub_row_to_time_space_garden(row)
            self._latest_inferred_hub_row = None
        elif observation.destination_map_id is not None:
            # The destination is already fixed by terminal-route metadata;
            # a later dungeon entry must not relabel this Heaven return as a
            # hidden Garden visit.
            self._latest_inferred_hub_row = None
        else:
            self._latest_inferred_hub_row = row
        self.detected_map_history.append(row)
        self.detected_map_history.sort(
            key=lambda item: float(item.get("observed_at") or 0.0)
        )

    def _observe_lobby_return(self, packet: PacketRecord) -> None:
        """Record the generic scene acknowledgement without overclaiming it."""

        if str(packet.transport or "").upper() != "UDP":
            return
        decoded = decode_v576_lobby_return(
            packet.payload,
            packet.inferred_direction(),
        )
        if decoded is None:
            return
        origin = self.first_timestamp or packet.timestamp_seconds
        observed_at = max(0.0, packet.timestamp_seconds - origin)
        if not self._scene_return_tracker.observe_transition_ack(
            observed_at=observed_at,
            route_code=decoded.route_code,
        ):
            return
        self.detected_scene_transition_ack_revision += 1
        self.detected_scene_transition_ack_observed_at = observed_at
        self.detected_scene_transition_ack_layout = decoded.layout
        self.detected_scene_transition_ack_route_code = decoded.route_code
        terminal_return = (
            self._scene_return_tracker.consume_configured_terminal_return()
        )
        if terminal_return is not None:
            self._publish_verified_hub_return(terminal_return)

    def _observe_gateway_route_catalog(self, packet: PacketRecord) -> None:
        """Finish a battle exit when the server reloads gateway routes."""

        if str(packet.transport or "").upper() != "TCP":
            return
        decoded = decode_v576_gateway_route_catalog(
            packet.payload,
            packet.inferred_direction(),
            connection_context=packet,
        )
        if decoded is None:
            return
        origin = self.first_timestamp or packet.timestamp_seconds
        observed_at = max(0.0, packet.timestamp_seconds - origin)
        observation = self._scene_return_tracker.observe_gateway_route_catalog(
            observed_at=observed_at,
        )
        if observation is not None:
            self._publish_verified_hub_return(observation)

    def _observe_resource_scene_return(
        self,
        packet: PacketRecord,
        health_events: list[LiveDpsEvent],
    ) -> None:
        """Confirm a hub return only after the fresh-scene 21 0C sync."""

        if not any(
            getattr(event, "packet_kind", "")
            == "s2c-udp-player-resource-sync-210c-v576"
            for event in health_events
        ):
            return
        origin = self.first_timestamp or packet.timestamp_seconds
        observed_at = max(0.0, packet.timestamp_seconds - origin)
        observation = self._scene_return_tracker.observe_resource_sync(
            observed_at=observed_at,
        )
        if observation is None:
            return
        self._publish_verified_hub_return(observation)

    def feed(
        self,
        packet: PacketRecord,
        packet_features: int | None = None,
    ) -> CombatEvent | None:
        self.last_boss_spawn_event = None
        self.last_monster_appearance_event = None
        self._retry_corrected_primary = False
        identity_target_corrected = False
        corrected_target_was_known = False
        if self.first_timestamp is None:
            self.first_timestamp = packet.timestamp_seconds
        packet_direction = packet.inferred_direction()
        self.probe.observe_packet(packet.transport, packet_direction, packet.payload)
        monster_spawn = decode_v576_monster_spawn(
            packet.payload,
            packet_direction,
            # A structurally valid unknown template still invalidates the old
            # lifetime. Catalog completeness must not decide entity reuse.
            None,
        )
        self.probe.note_spawn_attempt(
            hit=monster_spawn is not None,
            length=len(packet.payload),
            transport=packet.transport,
            direction=packet_direction,
            actor=(f"0x{monster_spawn.actor_id:08X}" if monster_spawn else ""),
            template_id=(monster_spawn.template_id if monster_spawn else 0),
        )
        if monster_spawn is not None:
            # Creation evidence, independent of damage and Boss classification.
            # Only the announcement target is retained; no per-packet snapshots.
            if monster_spawn.template_id in {4237036, 4237024, 503691, 503693, 4181001, 4181009, 4150065, 4150050, 4160001}:
                origin = self.first_timestamp or packet.timestamp_seconds
                self.last_monster_appearance_event = MonsterAppearanceEvent(
                    timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
                    target_id=f"0x{monster_spawn.actor_id:08X}@{monster_spawn.template_id}",
                    template_id=monster_spawn.template_id,
                )
                voice_feed = getattr(self, "voice_signal_feed", None)
                if voice_feed is not None:
                    voice_feed.publish({
                        "kind": "monster_spawn", "skill": 0,
                        "source": self.last_monster_appearance_event.target_id,
                        "timestamp": self.last_monster_appearance_event.timestamp_ms,
                        "templateId": monster_spawn.template_id,
                    })
            template = self.monster_templates.get(monster_spawn.template_id)
            if template is None:
                template = _MonsterTemplate(
                    template_id=monster_spawn.template_id,
                    name=f"未识别实体 {monster_spawn.template_id}",
                    level=0, hp=0, physical_defense=0, magic_defense=0,
                    critical_resistance=0, fire_resistance=0, ice_resistance=0,
                    light_resistance=0, dark_resistance=0, actor_table_id=0,
                    boss=False,
                )
            self.probe.record(
                "spawn_template",
                actor=f"0x{monster_spawn.actor_id:08X}",
                template_id=monster_spawn.template_id,
                known=monster_spawn.template_id in self.monster_templates,
                boss=bool(getattr(template, "boss", False)),
                name=str(getattr(template, "name", ""))[:40],
            )
            if template is not None and (
                template.boss or self.entity_owner_evidence.observe_birth(
                    monster_spawn.actor_id, monster_spawn.template_id,
                    packet.timestamp_seconds, sha256(packet.payload).hexdigest(), self._damage_object_packet_serial
                )
            ):
                # A new independently proven generation owns this short ID.
                # It supersedes any portable generation from the prior scene,
                # even when the template happens to be identical.
                self._forget_cross_scene_prebattle_summon(
                    monster_spawn.actor_id
                )
                nest_resolution = resolve_nest_template(
                    monster_spawn.template_id
                )
                if nest_resolution is not None:
                    origin = self.first_timestamp or packet.timestamp_seconds
                    self._scene_return_tracker.observe_battle_context(
                        observed_at=max(
                            0.0,
                            packet.timestamp_seconds - origin,
                        ),
                        context_key=nest_resolution.profile.key,
                    )
                if not self.detected_nest_key:
                    if nest_resolution is not None:
                        profile = nest_resolution.profile
                        self.detected_nest_key = profile.key
                        self.detected_nest_label = profile.label
                        self.detected_nest_name = profile.display_name
                        self.detected_nest_confidence = (
                            nest_resolution.confidence
                        )
                        self.detected_nest_template_id = (
                            nest_resolution.template_id
                        )
                        origin = self.first_timestamp or packet.timestamp_seconds
                        self.detected_nest_observed_at = max(
                            0.0, packet.timestamp_seconds - origin
                        )
                        self.probe.record(
                            "nest_identity",
                            route="template",
                            template_id=int(monster_spawn.template_id),
                            nest_key=self.detected_nest_key,
                            nest_name=self.detected_nest_name,
                            classic_regex_match=bool(
                                classic_nest_hint(self.detected_nest_name)
                            ),
                        )
                # Actor IDs are reused between stage transitions. Assignment,
                # rather than setdefault, keeps replay and live capture correct.
                phase_name = normalize_boss_hp_name(template.name)
                # 阶段体（红龙佩达 503681/503682）在客户端怪物表里没标 boss，
                # 只靠 table 标记会把这些 actor 从 boss_actor_ids 里剔掉，
                # 于是它们的状态包整段不解析 —— 表现就是「进本后一条机制提示都没有」。
                # 固定血量档案里登记过的 Boss 名字同样算数（这个集合本来只用于
                # 阶段计数，是个遗漏）。
                is_boss = bool(template.boss) or phase_name in self._fixed_boss_phase_names
                if is_boss and phase_name != self._boss_phase_name:
                    self._boss_phase_serial += 1
                    self._boss_phase_name = phase_name
                self.monster_entities[monster_spawn.actor_id] = template
                self._monster_birth_phase[monster_spawn.actor_id] = (
                    self._boss_phase_serial
                )
                if is_boss:
                    observe_scene_boss(self, packet)
                    self.boss_actor_ids.add(monster_spawn.actor_id)
                else:
                    self.boss_actor_ids.discard(monster_spawn.actor_id)
                self.probe.record(
                    "boss_bind",
                    actor=f"0x{monster_spawn.actor_id:08X}",
                    template_id=monster_spawn.template_id,
                    boss=is_boss,
                    bossActorCount=len(self.boss_actor_ids),
                    phase=self._boss_phase_name,
                )
                self.skill_actor_ids.discard(monster_spawn.actor_id)
                self.local_actor_ids.discard(monster_spawn.actor_id)
                self.actor_jobs.pop(monster_spawn.actor_id, None)
                self.actor_skill_profiles.pop(monster_spawn.actor_id, None)
                self.summon_owners.pop(monster_spawn.actor_id, None)
                self.summon_current_hp.pop(monster_spawn.actor_id, None)
                self.summon_lethal_actor_ids.discard(monster_spawn.actor_id)
                for object_key in tuple(self.recent_skill_object_owners):
                    if monster_spawn.actor_id in object_key:
                        self.recent_skill_object_owners.pop(object_key, None)
                if is_boss:
                    self.boss_actor_id = monster_spawn.actor_id
                    target_name, target_job = self._monster_display_context(
                        monster_spawn.actor_id
                    )
                    origin = self.first_timestamp or packet.timestamp_seconds
                    self.last_boss_spawn_event = BossSpawnEvent(
                        timestamp_ms=max(
                            0,
                            round((packet.timestamp_seconds - origin) * 1000),
                        ),
                        target_id=self._monster_event_id(monster_spawn.actor_id),
                        target_name=target_name,
                        target_job=target_job,
                        target_max_hp=max(0, int(template.hp)),
                    )
        if str(packet.transport).upper() == "UDP":
            for owner_record in self.entity_owner_evidence.observe_packet(
                packet.payload, packet.inferred_direction(), packet.timestamp_seconds,
                self._damage_object_packet_serial,
            ):
                confirmed = self._packet_entity_owner(owner_record.entity_id, packet.timestamp_seconds)
                if confirmed is not None:
                    self.summon_owners[owner_record.entity_id] = confirmed
        if (
            monster_spawn is not None
            and int(monster_spawn.template_id) in _CROSS_SCENE_PREBATTLE_SUMMON_SKILLS
        ):
            # A permanent companion can be born after its owner's class was
            # already proved, while its 34/2c owner row is delayed or absent.
            # Bind this exact observed generation immediately when exactly one
            # compatible player profile exists.  This is the same strict
            # evidence gate used by formal-capture handoff, not a recent-cast
            # or low actor-ID guess.
            prewarm_permanent_summon_lives(self, packet.timestamp_seconds)
        known_action_actor_ids = self.skill_actor_ids | self.local_actor_ids
        decoded = decode_v576_payload(
            packet.payload,
            packet.inferred_direction(),
            self.damage_skill_ids,
            self.action_skill_ids,
            # At capture start the local actor has not necessarily emitted a
            # full cast yet. An exact compact client action is sufficient to
            # bootstrap that identity; subsequent actions remain restricted
            # to the confirmed actor set and the normal job-tree checks.
            known_action_actor_ids or None,
            packet_features,
        )
        original_alternate_target = 0
        if decoded is not None:
            decoded = _canonicalize_live_message(decoded)
            original_target = decoded.target_actor_id
            # Keep the raw protocol's second actor before target canonicalizing
            # intentionally clears it for ordinary incoming/self-target forms.
            # This is diagnostic evidence only; attribution still uses the
            # selected target and the V2 proof chain.
            original_alternate_target = int(
                getattr(decoded, "alternate_target_actor_id", 0) or 0
            )
            decoded = canonicalize_udp_damage_identity(packet, decoded)
            identity_target_corrected = decoded.target_actor_id != original_target
            # Capture the OLD supplemental admission state before this hit
            # writes target history. It must not certify its own retry.
            corrected_target_was_known = (
                decoded.target_actor_id in self.monster_entities
                or decoded.target_actor_id in self.confirmed_damage_target_ids
                or decoded.target_actor_id in self.damage_target_counts
            )
            decoded = self._repair_proven_entity_target(packet, decoded)
            if decoded.kind == "damage_hit" and decoded.target_actor_id is not None:
                target_template = self.monster_entities.get(decoded.target_actor_id)
                if (
                    target_template is not None
                    and not target_template.boss
                    and 0 < target_template.hp <= 1_000
                    and int(decoded.damage or 0) >= 100_000
                    and self._boss_phase_serial
                    - self._monster_birth_phase.get(
                        decoded.target_actor_id, self._boss_phase_serial
                    ) >= 2
                    and decoded.target_actor_id not in self.summon_owners
                ):
                    # A short ID still labelled as a tiny scene object has
                    # become a high-damage target two Boss phases later. Its
                    # later birth was missed; the old template must not be
                    # carried into damage, skill or Boss attribution.
                    self.monster_entities.pop(decoded.target_actor_id, None)
                    self._monster_birth_phase.pop(decoded.target_actor_id, None)
        self.last_decoded_message = decoded
        if decoded is None:
            return None
        if (
            decoded.kind == "damage_hit"
            and decoded.alternate_target_actor_id is not None
            and decoded.alternate_target_actor_id in self.monster_entities
            and decoded.source_actor_id in self.monster_entities
            and self.monster_entities[decoded.source_actor_id].template_id
            in _SKILL_OBJECT_TEMPLATE_SKILL_IDS
        ):
            # A direct37 record is shared by ordinary short actor IDs and
            # spawned skill objects. Only the latter stores the struck Boss
            # immediately before the marker; keep both protocol candidates
            # and choose here, where the spawned template is known.
            decoded = replace(
                decoded,
                target_actor_id=decoded.alternate_target_actor_id,
            )
            self.last_decoded_message = decoded
        # Keep the selected physical record before presentation/legacy target
        # fallback can remap it. Incoming rows used to discard this entirely.
        wire_damage_evidence = (
            damage_wire_metadata(decoded, packet_payload=packet.payload if str(packet.transport).upper() == "UDP" else None, direction=packet.inferred_direction()) if decoded.kind == "damage_hit" else {}
        )
        if decoded.kind == "damage_hit" and original_alternate_target:
            wire_damage_evidence["wire_alternate_target_actor_id"] = (
                original_alternate_target
            )
        decoded = self._normalize_transient_damage_target(
            decoded, packet.timestamp_seconds
        )
        self.last_decoded_message = decoded
        if decoded.kind == "damage_hit":
            # The 06/27 entity birth datagram may be dropped while the exact
            # 34/2c owner row and a typed native summon attack both survive.
            # Recover that one generation before any legacy cast/profile
            # attribution runs. Shared attack keys deliberately fail closed.
            recover_missing_summon_life(self, packet, decoded)
        damage_target_recorded = False
        can_infer_summon = (
            decoded.kind in {"skill_cast", "skill_action"}
            and decoded.skill_id in self.skill_catalog
        )
        source_summon_owner = (
            self._infer_recent_summon_owner(
                decoded.source_actor_id, packet.timestamp_seconds
            )
            if decoded.source_actor_id is not None and can_infer_summon
            else None
        )
        if decoded.kind == "player_identity" and decoded.source_name:
            if (decoded.layout == "c2s-udp-player-identity-114-v576"
                    and packet.inferred_direction() == "client_to_server"):
                observe_local_character_identity(
                    self, decoded.source_name, packet.timestamp_seconds,
                    identity_evidence=f"{decoded.layout}:{sha256(packet.payload).hexdigest()}",
                )
                self.player_root_action_objects.observe_client_identity(decoded.source_name)
            self.local_name = decoded.source_name
        elif (
            decoded.kind == "skill_cast"
            and decoded.source_actor_id is not None
            and source_summon_owner is None
        ):
            self.local_actor_ids.add(decoded.source_actor_id)

        if (
            decoded.kind in {"skill_cast", "skill_action"}
            and decoded.source_actor_id is not None
            and decoded.skill_id is not None
            and source_summon_owner is None
            and decoded.source_actor_id not in self.monster_entities
        ):
            if self._action_conflicts_with_confirmed_job(
                decoded.source_actor_id, decoded.skill_id
            ):
                # Keep the packet available to the independent status decoder,
                # but do not let a coincidental skill integer corrupt the
                # actor's job evidence or create a cross-job timeline cast.
                self.last_decoded_message = None
                return None
            self.damage_object_origins.observe_action(
                decoded.source_actor_id,
                decoded.skill_id,
                packet.timestamp_seconds,
            )
            previous_profiles = frozenset(
                self.actor_skill_profiles.get(decoded.source_actor_id, ())
            )
            self._update_actor_job(decoded.source_actor_id, decoded.skill_id)
            self._observe_player_skill_activity(
                decoded.source_actor_id,
                decoded.skill_id,
                packet.timestamp_seconds,
            )
            current_profiles = frozenset(
                self.actor_skill_profiles.get(decoded.source_actor_id, ())
            )
            if (
                len(current_profiles) == 1
                and current_profiles != previous_profiles
            ):
                # The companion birth may have preceded the first
                # second-job-only action in an older capture.  Re-run the
                # permanent-singleton prewarmer only when profile evidence
                # actually narrows, avoiding per-packet scans and allowing
                # subsequent Hits to use the exact owner immediately.
                prewarm_permanent_summon_lives(
                    self, packet.timestamp_seconds
                )
            self.skill_actor_ids.add(decoded.source_actor_id)
            self.latest_actor_skills[decoded.source_actor_id] = (
                decoded.skill_id,
                packet.timestamp_seconds,
            )
            actor_casts = self.recent_actor_casts.setdefault(
                decoded.source_actor_id, []
            )
            if not actor_casts or not (
                actor_casts[-1][0] == decoded.skill_id
                and packet.timestamp_seconds - actor_casts[-1][1] <= 0.12
            ):
                actor_casts.append((decoded.skill_id, packet.timestamp_seconds))
            actor_cutoff = packet.timestamp_seconds - 15.0
            if len(actor_casts) > 48:
                self.recent_actor_casts[decoded.source_actor_id] = [
                    item for item in actor_casts if item[1] >= actor_cutoff
                ]
            casters = self.recent_skill_casters.setdefault(decoded.skill_id, [])
            casters.append((decoded.source_actor_id, packet.timestamp_seconds))
            cutoff = packet.timestamp_seconds - 120.0
            if len(casters) > 32:
                self.recent_skill_casters[decoded.skill_id] = [
                    item for item in casters if item[1] >= cutoff
                ]
            damage_casts = self.recent_damage_actor_casts.setdefault(
                decoded.source_actor_id, []
            )
            if decoded.layout != "c2s-udp-skill-input-46-v576":
                if not damage_casts or not (
                    damage_casts[-1][0] == decoded.skill_id
                    and packet.timestamp_seconds - damage_casts[-1][1]
                    <= 0.12
                ):
                    damage_casts.append(
                        (decoded.skill_id, packet.timestamp_seconds)
                    )
                if len(damage_casts) > 48:
                    self.recent_damage_actor_casts[
                        decoded.source_actor_id
                    ] = [
                        item
                        for item in damage_casts
                        if item[1] >= actor_cutoff
                    ]

        if decoded.kind == "damage_hit":
            target_was_confirmed = (
                decoded.layout not in _TRANSIENT_DAMAGE_TARGET_LAYOUTS
                or decoded.target_actor_id in self.confirmed_damage_target_ids
                or decoded.target_actor_id in self.monster_entities
            )
            damage_target_recorded = self._observe_damage_target(
                decoded.source_actor_id,
                decoded.target_actor_id,
                packet.timestamp_seconds,
                confirmed=target_was_confirmed,
            )

        if (
            decoded.kind != "damage_hit"
            or decoded.source_actor_id is None
            or decoded.damage is None
            or decoded.damage <= 0
            or decoded.damage > _MAX_REASONABLE_HIT_DAMAGE
            or (
                decoded.layout == "s2c-udp-hit-148-v576"
                and decoded.target_actor_id is not None
                and decoded.target_actor_id == decoded.source_actor_id
            )
        ):
            return None
        if (
            decoded.layout == "s2c-udp-hit-218-v576"
            and decoded.source_actor_id not in self.skill_actor_ids
            and decoded.source_actor_id not in self.local_actor_ids
            and decoded.source_actor_id not in self.monster_entities
            and decoded.source_actor_id not in self.summon_owners
        ):
            # The 218-byte state-sync packet shares the old fixed-hit prefix.
            # A genuine hit always comes from an actor that has already been
            # observed as a player, monster or summon.  Rejecting anonymous
            # sources prevents their counters from becoming enormous damage.
            return None
        if (
            decoded.layout
            == "s2c-udp-hit-effect-direct37-object-continuation-v576"
            and (
                decoded.source_actor_id not in self.monster_entities
                or decoded.target_actor_id not in self.monster_entities
            )
        ):
            # The structural signature also occurs in unrelated compound
            # messages. A real object continuation has both a previously
            # spawned skill object and a spawned monster target.
            # A verified physical target correction must not cause the old
            # supplemental record to become falsely "already consumed".
            # Retry only this context rejection, through the unchanged
            # supplemental owner/target/amount gates; never retry outliers,
            # invalid source/player targets or every un-emitted primary.
            self._retry_corrected_primary = identity_target_corrected and corrected_target_was_known
            return None
        exact_packet_skill = bool(getattr(decoded, "skill_id_exact", False))
        skill_id_value = decoded.skill_id
        exact_dungeon_special_skill = bool(
            exact_packet_skill
            and skill_id_value in _DUNGEON_SPECIAL_SKILL_NAMES
        )
        exact_basic_attack_key = bool(
            exact_packet_skill and skill_id_value == _NORMAL_ATTACK_SKILL_ID
        )
        # ``0xFFFFFFFF`` is the client's unowned/native attack-result sentinel,
        # not sufficient proof that the player pressed an ordinary attack.  It
        # is also used by skill continuations, transformed attacks and derived
        # combo hits whose concrete owner is carried by another action/object
        # record.  Treat it as a candidate that stronger lifecycle/timeline
        # evidence may resolve, rather than locking the row in the generic
        # ``普通/特殊攻击`` bucket before those resolvers run.
        exact_named_packet_skill = exact_packet_skill and not exact_basic_attack_key
        if exact_basic_attack_key:
            skill_id_value = None
        if not exact_named_packet_skill and self._is_ambiguous_dark_avenger_packet_skill(
            decoded.source_actor_id, skill_id_value
        ):
            skill_id_value = None
        packet_candidate_skill_ids = (
            []
            if exact_named_packet_skill
            else [
                skill_id
                for skill_id in decoded.candidate_skill_ids
                if skill_id in self.damage_skill_ids
                and skill_id in self.skill_catalog
                and not self._action_conflicts_with_confirmed_job(
                    decoded.source_actor_id, skill_id
                )
                and not self._is_ambiguous_dark_avenger_packet_skill(
                    decoded.source_actor_id, skill_id
                )
            ]
        )
        actor_profiles = self.actor_skill_profiles.get(decoded.source_actor_id)
        if actor_profiles:
            packet_candidate_skill_ids = [
                skill_id
                for skill_id in packet_candidate_skill_ids
                if not self.skill_profiles.get(skill_id)
                or not actor_profiles.isdisjoint(
                    self.skill_profiles.get(skill_id, frozenset())
                )
            ]
        packet_attribution_method = (
            "packet_dungeon_special_skill"
            if exact_dungeon_special_skill
            else "unresolved_native_attack_sentinel"
            if exact_basic_attack_key
            else "packet_skill_id"
        )
        if packet_candidate_skill_ids:
            packet_candidate_skill_ids = list(
                dict.fromkeys(packet_candidate_skill_ids)
            )
            recent_packet_candidates: list[tuple[float, int]] = []
            for candidate_skill_id in packet_candidate_skill_ids:
                for actor_id, cast_time in self.recent_skill_casters.get(
                    candidate_skill_id, []
                ):
                    age = packet.timestamp_seconds - cast_time
                    if (
                        actor_id == decoded.source_actor_id
                        and 0.0 <= age <= 15.0
                    ):
                        recent_packet_candidates.append(
                            (cast_time, candidate_skill_id)
                        )
            # Compound records can include several previous action/status IDs.
            # The newest matching cast is stronger evidence than byte order;
            # without cast context the protocol's end-biased ordering is the
            # deterministic fallback.
            if recent_packet_candidates:
                skill_id_value = max(recent_packet_candidates)[1]
                packet_attribution_method = (
                    "embedded_single_candidate"
                    if len(packet_candidate_skill_ids) == 1
                    else "compound_candidate_recent_cast"
                )
            elif actor_profiles:
                skill_id_value = packet_candidate_skill_ids[0]
                packet_attribution_method = (
                    "embedded_single_candidate"
                    if len(packet_candidate_skill_ids) == 1
                    else "compound_candidate_profile"
                )
            elif (
                skill_id_value not in packet_candidate_skill_ids
                or skill_id_value < 100
            ):
                # With no known actor profile or matching cast, a small value
                # embedded in a compound packet is usually a state counter.
                # Leave it unresolved until stronger timing/status evidence is
                # available instead of inventing damage for a basic skill.
                skill_id_value = None
            else:
                packet_attribution_method = (
                    "embedded_single_candidate"
                    if len(packet_candidate_skill_ids) == 1
                    else "compound_candidate_ranked"
                )
        appended_status_skill_id = decoded.appended_status_skill_id
        attribution_method = (
            "rejected_transformed_base_id"
            if not exact_named_packet_skill
            and decoded.skill_id is not None
            and self._is_ambiguous_dark_avenger_packet_skill(
                decoded.source_actor_id, decoded.skill_id
            )
            else packet_attribution_method
        )
        damage_object_attribution = None
        damage_object_candidate_skill_ids: frozenset[int] = frozenset()
        object_packet_context_skill_id = None
        periodic_damage_state: _ActivePeriodicDamage | None = None
        # Effect-bearing hit packets often append the skill ID that owns the
        # refreshed status. That ID is not necessarily the source of the
        # damage: periodic Burn ticks can refresh an unrelated ice status that
        # is already on the target. Treat it as status context and let the
        # stateful matcher identify only the first direct hit of a new cast.
        if skill_id_value is not None and not exact_named_packet_skill:
            payload_statuses = decode_v576_status_events(
                packet.payload,
                packet.inferred_direction(),
                self.status_known_skill_ids,
            )
            if any(
                _canonical_live_skill_id(status.skill_id) == skill_id_value
                for status in payload_statuses
            ):
                appended_status_skill_id = skill_id_value
                skill_id_value = None
                attribution_method = "appended_status_skill"
        if (
            skill_id_value is not None
            and skill_id_value != _NORMAL_ATTACK_SKILL_ID
            and skill_id_value in self.skill_catalog
            and skill_id_value not in self.damage_skill_ids
        ):
            # A compound damage packet may contain an unrelated integer that
            # equals a utility skill from the same class tree. It is valid as
            # a cast ID but cannot own damage; continue with cast/timing based
            # attribution instead.
            skill_id_value = None
            attribution_method = "rejected_non_damage_id"
        if (
            skill_id_value is not None
            and skill_id_value != _NORMAL_ATTACK_SKILL_ID
            and not exact_named_packet_skill
        ):
            actor_profiles = self.actor_skill_profiles.get(decoded.source_actor_id)
            skill_profiles = self.skill_profiles.get(skill_id_value, frozenset())
            if (
                actor_profiles
                and skill_profiles
                and actor_profiles.isdisjoint(skill_profiles)
            ):
                skill_id_value = None
                attribution_method = "rejected_cross_job_id"
        if (
            skill_id_value in _CAST_REQUIRED_PACKET_SKILL_IDS
            and not exact_named_packet_skill
        ):
            corroborating_cast = any(
                actor_id == decoded.source_actor_id
                and 0.0 <= packet.timestamp_seconds - cast_time <= 8.0
                for actor_id, cast_time in self.recent_skill_casters.get(
                    skill_id_value, []
                )
            )
            if not corroborating_cast:
                skill_id_value = None
                attribution_method = "rejected_uncorroborated_packet_skill"
        if (
            skill_id_value is not None
            and skill_id_value != _NORMAL_ATTACK_SKILL_ID
            and skill_id_value not in _DUNGEON_SPECIAL_SKILL_NAMES
            and skill_id_value not in self.skill_catalog
        ):
            skill_id_value = None
        if "direct37" in decoded.layout:
            # Every direct37 row is offered to the exact object chain first.
            # A plausible embedded skill integer must never bypass a proven
            # Projectile-template + damage_object_id join.
            damage_object_attribution = (
                self.damage_object_origins.attribute_direct37(
                    source_actor_id=decoded.source_actor_id,
                    timestamp=packet.timestamp_seconds,
                    layout=decoded.layout,
                    payload=packet.payload,
                    direction=packet.inferred_direction(),
                    damage=decoded.damage,
                    record_offset=getattr(decoded, "record_offset", 0),
                    packet_serial=self._damage_object_packet_serial,
                )
            )
            if damage_object_attribution is None:
                damage_object_candidate_skill_ids = (
                    self.damage_object_origins.direct37_candidate_skill_ids(
                        source_actor_id=decoded.source_actor_id,
                        timestamp=packet.timestamp_seconds,
                        layout=decoded.layout,
                        payload=packet.payload,
                        direction=packet.inferred_direction(),
                        damage=decoded.damage,
                        record_offset=getattr(decoded, "record_offset", 0),
                        packet_serial=self._damage_object_packet_serial,
                    )
                )
            if (
                damage_object_attribution is not None
                and self._action_conflicts_with_confirmed_job(
                    damage_object_attribution.owner_actor_id,
                    damage_object_attribution.skill_id,
                )
            ):
                # A typed client object can still be an internal equipment or
                # transformation effect rather than a player-skill foreign
                # key.  Never let it cross a confirmed profession boundary.
                self.damage_object_skill_conflicts += 1
                self.damage_object_origins.invalidate_origin(
                    source_actor_id=damage_object_attribution.owner_actor_id,
                    damage_object_id=damage_object_attribution.damage_object_id,
                    timestamp=packet.timestamp_seconds,
                    packet_serial=self._damage_object_packet_serial,
                    record_offset=max(
                        1, getattr(decoded, "record_offset", 0) + 1
                    ),
                )
                damage_object_attribution = None
            if (
                damage_object_attribution is not None
                and exact_named_packet_skill
                and skill_id_value is not None
                and _canonical_live_skill_id(skill_id_value)
                != _canonical_live_skill_id(
                    damage_object_attribution.skill_id
                )
            ):
                # Concurrent attacks can carry a different skill context in
                # the fixed result field: captured Ice Injection child objects
                # retain their exact creator/parent chain while a Firebird cast
                # supplies this field. Do not destroy that independently
                # action-backed, full-ID chain. Static templates or nearest-
                # parent guesses alone are NOT sufficient to override it.
                self.damage_object_skill_conflicts += 1
                if (
                    damage_object_attribution.parent_action_lineage_validated
                    or damage_object_attribution.exact_parent_child_skill_validated
                ):
                    object_packet_context_skill_id = int(decoded.skill_id)
                else:
                    self.damage_object_origins.invalidate_origin(
                        source_actor_id=damage_object_attribution.owner_actor_id,
                        damage_object_id=damage_object_attribution.damage_object_id,
                        timestamp=packet.timestamp_seconds,
                        packet_serial=self._damage_object_packet_serial,
                        record_offset=max(
                            1, getattr(decoded, "record_offset", 0) + 1
                        ),
                    )
                    damage_object_attribution = None
            if (
                damage_object_attribution is not None
                and exact_named_packet_skill
                and skill_id_value is not None
                and damage_object_attribution.packet_skill_is_shared_child_context_only(
                    _canonical_live_skill_id(skill_id_value)
                )
            ):
                # A shared child object may be reported while Firebird/Flame
                # Road is the packet's current skill context.  Preserve that
                # context for auditing, but do not promote it to deterministic
                # damage identity until the full parent chain is available.
                object_packet_context_skill_id = int(decoded.skill_id)
            if damage_object_attribution is not None and (
                object_packet_context_skill_id is not None
                or should_prefer_damage_object_skill(
                packet_skill_exact=(
                    exact_named_packet_skill and skill_id_value is not None
                ),
                object_mapping_packet_validated=(
                    damage_object_attribution.deterministically_validated
                ),
                )
            ):
                skill_id_value = damage_object_attribution.skill_id
                attribution_method = (
                    "damage_object_projectile_link"
                    if damage_object_attribution.deterministically_validated
                    else "damage_object_projectile_link_provisional"
                )
            elif (
                skill_id_value in DAMAGE_OBJECT_SKILL_IDS
                and not exact_named_packet_skill
            ):
                # Object-backed skills are authoritative only after their
                # exact runtime object resolves. Continue through the lower
                # evidence tiers instead of accepting a scanned/embedded
                # integer. A fixed-slot wire skill remains authoritative even
                # when the configured object template disagrees with it.
                skill_id_value = None
                attribution_method = "awaiting_damage_object_projectile_link"
        if decoded.periodic_kind and should_replace_attribution(
            attribution_method, "periodic_status_packet"
        ):
            periodic_damage_state = self._periodic_damage_candidate(
                decoded.source_actor_id,
                decoded.target_actor_id,
                packet.timestamp_seconds,
                appended_status_skill_id,
            )
            if periodic_damage_state is None:
                # Capturing may start while a pre-existing Burn is already
                # ticking, before its original status packet was observed.
                periodic_damage_state = _ActivePeriodicDamage(
                    source_actor_id=decoded.source_actor_id,
                    target_actor_id=decoded.target_actor_id or 0,
                    raw_skill_id=0,
                    source_skill_id=0,
                    source_skill_name="",
                    kind=decoded.periodic_kind,
                    label=decoded.periodic_label or decoded.periodic_kind,
                    effect_code="",
                    applied_at=packet.timestamp_seconds,
                    expires_at=packet.timestamp_seconds,
                )
            skill_id_value = periodic_damage_state.source_skill_id
            attribution_method = "periodic_status_packet"
        skill_object_owner = self._skill_object_owner(
            decoded.source_actor_id,
            decoded.target_actor_id,
            skill_id_value,
            packet.timestamp_seconds,
        )
        summon_owner = skill_object_owner
        if summon_owner is None:
            explicit_summon_owner = self._summon_owner_from_explicit_skill(
                decoded.source_actor_id,
                skill_id_value,
                packet.timestamp_seconds,
            )
            existing_summon_owner = self._cached_summon_owner(
                decoded.source_actor_id, packet.timestamp_seconds
            )
            if (
                skill_id_value is not None
                and existing_summon_owner is not None
                and _LIVE_DAMAGE_SKILL_ALIASES.get(
                    skill_id_value, skill_id_value
                )
                != existing_summon_owner[1]
            ):
                existing_summon_owner = None
            summon_owner = explicit_summon_owner or existing_summon_owner
            if (
                summon_owner is None
                and skill_id_value is None
                and decoded.target_actor_id is not None
                and decoded.target_actor_id <= _SUMMON_ACTOR_ID_MAX
            ):
                summon_owner = self._infer_recent_summon_owner(
                    decoded.source_actor_id,
                    packet.timestamp_seconds,
                )
        if (
            summon_owner is not None
            and periodic_damage_state is None
            and should_replace_attribution(
                attribution_method,
                (
                    "skill_object_owner"
                    if (
                        skill_object_owner is not None
                        and skill_object_owner[1]
                        not in self.summon_creation_skill_ids
                    )
                    else "summon_owner"
                ),
            )
        ):
            # A clone can carry the ID of the attack it copied.  Its damage is
            # still produced by the summoning skill and belongs in that row.
            skill_id_value = summon_owner[1]
            attribution_method = (
                "skill_object_owner"
                if (
                    skill_object_owner is not None
                    and skill_object_owner[1]
                    not in self.summon_creation_skill_ids
                )
                else "summon_owner"
            )
        if skill_id_value is None and summon_owner is not None:
            skill_id_value = summon_owner[1]
            attribution_method = "summon_owner"
        if (
            skill_id_value is None
            and appended_status_skill_id is not None
            and appended_status_skill_id not in DAMAGE_OBJECT_SKILL_IDS
        ):
            status_skill_id = appended_status_skill_id
            status_casts = [
                item
                for item in self.recent_skill_casters.get(status_skill_id, [])
                if item[0] == decoded.source_actor_id
                and 0.0 <= packet.timestamp_seconds - item[1] <= 4.0
            ]
            latest_status_cast = max(status_casts, key=lambda item: item[1]) if status_casts else None
            previous_damage = self.latest_skill_damage.get(
                (decoded.source_actor_id, status_skill_id),
                float("-inf"),
            )
            if latest_status_cast is not None and previous_damage < latest_status_cast[1]:
                skill_id_value = status_skill_id
                attribution_method = "appended_status_first_hit"
        if skill_id_value is None:
            continuation_skill_id = self._continuation_damage_candidate(
                decoded.source_actor_id,
                packet.timestamp_seconds,
                packet,
                profiled_only=True,
            )
            if (
                continuation_skill_id is not None
                and not self._is_ambiguous_dark_avenger_packet_skill(
                    decoded.source_actor_id, continuation_skill_id
                )
            ):
                skill_id_value = continuation_skill_id
                attribution_method = "continuation_window"
        if skill_id_value is None:
            aura_skill_id = self._refreshable_pulsing_aura_candidate(
                decoded.source_actor_id,
                decoded.target_actor_id,
                packet.timestamp_seconds,
                decoded.layout,
            )
            if aura_skill_id is not None:
                skill_id_value = aura_skill_id
                attribution_method = "refreshable_pulsing_aura"
        if skill_id_value is None:
            # Unprofiled continuation windows remain a final display fallback,
            # but no longer steal a mechanically bounded persistent pulse.
            continuation_skill_id = self._continuation_damage_candidate(
                decoded.source_actor_id,
                packet.timestamp_seconds,
                packet,
            )
            if (
                continuation_skill_id is not None
                and not self._is_ambiguous_dark_avenger_packet_skill(
                    decoded.source_actor_id, continuation_skill_id
                )
            ):
                skill_id_value = continuation_skill_id
                attribution_method = "continuation_window"
        if skill_id_value is None:
            learned_skill_id = self._learned_damage_candidate(
                decoded.source_actor_id,
                packet.timestamp_seconds,
                self._damage_signature(packet, decoded.layout),
            )
            if (
                learned_skill_id is not None
                and learned_skill_id
                not in DAMAGE_OBJECT_SKILL_IDS
                and not self._is_ambiguous_dark_avenger_packet_skill(
                    decoded.source_actor_id, learned_skill_id
                )
            ):
                skill_id_value = learned_skill_id
                attribution_method = "learned_cast_profile"
        if skill_id_value is None:
            periodic_damage_state = self._periodic_damage_candidate(
                decoded.source_actor_id,
                decoded.target_actor_id,
                packet.timestamp_seconds,
                appended_status_skill_id,
            )
            if periodic_damage_state is not None:
                skill_id_value = periodic_damage_state.source_skill_id
                attribution_method = "periodic_status"
        if skill_id_value is None and actor_profiles:
            # Once a teammate's class tree is known, an otherwise untagged
            # direct hit can safely follow the newest damaging action from
            # that same tree.  This covers compact combo/projectile records
            # that carry only low-valued state counters.  Periodic, summon and
            # learned-profile attribution have already had priority above.
            for cast_skill_id, cast_time in reversed(
                self._damage_casts_for_actor(decoded.source_actor_id)
            ):
                age = packet.timestamp_seconds - cast_time
                if age < 0:
                    continue
                if age > 3.0:
                    break
                cast_profiles = self.skill_profiles.get(
                    cast_skill_id, frozenset()
                )
                if (
                    cast_skill_id in self.damage_skill_ids
                    and cast_skill_id
                    not in DAMAGE_OBJECT_SKILL_IDS
                    and (
                        cast_skill_id != 5207
                        or self.field_effect_instances.active_generation(
                            decoded.source_actor_id,
                            cast_skill_id,
                            packet.timestamp_seconds,
                            expiry_grace_seconds=0.5,
                        )
                        is not None
                    )
                    and not self._is_ambiguous_dark_avenger_packet_skill(
                        decoded.source_actor_id, cast_skill_id
                    )
                    and (
                        not cast_profiles
                        or not actor_profiles.isdisjoint(cast_profiles)
                    )
                ):
                    skill_id_value = cast_skill_id
                    attribution_method = "latest_profiled_direct_cast"
                    break
        if skill_id_value is None:
            # Untagged server hit records must not be handed to the last button
            # pressed when several skills overlap. That shortcut is correct in
            # a single-skill test but silently steals delayed and periodic hits
            # in a real rotation. Only a single unambiguous active cast may own
            # the record; otherwise keep the damage in an explicit unresolved
            # bucket so totals stay intact without corrupting skill shares.
            recent_casts = [
                item
                for item in self._damage_casts_for_actor(
                    decoded.source_actor_id
                )
                if item[0] in self.damage_skill_ids
                if item[0] not in DAMAGE_OBJECT_SKILL_IDS
                if (
                    item[0] != 5207
                    or self.field_effect_instances.active_generation(
                        decoded.source_actor_id,
                        item[0],
                        packet.timestamp_seconds,
                        expiry_grace_seconds=0.5,
                    )
                    is not None
                )
                if not self._is_ambiguous_dark_avenger_packet_skill(
                    decoded.source_actor_id, item[0]
                )
                if 0.0 <= packet.timestamp_seconds - item[1] <= 4.0
            ]
            unique_recent_skills = {item[0] for item in recent_casts}
            if len(unique_recent_skills) == 1:
                skill_id_value = next(iter(unique_recent_skills))
                attribution_method = "unique_active_cast"
            else:
                periodic_damage_state = self._periodic_cast_damage_fallback(
                    decoded.source_actor_id,
                    decoded.target_actor_id,
                    packet.timestamp_seconds,
                    packet,
                )
                if periodic_damage_state is not None:
                    skill_id_value = periodic_damage_state.source_skill_id
                    attribution_method = "periodic_cast_fallback"
                else:
                    sustained_skill_id = (
                        self._sustained_cast_damage_candidate(
                            decoded.source_actor_id,
                            packet.timestamp_seconds,
                            packet,
                        )
                    )
                    if sustained_skill_id is not None:
                        skill_id_value = sustained_skill_id
                        attribution_method = "sustained_cast_window"
                    else:
                        # Silence after a recent cast is not an attack
                        # identity. Basic/special attacks need either a fixed
                        # packet attack key or a reviewed stable action+object
                        # signature; otherwise the factual hit stays unknown.
                        skill_id_value = _UNRESOLVED_SKILL_ID
                        attribution_method = "unresolved"

        if summon_owner is None:
            skill_object_owner = self._skill_object_owner(
                decoded.source_actor_id,
                decoded.target_actor_id,
                skill_id_value,
                packet.timestamp_seconds,
            )
            if skill_object_owner is not None:
                summon_owner = skill_object_owner
                if should_replace_attribution(
                    attribution_method, "skill_object_owner"
                ):
                    skill_id_value = skill_object_owner[1]
                    attribution_method = "skill_object_owner"

        if summon_owner is not None:
            owner_method = (
                "skill_object_owner"
                if skill_object_owner is not None
                and skill_object_owner[1] not in self.summon_creation_skill_ids
                else "summon_owner"
            )
            if should_replace_attribution(attribution_method, owner_method):
                skill_id_value = summon_owner[1]
                attribution_method = owner_method

        skill_id_value = _LIVE_DAMAGE_SKILL_ALIASES.get(
            skill_id_value, skill_id_value
        )
        # Reproduce the client meter's bounded per-result key read.  The key
        # belongs to this exact physical row; it is not cached or extended to
        # later hits.  Reviewed dungeon-special keys are inspected for every
        # physical row because their source is encounter-owned rather than a
        # player profession.  Ordinary native skill keys still require a
        # proven player source and the normal profession gates.
        player_source_proven = (
            decoded.source_actor_id not in self.monster_entities
            and decoded.source_actor_id in (
                self.skill_actor_ids
                | self.local_actor_ids
                | set(self.known_actor_names)
            )
        )
        native_basic_attack_key_seen = exact_basic_attack_key
        if not exact_named_packet_skill:
            native_skill = resolve_native_damage_skill_key(
                packet.payload,
                packet.inferred_direction(),
                wire_layout=decoded.layout,
                record_offset=getattr(decoded, "record_offset", 0),
                source_actor_id=decoded.source_actor_id,
                damage=decoded.damage,
                critical=bool(decoded.critical),
                skill_catalog=self.skill_catalog,
                damage_skill_ids=self.damage_skill_ids,
                live_aliases=_LIVE_DAMAGE_SKILL_ALIASES,
                external_damage_skill_ids={
                    *_DUNGEON_SPECIAL_SKILL_NAMES,
                    _NORMAL_ATTACK_SKILL_ID,
                },
            )
            native_is_dungeon_special = bool(
                native_skill is not None
                and native_skill.skill_id in _DUNGEON_SPECIAL_SKILL_NAMES
            )
            native_is_basic_attack = bool(
                native_skill is not None
                and native_skill.skill_id == _NORMAL_ATTACK_SKILL_ID
                and player_source_proven
            )
            native_primary_key = bool(native_skill is not None
                                      and native_skill.field == "primary90"
                                      and not decoded.periodic_kind)
            native_is_player_skill = bool(
                native_skill is not None
                and player_source_proven
                and should_replace_attribution(
                    attribution_method, "packet_native_attack_key",
                    native_primary_key=native_primary_key,
                )
                and not self._action_conflicts_with_confirmed_job(
                    decoded.source_actor_id, native_skill.skill_id
                )
            )
            if native_is_dungeon_special and should_replace_attribution(
                attribution_method, "packet_dungeon_special_skill",
                native_primary_key=native_primary_key,
            ):
                skill_id_value = native_skill.skill_id
                attribution_method = "packet_dungeon_special_skill"
                periodic_damage_state = None
            elif native_is_basic_attack and should_replace_attribution(
                attribution_method, "packet_basic_attack_key"
            ):
                # The fixed native key alone says only that this result has no
                # player-skill RowId.  Keep any object/entity/action/timeline
                # owner already found; otherwise leave the row unresolved for
                # the strict offline timing matcher.  A reviewed stable basic-
                # attack object signature still uses its independent object
                # evidence route and is unaffected by this gate.
                native_basic_attack_key_seen = True
            elif native_is_player_skill:
                skill_id_value = native_skill.skill_id
                attribution_method = "packet_native_attack_key"
                # A target having a periodic status is an inferred fallback,
                # not this result's attack identity. The client selects a
                # positive +90 key before consulting its object/status state.
                # Retaining this candidate would overwrite the exact key
                # again when the display bucket is constructed below.
                periodic_damage_state = None
        entity_evidence = {}
        if summon_owner is not None and attribution_method in {
            "summon_owner", "skill_object_owner", "supplemental_summon_owner"
        }:
            entity_evidence = self._entity_damage_evidence(
                decoded.source_actor_id, packet.timestamp_seconds, summon_owner[0], skill_id_value
            )
            if entity_evidence:
                attribution_method = "packet_entity_template_owner"
        if summon_owner is not None and not entity_evidence:
            entity_evidence = self._entity_wire_context(decoded.source_actor_id)
        inferred_entity_hit = bool(
            periodic_damage_state is not None and not decoded.periodic_kind
            and attribution_method == "periodic_status"
            and has_inferred_status_object_hit(
                SimpleNamespace(amount=decoded.damage, critical=bool(decoded.critical),
                                healing=False, attribution_method=attribution_method,
                                has_real_hit_evidence=False, **wire_damage_evidence),
                packet.payload, packet.inferred_direction(),
            )
        )
        if (((periodic_damage_state is None
              and _has_real_hit_evidence(decoded.layout, attribution_method))
             or inferred_entity_hit)
                and should_replace_attribution(
                    attribution_method, "packet_entity_self_attack",
                    physical_hit_proven=inferred_entity_hit)):
            self_attack = self._entity_self_attack_evidence(packet, decoded)
            if self_attack is not None:
                owner, skill_id_value, entity_evidence = self_attack
                summon_owner = (owner, skill_id_value)
                attribution_method = "packet_entity_self_attack"
                periodic_damage_state = None
                damage_object_attribution = None
                object_packet_context_skill_id = None
        if (
            damage_object_candidate_skill_ids
            and attribution_layer(attribution_method) is AttributionLayer.INFERENCE
            and skill_id_value is not None
            and skill_id_value > 0
            and _canonical_live_skill_id(skill_id_value)
            not in {
                _canonical_live_skill_id(candidate)
                for candidate in damage_object_candidate_skill_ids
            }
        ):
            # A live configured object generation is insufficient to name a
            # shared child without its required parent proof, but it is strong
            # negative evidence against unrelated cast-time guesses. Preserve
            # the factual damage in the unresolved bucket instead of letting
            # continuation/latest-cast rules corrupt the skill composition.
            skill_id_value = _UNRESOLVED_SKILL_ID
            attribution_method = "object_candidate_skill_conflict"
        if self._is_ambiguous_dark_avenger_packet_skill(
            decoded.source_actor_id, skill_id_value
        ):
            skill_id_value = _UNRESOLVED_SKILL_ID
            attribution_method = "unresolved_transformed_base_id"

        direct_hit_limit = _DIRECT_HIT_LIMITS_PER_TARGET.get(skill_id_value)
        direct_hit_overflow_skill_id: int | None = None
        direct_hit_overflow_method = ""
        if (
            direct_hit_limit is not None
            and summon_owner is None
            and periodic_damage_state is None
            and attribution_method == "latest_profiled_direct_cast"
            and decoded.target_actor_id is not None
        ):
            cast_time = self._latest_cast_time(
                decoded.source_actor_id,
                skill_id_value,
                packet.timestamp_seconds,
            )
            if cast_time is not None:
                usage_key = (
                    decoded.source_actor_id,
                    skill_id_value,
                    cast_time,
                    decoded.target_actor_id,
                )
                self.direct_hit_profile_usage[usage_key] += 1
                if self.direct_hit_profile_usage[usage_key] > direct_hit_limit:
                    direct_hit_overflow_skill_id = skill_id_value
                    direct_hit_overflow_method = attribution_method
                    skill_id_value = 0
                    attribution_method = "direct_hit_overflow"

        if skill_id_value > 0 and periodic_damage_state is None:
            self.latest_skill_damage[
                (decoded.source_actor_id, skill_id_value)
            ] = packet.timestamp_seconds

        exact_entity_owner_id = (
            self._current_entity_owner_id(
                decoded.source_actor_id,
                packet.timestamp_seconds,
            )
            if summon_owner is None
            else None
        )
        attribution_actor_id = (
            summon_owner[0]
            if summon_owner is not None
            else exact_entity_owner_id
            if exact_entity_owner_id is not None
            else decoded.source_actor_id
        )
        source_template = self.monster_entities.get(decoded.source_actor_id)
        profile_restricted_skill_object = bool(
            source_template is not None
            and source_template.template_id
            in _SKILL_OBJECT_TEMPLATE_OWNER_PROFILES
        )
        if (
            summon_owner is not None
            and decoded.target_actor_id == attribution_actor_id
        ):
            bundled_enemy_target = any(
                embedded.source_actor_id == decoded.source_actor_id
                and embedded.damage == decoded.damage
                and embedded.target_actor_id is not None
                and embedded.target_actor_id != attribution_actor_id
                for embedded in decode_v576_embedded_direct_damage_events(
                    packet.payload,
                    packet.inferred_direction(),
                    self.damage_skill_ids,
                )
            )
            if profile_restricted_skill_object and bundled_enemy_target:
                # This wire form stores the owner in the primary target slot
                # and the factual enemy in its bundled direct-hit record.  Do
                # not invent another enemy from recent-target history; the
                # bundled pass will emit the one packet-backed hit below.
                return None
            # Some player-created damage objects put their owner in the target
            # field. Once ownership is known, resolve that self-looking record
            # to the same reliable enemy the player was already attacking.
            resolved_target = self._recent_confirmed_damage_target(
                attribution_actor_id, packet.timestamp_seconds
            )
            if resolved_target is not None:
                decoded = replace(
                    decoded,
                    target_actor_id=resolved_target,
                    alternate_target_actor_id=(
                        decoded.alternate_target_actor_id
                        or attribution_actor_id
                    ),
                )
                self.last_decoded_message = decoded
                if not damage_target_recorded:
                    damage_target_recorded = self._observe_damage_target(
                        attribution_actor_id,
                        resolved_target,
                        packet.timestamp_seconds,
                        confirmed=True,
                    )
        player_actors = (
            self.skill_actor_ids
            | self.local_actor_ids
            | set(self.known_actor_names)
        ) - self.monster_entities.keys()
        if (
            profile_restricted_skill_object
            and summon_owner is not None
            and decoded.target_actor_id in player_actors
        ):
            # A player-owned object cannot damage a party member in the PvE
            # ledger.  In this packet form the player-sized primary target is
            # object/owner context; the factual enemy target, when present,
            # is decoded by the bundled-direct pass below.  Dropping this
            # primary interpretation avoids both a fake survival hit and an
            # outgoing event whose target is another player.
            return None
        incoming_target_actor_id = (
            decoded.target_actor_id
            if decoded.target_actor_id in player_actors
            else decoded.alternate_target_actor_id
            if decoded.source_actor_id not in player_actors
            and decoded.alternate_target_actor_id in player_actors
            # Player-created monster actors put their owner in the alternate
            # target field of otherwise ordinary outgoing-hit packets.  Once
            # the object owner is resolved, that field is ownership metadata,
            # not proof that the player was struck.  Treating it as incoming
            # damage both created a false survival hit and made the companion
            # damage depend on whether the bundled decoder happened to emit a
            # duplicate copy of the same record.
            and not (
                profile_restricted_skill_object
                and summon_owner is not None
            )
            else None
        )
        if incoming_target_actor_id is not None:
            # Player-target records never enter the outgoing-damage ledger.
            # Preserve only the proven direct-hit layout as separate incoming
            # evidence for survival discipline scoring. Some direct37 records
            # carry a transient hit object in the primary target field and the
            # struck player in the alternate target field.
            if (
                decoded.layout in _INCOMING_DAMAGE_LAYOUTS
                and decoded.source_actor_id not in player_actors
                and decoded.damage > 0
            ):
                target_name, target_job = self._actor_display_context(
                    incoming_target_actor_id
                )
                origin = self.first_timestamp or packet.timestamp_seconds
                return CombatEvent(
                    timestamp_ms=max(
                        0,
                        round((packet.timestamp_seconds - origin) * 1000),
                    ),
                    source_id="__incoming_damage__",
                    source_name="Boss/环境",
                    job="",
                    skill_id="incoming_damage",
                    skill_name="受到伤害",
                    amount=decoded.damage,
                    target_id=f"0x{incoming_target_actor_id:08X}",
                    target_name=target_name,
                    target_job=target_job,
                    attribution_method="incoming_damage",
                    raw_attribution_method=decoded.layout,
                    has_real_hit_evidence=_has_real_hit_evidence(
                        decoded.layout,
                        "incoming_damage",
                    ),
                    **wire_damage_evidence,
                )
            return None
        if self._reject_extreme_damage_outlier(
            decoded.source_actor_id,
            decoded.damage,
        ):
            return None
        field_effect_generation = None
        if (
            skill_id_value == 5207
            and attribution_layer(attribution_method)
            is AttributionLayer.INFERENCE
            and damage_object_attribution is None
            and not damage_object_candidate_skill_ids
        ):
            field_effect_generation = (
                self.field_effect_instances.pulse_generation(
                    decoded.source_actor_id,
                    skill_id_value,
                    packet.timestamp_seconds,
                    # Packet delivery can trail the declared status lifetime,
                    # but the ACT phase must still match below.
                    expiry_grace_seconds=0.5,
                )
            )
            if field_effect_generation is not None:
                attribution_method = "field_effect_status_phase"
            else:
                # Do not retain the former whole-duration timing guess.  The
                # factual Hit remains present, but its skill stays unknown
                # until another object/state/direct key proves ownership.
                skill_id_value = _UNRESOLVED_SKILL_ID
                attribution_method = "unresolved_field_effect_phase"
        if (
            skill_id_value > 0
            and skill_id_value != _NORMAL_ATTACK_SKILL_ID
            and summon_owner is None
            and periodic_damage_state is None
        ):
            self._learn_damage_timing(
                attribution_actor_id,
                skill_id_value,
                packet.timestamp_seconds,
                self._damage_signature(packet, decoded.layout),
                attribution_method,
            )

        unowned_short_entity = bool(
            summon_owner is None
            and exact_entity_owner_id is None
            and 0 < decoded.source_actor_id <= _SUMMON_ACTOR_ID_MAX
            and decoded.source_actor_id != self.boss_actor_id
            and decoded.source_actor_id not in player_actors
        )
        if unowned_short_entity:
            source_id = f"__unowned_entity__:0x{decoded.source_actor_id:08X}"
            source_name, source_job = (
                f"未归属召唤/实体 0x{decoded.source_actor_id:08X}",
                "",
            )
        else:
            source_id = f"0x{attribution_actor_id:08X}"
            source_name, source_job = self._actor_display_context(attribution_actor_id)
        origin = self.first_timestamp or packet.timestamp_seconds
        if periodic_damage_state is not None:
            skill_id = f"periodic:{periodic_damage_state.kind}"
            skill_name = periodic_damage_state.label
        elif attribution_method == "direct_hit_overflow":
            skill_id = "supplemental:status_damage"
            skill_name = "额外伤害"
        else:
            skill_id = str(skill_id_value)
            skill_name = (
                _UNRESOLVED_SKILL_NAME
                if skill_id_value == _UNRESOLVED_SKILL_ID
                else "弩系普通攻击"
                if (
                    skill_id_value == _NORMAL_ATTACK_SKILL_ID
                    and damage_object_attribution is not None
                    and damage_object_attribution.projectile_template_id == 92
                )
                else _skill_display_name(self.skill_catalog, skill_id_value)
            )
        target_name, target_job = self._monster_display_context(
            decoded.target_actor_id
        )
        return self._finalize_wire_damage_owner(CombatEvent(
            timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
            source_id=source_id,
            source_name=source_name,
            job=source_job,
            skill_id=skill_id,
            skill_name=skill_name,
            amount=decoded.damage,
            critical=bool(decoded.critical),
            target_id=(
                self._monster_event_id(decoded.target_actor_id)
                if decoded.target_actor_id is not None
                else ""
            ),
            target_name=target_name,
            attribution_method=attribution_method,
            target_job=target_job,
            target_max_hp=self._monster_max_hp(decoded.target_actor_id),
            raw_skill_id=(
                str(direct_hit_overflow_skill_id)
                if direct_hit_overflow_skill_id is not None
                else str(_NORMAL_ATTACK_SKILL_ID)
                if native_basic_attack_key_seen
                else ""
            ),
            raw_skill_name=(
                _skill_display_name(
                    self.skill_catalog,
                    direct_hit_overflow_skill_id,
                )
                if direct_hit_overflow_skill_id is not None
                else _NORMAL_ATTACK_SKILL_NAME
                if native_basic_attack_key_seen
                else ""
            ),
            # Preserve the wire disagreement without placing a concurrent
            # skill in raw_skill_id (used by proc/glyph inference).
            raw_attribution_method=(
                direct_hit_overflow_method
                or (
                    "native_attack_result_sentinel"
                    if native_basic_attack_key_seen else ""
                )
                or (
                    f"packet_skill_context:{object_packet_context_skill_id}"
                    if object_packet_context_skill_id is not None else ""
                )
            ),
            cast_instance_id=(
                (
                    f"damage-object:"
                            f"{damage_object_attribution.owner_actor_id:08X}:"
                            f"{damage_object_attribution.damage_object_id}:"
                            f"{damage_object_attribution.generation}"
                )
                if damage_object_attribution is not None
                else field_effect_generation.evidence_id
                if field_effect_generation is not None
                else ""
            ),
            attribution_confidence=(
                damage_object_attribution.confidence
                if damage_object_attribution is not None
                else 0.98
                if field_effect_generation is not None
                else 0.0
            ),
            has_real_hit_evidence=_has_real_hit_evidence(
                decoded.layout,
                attribution_method,
            ),
            **wire_damage_evidence,
            **entity_evidence,
        ), packet)

    def feed_prebattle_events(self, packet: PacketRecord) -> list[LiveDpsEvent]:
        """Idle-only state observer: no aggregation, trace writes or hit history.

        Use the reviewed combat state parser for state-bearing UDP messages;
        skip the overwhelmingly more frequent pure movement/damage packets.
        This observer is suspended as soon as formal capture starts.
        """
        if packet.transport.upper() == "TCP":
            return self.feed_identity_profile_events(packet)
        features = scan_v576_packet_features(
            packet.payload, packet.inferred_direction(), packet.transport,
        )
        # Fixed client cast layouts (e.g. 45 bytes) do not carry the variable
        # action marker. They still establish local identity/permanent auras.
        client_cast = packet.inferred_direction() == "client_to_server" and bool(features & V576_PACKET_PRIMARY)
        entity_birth = decode_v576_monster_spawn(packet.payload, packet.inferred_direction(), None) is not None
        if not client_cast and not entity_birth and not features & (V576_PACKET_STATUS | V576_PACKET_STATUS_END | V576_PACKET_ACTION | V576_PACKET_HEALTH | V576_PACKET_LIFECYCLE):
            return []
        return [event for event in self.feed_events(packet) if isinstance(
            event, (BossStatusEvent, PlayerHealthEvent, PlayerLifecycleEvent, BossActionEvent),
        )]

    def feed_identity_profile_events(
        self, packet: PacketRecord
    ) -> list[LiveDpsEvent]:
        """Decode only identity, inspect-profile and scene-boundary TCP data.

        The always-on identity listener must not run the combat decoder for
        every lobby TCP message.  Apart from wasting CPU, that let ordinary
        traffic queue ahead of the explicit 03 15 inspect response for many
        seconds.  This path retains everything the independent listener owns
        while skipping damage/action/object attribution entirely.
        """

        if self.first_timestamp is None:
            self.first_timestamp = packet.timestamp_seconds
        if packet.transport.upper() != "TCP":
            return []

        # These observers all have strict direction/length gates and preserve
        # the scene-change signals used to reopen bounded identity discovery.
        self._observe_map_identity(packet)
        self._observe_town_scene_load(packet)
        self._observe_scene_teardown(packet)
        self._observe_gateway_route_catalog(packet)
        self._observe_login_identity(packet)
        self._observe_tcp_player_spawn_identity(packet)
        roster_events = self._observe_tcp_party_roster_identity(packet)

        events: list[LiveDpsEvent] = list(roster_events)
        profile_probe = self._observe_profile_probe(packet, None)
        if profile_probe is not None:
            events.append(profile_probe)
        for observer in (
            self._observe_self_card_mastery,
            self._observe_self_item_archive,
            self._observe_card_profile_update,
        ):
            event = observer(packet)
            if event is not None:
                events.append(event)
        return events

    def feed_events(self, packet: PacketRecord) -> list[LiveDpsEvent]:
        from engine.live.holy_hammer_actions import finalize_packet
        from engine.live.boss_health_wire import packet_boss_health_events
        events = finalize_packet(self, packet, self._feed_events_before_hammer_inputs(packet))
        # After spawn/identity processing, independent of attribution or hit filtering.
        hp_events = packet_boss_health_events(self, packet)
        events.extend(hp_events)
        from engine.live.circle_swing_forms import finalize_packet as circle_finalize
        events = circle_finalize(self, packet, events)
        feed = getattr(self, 'voice_signal_feed', None)
        if feed is not None:
            for event in hp_events:
                feed.health(event)
            for event in events:
                if isinstance(event, BossActionEvent) and event.packet_kind == 'boss_completion_trigger':
                    # Encounter exit is sufficient to retire visual timers, but
                    # is deliberately NOT published as a fabricated zero-HP packet.
                    feed.publish(dict(source=event.source_id, skill=0,
                        timestamp=event.timestamp_ms, templateId=event.template_id,
                        kind='boss_entity_end'))
        return events

    def _feed_events_before_hammer_inputs(self, packet: PacketRecord) -> list[LiveDpsEvent]:
        self._damage_object_packet_serial += 1
        direction = packet.inferred_direction()
        if self.first_timestamp is None:
            self.first_timestamp = packet.timestamp_seconds
        self._observe_map_identity(packet)
        self._observe_town_scene_load(packet)
        self._observe_tcp_world_return(packet)
        self._observe_lobby_return(packet)
        self._observe_scene_teardown(packet)
        self._observe_gateway_route_catalog(packet)
        # 必须早于本包的 Boss 动作/血量解码：技能号反查出来的实体要能在同一个
        # 包里立刻生效，否则中途开抓时第一波技能还是会被丢掉。
        self._observe_skill_identity(packet, direction)
        settlement = (
            decode_settlement(packet, self.first_timestamp)
            if packet.transport.upper() == "TCP" and may_contain_settlement(packet.payload)
            else None
        )
        if settlement is not None:
            return [settlement]
        if packet.transport.upper() == "UDP" and len(packet.payload) == 18:
            boss_actor_id = getattr(self, "boss_actor_id", None)
            boss_template = getattr(self, "monster_entities", {}).get(boss_actor_id)
            if boss_template is not None:
                terminal_end = decode_terminal_boss_end(
                    packet, self.first_timestamp,
                    map_id=int(getattr(self, "detected_map_id", 0) or 0),
                    boss_template_id=int(boss_template.template_id),
                    boss_id=self._monster_event_id(boss_actor_id),
                )
                if terminal_end is not None:
                    return [terminal_end]
        if packet.transport.upper() == "TCP" and len(packet.payload) == 15:
            active_template_ids = {
                int(template.template_id)
                for template in getattr(self, "monster_entities", {}).values()
            }
            completion_rule = match_boss_completion_trigger(
                packet, active_template_ids
            )
            if completion_rule is not None:
                matching_actor_ids = sorted(
                    actor_id
                    for actor_id, template in self.monster_entities.items()
                    if int(template.template_id) == completion_rule.template_id
                )
                if len(matching_actor_ids) == 1:
                    actor_id = matching_actor_ids[0]
                    name, job = self._monster_display_context(actor_id)
                    elapsed_ms = max(
                        0,
                        round(
                            (packet.timestamp_seconds - self.first_timestamp)
                            * 1000
                        ),
                    )
                    return [
                        BossActionEvent(
                            timestamp_ms=elapsed_ms,
                            source_id=self._monster_event_id(actor_id),
                            source_name=name,
                            job=job,
                            skill_id="boss_completion_trigger",
                            skill_name="机制结算",
                            template_id=completion_rule.template_id,
                            action_code=0,
                            packet_kind="boss_completion_trigger",
                            packet_signature=sha256(packet.payload).hexdigest(),
                            packet_length=len(packet.payload),
                            mapped=True,
                            confidence=1.0,
                            evidence_types=(
                                "tcp_trigger_fingerprint",
                                "trigger_ini_all_die_monster_area",
                                completion_rule.trigger_name,
                            ),
                        )
                    ]
            boss_actor_id = getattr(self, "boss_actor_id", None)
            boss_template = getattr(self, "monster_entities", {}).get(boss_actor_id)
            if boss_template is not None:
                clear_trigger = decode_clear_trigger(
                    packet, self.first_timestamp,
                    map_id=int(getattr(self, "detected_map_id", 0) or 0),
                    boss_template_id=int(boss_template.template_id),
                    boss_id=self._monster_event_id(boss_actor_id),
                )
                if clear_trigger is not None:
                    return [clear_trigger]
                clear_reward = decode_clear_reward_trigger(
                    packet, self.first_timestamp,
                    map_id=int(getattr(self, "detected_map_id", 0) or 0),
                    boss_template_id=int(boss_template.template_id),
                    boss_id=self._monster_event_id(boss_actor_id),
                )
                if clear_reward is not None:
                    return [clear_reward]
        packet_features = scan_v576_packet_features(
            packet.payload,
            direction,
            packet.transport,
        )
        is_tcp = packet.transport.upper() == "TCP"
        if not is_tcp and packet_features & V576_PACKET_ACTION:
            self._preobserve_damage_object_actions(packet)
        if not is_tcp and (
            packet_features & V576_PACKET_ACTION
            or packet_may_contain_player_root(packet.payload, direction)
            or self.player_root_action_objects.may_observe_object_birth(
                packet.timestamp_seconds
            )
        ):
            self.player_root_action_objects.observe_packet(
                packet.payload,
                direction,
                packet.timestamp_seconds,
                self._damage_object_packet_serial,
            )
        if not is_tcp:
            self.compact_entity_objects.observe_packet(packet.payload, direction,
                packet.timestamp_seconds, self._damage_object_packet_serial, self.entity_owner_evidence)
        # The owner-only 34/44 message is not a legacy PRIMARY packet. Do
        # not let the fast feature gate discard this independent evidence.
        if not is_tcp and not packet_features & V576_PACKET_PRIMARY:
            for owner_record in self.entity_owner_evidence.observe_packet(
                packet.payload, direction, packet.timestamp_seconds, self._damage_object_packet_serial,
            ):
                confirmed = self._packet_entity_owner(owner_record.entity_id, packet.timestamp_seconds)
                if confirmed is not None:
                    self.summon_owners[owner_record.entity_id] = confirmed
        self.last_boss_spawn_event = None
        self.last_decoded_message = None
        self._retry_corrected_primary = False

        client_registrations = decode_v576_damage_object_registrations(
            packet.payload, direction, transport=packet.transport,
        )
        server_registrations = decode_v576_server_damage_object_registrations(
            packet.payload, direction, DAMAGE_OBJECT_PROJECTILE_TEMPLATE_IDS,
            include_unmapped=True,
        )
        client_compound_births = set()
        legacy_registration = (
            decode_v576_damage_object_registration(
                packet.payload, direction, transport=packet.transport,
            ) if any(r.layout.startswith('c2s-compound-') for r in client_registrations)
            else None
        )
        legacy_key = ((legacy_registration.source_actor_id,
                       legacy_registration.damage_object_id,
                       legacy_registration.record_offset)
                      if legacy_registration is not None else None)
        for damage_object_registration in client_registrations:
            audited_state_fallback = False
            if (damage_object_registration.layout.startswith('c2s-compound-')
                    and damage_object_registration.projectile_template_id == 0):
                # A zero native template means this new reader could not
                # validate the tail. It cannot erase a stronger certificate
                # for this exact bounded record from an existing reader.
                audited_records = tuple(
                    row for row in server_registrations
                    if row.source_actor_id == damage_object_registration.source_actor_id
                    and row.damage_object_id == damage_object_registration.damage_object_id
                    and row.record_offset == damage_object_registration.record_offset
                    and row.xor_phase == damage_object_registration.xor_phase
                    and row.projectile_template_id > 0
                )
                if (audited_records
                        and len({row.projectile_template_id for row in audited_records}) == 1):
                    damage_object_registration = replace(
                        audited_records[0],
                        layout='c2s-compound-audited-object-state-v576',
                        registration_key=damage_object_registration.registration_key,
                    )
                    audited_state_fallback = True
            if damage_object_registration.layout.startswith('c2s-compound-'):
                client_compound_births.add((
                    damage_object_registration.source_actor_id,
                    damage_object_registration.damage_object_id,
                    damage_object_registration.record_offset,
                ))
            if not is_tcp and direction == 'client_to_server':
                new_client_birth = self.player_root_action_objects.observe_client_registration(
                    damage_object_registration, packet.payload,
                    packet.timestamp_seconds, self._damage_object_packet_serial,
                )
                if (not new_client_birth
                        and damage_object_registration.layout.startswith("c2s-compound-")):
                    # A repeated complete compound birth cannot renew the
                    # parent's lifetime or rebind its existing descendants.
                    continue
            # Some complete 09 parent records were already audited as
            # state completions. Richer native fields must not turn those
            # observations into births or renew an expired generation.
            registration_key = (
                damage_object_registration.source_actor_id,
                damage_object_registration.damage_object_id,
                damage_object_registration.record_offset,
            )
            matching_parent_states = tuple(
                row for row in server_registrations
                if (row.source_actor_id, row.damage_object_id, row.record_offset) == registration_key
                and row.projectile_template_id == damage_object_registration.projectile_template_id
                and row.parent_qualified_subtype
                and row.parent_damage_object_id == damage_object_registration.parent_damage_object_id
            ) if damage_object_registration.parent_damage_object_id > 0 else ()
            parent_state_completion = (
                registration_key in client_compound_births
                and registration_key != legacy_key
                and bool(matching_parent_states)
            )
            self.damage_object_origins.observe_registration(
                source_actor_id=damage_object_registration.source_actor_id,
                damage_object_id=damage_object_registration.damage_object_id,
                projectile_template_id=(
                    damage_object_registration.projectile_template_id
                ),
                effect_resource_id=(
                    damage_object_registration.effect_resource_id
                ),
                child_sequence=damage_object_registration.child_sequence,
                timestamp=packet.timestamp_seconds,
                packet_serial=self._damage_object_packet_serial,
                record_offset=damage_object_registration.record_offset,
                packet_validated_subtype=(
                    damage_object_registration.packet_validated_subtype
                ),
                action_qualified_subtype=(
                    damage_object_registration.action_qualified_subtype
                ),
                parent_qualified_subtype=(
                    damage_object_registration.parent_qualified_subtype
                ),
                parent_damage_object_id=damage_object_registration.parent_damage_object_id,
                registration_key=damage_object_registration.registration_key,
                requires_root_action_proof=(
                    damage_object_registration.requires_root_action_proof
                    or (parent_state_completion and any(
                        row.requires_root_action_proof for row in matching_parent_states
                    ))
                ),
                state_observation=audited_state_fallback or parent_state_completion,
            )

        # Other party members' projectile objects are broadcast by the
        # server rather than echoed through the local 107-byte registration.
        # Decode the exact owner + object + Projectile RowId before damage in
        # the same message is processed, so later direct37 rows can use the
        # same deterministic object chain for hosts and guests alike.
        for server_registration in server_registrations:
            if (server_registration.source_actor_id,
                    server_registration.damage_object_id,
                    server_registration.record_offset) in client_compound_births:
                # The generic broadcast reader also sees C2S 09 records. A
                # bounded local birth is authoritative; a second state read
                # cannot replace its explicit parent or revive a duplicate.
                continue
            self.damage_object_origins.observe_registration(
                source_actor_id=server_registration.source_actor_id,
                damage_object_id=server_registration.damage_object_id,
                projectile_template_id=(
                    server_registration.projectile_template_id
                ),
                effect_resource_id=server_registration.effect_resource_id,
                child_sequence=server_registration.child_sequence,
                timestamp=packet.timestamp_seconds,
                packet_serial=self._damage_object_packet_serial,
                record_offset=server_registration.record_offset,
                packet_validated_subtype=(
                    server_registration.packet_validated_subtype
                ),
                action_qualified_subtype=(
                    server_registration.action_qualified_subtype
                ),
                parent_qualified_subtype=(
                    server_registration.parent_qualified_subtype
                ),
                parent_damage_object_id=server_registration.parent_damage_object_id,
                registration_key=server_registration.registration_key,
                requires_root_action_proof=server_registration.requires_root_action_proof,
                # The complete tail proves parent identity, not another
                # creation. Its fingerprint can recur after 250 ms.
                state_observation=True,
            )


        tcp_roster_events: list[PartyMemberEvent] = []
        if is_tcp:
            self._observe_login_identity(packet)
            self._observe_tcp_player_spawn_identity(packet)
            tcp_roster_events = self._observe_tcp_party_roster_identity(packet)
        combat_event = (
            self.feed(packet, packet_features)
            if packet_features & V576_PACKET_PRIMARY
            else None
        )
        decoded_for_cast = self.last_decoded_message
        events: list[LiveDpsEvent] = []
        events.extend(tcp_roster_events)
        if is_tcp:
            profile_probe = self._observe_profile_probe(packet, decoded_for_cast)
            if profile_probe is not None:
                events.append(profile_probe)
            card_mastery = self._observe_self_card_mastery(packet)
            if card_mastery is not None:
                events.append(card_mastery)
            item_archive = self._observe_self_item_archive(packet)
            if item_archive is not None:
                events.append(item_archive)
            card_update = self._observe_card_profile_update(packet)
            if card_update is not None:
                events.append(card_update)
        if self.last_boss_spawn_event is not None:
            events.append(self.last_boss_spawn_event)
        if self.last_monster_appearance_event is not None:
            events.append(self.last_monster_appearance_event)
            self.last_monster_appearance_event = None
        # Boss action grouping also consumes ordinary Boss-to-player damage
        # hits as weak evidence.  Those fixed hit packets do not carry an
        # action marker, so route them by the already-decoded primary message.
        boss_hit_candidate = (
            decoded_for_cast is not None
            and decoded_for_cast.kind == "damage_hit"
            and decoded_for_cast.source_actor_id in self.boss_actor_ids
        )
        if packet_features & V576_PACKET_BOSS_ACTION or boss_hit_candidate:
            events.extend(self._boss_action_events(packet))
        if (
            decoded_for_cast is not None
            and decoded_for_cast.kind in {"skill_cast", "skill_action"}
            and decoded_for_cast.source_actor_id is not None
            and decoded_for_cast.skill_id is not None
            and decoded_for_cast.skill_id in self.skill_catalog
            and decoded_for_cast.source_actor_id not in self.summon_owners
        ):
            cast_key = (
                decoded_for_cast.source_actor_id,
                decoded_for_cast.skill_id,
            )
            previous_cast = self.recent_emitted_casts.get(
                cast_key, float("-inf")
            )
            # A server action normally follows the client's cast message by
            # roughly 0.07 seconds. Keep one exact cast on the timeline.
            if packet.timestamp_seconds - previous_cast > 0.2:
                actor_id = decoded_for_cast.source_actor_id
                party_event = self._observe_party_member(
                    actor_id,
                    packet.timestamp_seconds,
                    decoded_for_cast.layout,
                )
                if party_event is not None:
                    events.append(party_event)
                source_id = f"0x{actor_id:08X}"
                source_name, source_job = self._actor_display_context(actor_id)
                origin = self.first_timestamp or packet.timestamp_seconds
                combo_rules = self.derived_combo_action_rules.get(
                    decoded_for_cast.skill_id, ()
                )
                if combo_rules:
                    action_history = self.recent_actor_casts.get(actor_id, [])
                    for combo_rule in combo_rules:
                        predecessor_match = combo_rule.match_predecessor(
                            action_history,
                            packet.timestamp_seconds,
                        )
                        if predecessor_match is None:
                            continue
                        inferred_skill_id = combo_rule.intermediate_skill_id
                        inferred_key = (actor_id, inferred_skill_id)
                        inferred_previous = self.recent_emitted_casts.get(
                            inferred_key, float("-inf")
                        )
                        if (
                            packet.timestamp_seconds - inferred_previous
                            > 0.2
                        ):
                            events.append(
                                SkillCastEvent(
                                    timestamp_ms=max(
                                        0,
                                        round(
                                            (
                                                packet.timestamp_seconds
                                                - origin
                                            )
                                            * 1000
                                        )
                                        - round(
                                            combo_rule.anchor_before_trigger_seconds
                                            * 1000
                                        ),
                                    ),
                                    source_id=source_id,
                                    source_name=source_name,
                                    job=source_job,
                                    skill_id=str(inferred_skill_id),
                                    skill_name=_skill_display_name(
                                        self.skill_catalog, inferred_skill_id
                                    ),
                                    packet_kind="inferred_combo",
                                )
                            )
                            self.recent_emitted_casts[
                                inferred_key
                            ] = packet.timestamp_seconds
                packet_kind = decoded_for_cast.kind
                if (
                    decoded_for_cast.layout
                    == "c2s-udp-skill-input-46-v576"
                ):
                    # The compact client record proves that the button/action
                    # occurred, but it does not prove that the action reached
                    # its damage stage. Keep it on the timeline without
                    # letting attribution steal nearby damage from another
                    # skill when the action was cancelled or used for movement.
                    packet_kind = "compact_skill_input"
                cast_event = SkillCastEvent(
                    timestamp_ms=max(
                        0,
                        round((packet.timestamp_seconds - origin) * 1000),
                    ),
                    source_id=source_id,
                    source_name=source_name,
                    job=source_job,
                    skill_id=str(decoded_for_cast.skill_id),
                    skill_name=_skill_display_name(
                        self.skill_catalog, decoded_for_cast.skill_id
                    ),
                    packet_kind=packet_kind,
                )
                events.append(cast_event)
                events.extend(
                    self._activate_permanent_player_buff(
                        source_actor_id=actor_id,
                        skill_id=decoded_for_cast.skill_id,
                        timestamp_ms=cast_event.timestamp_ms,
                    )
                )
                self.recent_emitted_casts[cast_key] = packet.timestamp_seconds
        if (
            self.action_parser_mode == "structured"
            and packet_features & V576_PACKET_ACTION
        ):
            known_action_actors = (
                self.skill_actor_ids
                | self.local_actor_ids
                | set(self.known_actor_names)
            )
            structured_actions = decode_v576_structured_actions(
                packet.payload,
                packet.inferred_direction(),
                self.action_skill_ids,
                known_action_actors,
                allow_unknown_actor_ids=True,
            )
            primary_identity = (
                (decoded_for_cast.source_actor_id, decoded_for_cast.skill_id)
                if decoded_for_cast is not None
                and decoded_for_cast.kind in {"skill_cast", "skill_action"}
                else None
            )
            for structured_action in structured_actions:
                party_event = self._observe_party_member(
                    structured_action.source_actor_id,
                    packet.timestamp_seconds,
                    structured_action.layout,
                )
                if party_event is not None:
                    events.append(party_event)
                identity = (
                    structured_action.source_actor_id,
                    _canonical_live_skill_id(structured_action.skill_id),
                )
                if identity == primary_identity:
                    continue
                self.structured_action_candidates += 1
                events.extend(
                    self._structured_action_events(structured_action, packet)
                )
        if combat_event is not None:
            try:
                combat_actor_id = int(str(combat_event.source_id), 16)
            except (TypeError, ValueError):
                combat_actor_id = 0
            party_event = self._observe_party_member(
                combat_actor_id,
                packet.timestamp_seconds,
                getattr(decoded_for_cast, "layout", "damage_observation"),
            )
            if party_event is not None:
                events.append(party_event)
            events.append(combat_event)
        if packet_features & V576_PACKET_HEALTH:
            health_events = self._player_health_events(packet)
            events.extend(health_events)
            self._observe_resource_scene_return(packet, health_events)
        if packet_features & V576_PACKET_LIFECYCLE:
            events.extend(self._player_lifecycle_events(packet))
        if packet_features & V576_PACKET_HEALING:
            events.extend(self._healing_events(packet))
        if packet_features & V576_PACKET_STATUS_DAMAGE:
            events.extend(
                self._supplemental_status_damage_events(
                    packet,
                    decoded_for_cast,
                )
            )
        if packet_features & V576_PACKET_EMBEDDED_DIRECT_DAMAGE:
            events.extend(
                self._supplemental_embedded_direct_damage_events(
                    packet,
                    decoded_for_cast,
                    retry_corrected_primary=(combat_event is None and self._retry_corrected_primary),
                )
            )
        events.extend(self._backfill_permanent_player_buffs())
        events.extend(
            self._azuna_breath_periodic_refresh_events(
                packet.timestamp_seconds
            )
        )
        statuses = (
            decode_v576_status_events(
                packet.payload,
                direction,
                self.status_known_skill_ids,
            )
            if packet_features & V576_PACKET_STATUS
            else ()
        )
        aura_ends = (
            decode_aura_instance_ends(packet.payload, direction)
            if not is_tcp and packet_features & V576_PACKET_STATUS_END
            else ()
        )
        visible_statuses = []
        for status in statuses:
            if self.instant_action_damage.observe_status(
                status,
                timestamp=packet.timestamp_seconds,
                packet_serial=self._damage_object_packet_serial,
            ):
                # These zero-duration action/filter responses are internal
                # lifecycle evidence. Publishing them as a Boss debuff or a
                # player Buff would create a fictitious 0-second status.
                continue
            visible_statuses.append(status)
            self.field_effect_instances.observe_status(
                source_actor_id=int(status.source_actor_id or 0),
                target_actor_id=int(status.target_actor_id or 0),
                skill_id=int(status.skill_id or 0),
                effect_code=str(status.effect_code or ""),
                effect_class_id=int(status.effect_class_id or 0),
                parameters=str(status.parameters or ""),
                duration_ms=int(status.duration_ms or 0),
                effect_instance_id=int(status.effect_instance_id or 0),
                timestamp=packet.timestamp_seconds,
            )
            self.damage_object_origins.observe_response(
                source_actor_id=status.source_actor_id,
                skill_id=status.skill_id,
                effect_code=status.effect_code,
                parameters=status.parameters,
                timestamp=packet.timestamp_seconds,
                effect_instance_id=status.effect_instance_id,
                skill_level_row_id=status.skill_level_row_id,
                effect_class_id=status.effect_class_id,
            )
            # Preserve the unprojected response row before Buff/debuff display
            # filtering.  Glyph inference needs the extra effect class even
            # when the target is a temporary projectile or field object that
            # correctly has no user-facing status row.
            source_actor_id, resolved_skill_id = self._status_source_context(
                status.source_actor_id,
                status.skill_id,
                packet.timestamp_seconds,
            )
            source_name, source_job = self._actor_display_context(
                source_actor_id
            )
            direct_effect = self.status_effect_catalog.get(status.skill_id)
            status_skill_name = self.status_skill_catalog.get(
                status.skill_id, ""
            )
            if status.skill_id in _AZUNA_BREATH_INTERNAL_STATUS_SKILL_IDS:
                skill_name = _skill_display_name(
                    self.skill_catalog, resolved_skill_id
                )
            elif direct_effect is not None:
                skill_name = direct_effect.display_name
            elif resolved_skill_id == status.skill_id and status_skill_name:
                skill_name = status_skill_name
            else:
                skill_name = _skill_display_name(
                    self.skill_catalog, resolved_skill_id
                )
            origin = self.first_timestamp or packet.timestamp_seconds
            events.append(
                SkillParameterEvent(
                    timestamp_ms=max(
                        0,
                        round(
                            (packet.timestamp_seconds - origin) * 1000
                        ),
                    ),
                    source_id=f"0x{source_actor_id:08X}",
                    source_name=source_name,
                    job=source_job,
                    skill_id=str(resolved_skill_id),
                    skill_name=skill_name,
                    target_id=f"0x{status.target_actor_id:08X}",
                    raw_skill_id=str(status.skill_id),
                    skill_level_row_id=int(status.skill_level_row_id or 0),
                    effect_class_id=int(status.effect_class_id or 0),
                    effect_code=status.effect_code,
                    parameters=status.parameters,
                    duration_ms=max(0, int(status.duration_ms)),
                    permanent=bool(status.permanent),
                    effect_instance_id=int(status.effect_instance_id or 0),
                )
            )
        statuses = tuple(visible_statuses)
        events.extend(self._direct_player_buff_clear_events(statuses, packet))
        events.extend(self._boss_status_action_events(statuses, packet))
        if (self.boss_actor_id in (self.skill_actor_ids | self.local_actor_ids)
            and self.boss_actor_id not in self.monster_entities):
            self.boss_actor_id = None  # Clear stale guesses even on identity-only packets.
        if not statuses:
            for target_id, instance in aura_ends:
                removed = self.aura_instances.end(
                    f"0x{target_id:08X}", instance,
                    max(0, round((packet.timestamp_seconds - (self.first_timestamp or packet.timestamp_seconds)) * 1000)),
                )
                if removed is not None:
                    events.append(removed)
            return self._canonicalize_runtime_alias_events(
                self._finalize_player_root_action_objects(
                    self._finalize_late_field_hits(events, packet), packet
                )
            )

        # Damage records identify the active enemy directly. Player targets are
        # kept separately so party and personal buffs can be inspected without
        # being mixed into the Boss debuff table.
        player_actors = (
            self.skill_actor_ids | self.local_actor_ids
        ) - self.monster_entities.keys()
        for status in statuses:
            if (is_aura_application(status)
                and status.source_actor_id not in self.monster_entities
                and status.target_actor_id not in self.monster_entities):
                self.packet_aura_player_ids.update((status.source_actor_id, status.target_actor_id))
        player_actors |= self.packet_aura_player_ids - self.monster_entities.keys()
        if self.boss_actor_id in player_actors:
            # A provisional status-only target must not override proven players.
            self.boss_actor_id = None
        boss_actor_id = self.boss_actor_id
        if boss_actor_id is None:
            candidate_targets = [
                status.target_actor_id
                for status in statuses
                if status.source_actor_id in player_actors
                and status.target_actor_id not in player_actors
                and status.skill_id not in AURA_SKILL_IDS
                and _summon_skill_key(_skill_display_name(self.skill_catalog, status.skill_id)) not in self.team_buff_skill_names
            ]
            if candidate_targets:
                boss_actor_id = Counter(candidate_targets).most_common(1)[0][0]
                self.boss_actor_id = boss_actor_id
        if boss_actor_id is None and not player_actors:
            return self._canonicalize_runtime_alias_events(
                self._finalize_player_root_action_objects(
                    self._finalize_late_field_hits(events, packet), packet
                )
            )

        origin = self.first_timestamp or packet.timestamp_seconds
        timestamp_ms = max(0, round((packet.timestamp_seconds - origin) * 1000))
        for status in statuses:
            target_template = self.monster_entities.get(status.target_actor_id)
            target_scope = (
                ""
                if status.target_actor_id in self.summon_owners
                else (
                    "player"
                    if status.target_actor_id in player_actors
                    else "boss"
                    if (
                        status.target_actor_id == boss_actor_id
                        or status.target_actor_id in self.damage_target_counts
                        or (
                            target_template is not None
                            and target_template.boss
                        )
                    )
                    else ""
                )
            )
            if not target_scope:
                continue
            if status.skill_id in AURA_SKILL_IDS and not is_aura_application(status):
                continue
            if target_scope == "player" and _is_holy_hammer_attack_status(
                status.skill_id,
                status.effect_code,
                status.duration_ms,
                status.permanent,
            ):
                attack_key = (status.source_actor_id, status.target_actor_id)
                previous_attack = self.recent_holy_hammer_attacks.get(
                    attack_key, float("-inf")
                )
                # Legacy status-only fallback for actors without native input
                # evidence. A status group does NOT count every combo attack;
                # finalize_packet suppresses it when UDP basic inputs exist.
                if packet.timestamp_seconds - previous_attack > 0.08:
                    source_name, job = self._actor_display_context(
                        status.source_actor_id
                    )
                    events.append(
                        SkillCastEvent(
                            timestamp_ms=timestamp_ms,
                            source_id=f"0x{status.source_actor_id:08X}",
                            source_name=source_name,
                            job=job,
                            skill_id="3224:basic_attack",
                            skill_name="神圣之锤攻击",
                            packet_kind="status_skill_action",
                        )
                    )
                    self.recent_holy_hammer_attacks[
                        attack_key
                    ] = packet.timestamp_seconds
                continue
            if target_scope == "player" and _is_zero_value_action_status_marker(
                status.effect_code,
                status.parameters,
                status.permanent,
            ):
                # Several skills include this permanent-looking zero component
                # in their action state.  It is not the actual player buff;
                # treating it as one makes finite effects such as Ghost Soul
                # appear active until the capture ends.
                continue
            source_actor_id, resolved_skill_id = self._status_source_context(
                status.source_actor_id,
                status.skill_id,
                packet.timestamp_seconds,
            )
            if (
                target_scope == "player"
                and resolved_skill_id in {6316, 6318}
                and status.effect_code in {"134400", "136900"}
            ):
                # These are the player's persistent companion-control and
                # constructor states. They prove that an Azuna object exists,
                # but they are not the +7% party Buff. Only the companion's
                # 2619xx -> 134700 / 0.07 / 30000ms refresh is displayable and
                # scoreable as Breath.
                continue
            self._remember_refreshable_pulsing_aura_status(
                source_actor_id=source_actor_id,
                target_actor_id=status.target_actor_id,
                raw_skill_id=status.skill_id,
                resolved_skill_id=resolved_skill_id,
                effect_class_id=int(status.effect_class_id or 0),
                parameters=status.parameters,
                duration_ms=status.duration_ms,
                effect_instance_id=int(status.effect_instance_id or 0),
                timestamp=packet.timestamp_seconds,
            )
            packet_value = self._packet_percent(status.parameters)
            if target_scope == "player":
                source_actor_id, resolved_skill_id = self._player_buff_source_context(
                    source_actor_id=source_actor_id,
                    target_actor_id=status.target_actor_id,
                    resolved_skill_id=resolved_skill_id,
                    effect_code=status.effect_code,
                    duration_ms=status.duration_ms,
                    packet_value=packet_value,
                    timestamp=packet.timestamp_seconds,
                )
            if target_scope == "player" and source_actor_id not in player_actors:
                # Enemy states on party members are debuffs, not party gains.
                # This page intentionally reports player-origin buffs only.
                continue
            if target_scope == "player":
                mechanic_cast = self._status_backed_mechanic_cast_event(
                    source_actor_id=source_actor_id,
                    target_actor_id=status.target_actor_id,
                    skill_id=resolved_skill_id,
                    timestamp=packet.timestamp_seconds,
                    timestamp_ms=timestamp_ms,
                )
                if mechanic_cast is not None:
                    events.append(mechanic_cast)
            if target_scope == "boss":
                self._remember_periodic_status(
                    source_actor_id=source_actor_id,
                    target_actor_id=status.target_actor_id,
                    raw_skill_id=status.skill_id,
                    resolved_skill_id=resolved_skill_id,
                    parameters=status.parameters,
                    duration_ms=status.duration_ms,
                    effect_code=status.effect_code,
                    timestamp=packet.timestamp_seconds,
                )
            source_id = f"0x{source_actor_id:08X}"
            source_name, job = self._actor_display_context(source_actor_id)
            direct_effect = self.status_effect_catalog.get(status.skill_id)
            status_skill_name = self.status_skill_catalog.get(status.skill_id, "")
            if status.skill_id in _AZUNA_BREATH_INTERNAL_STATUS_SKILL_IDS:
                skill_name = _skill_display_name(
                    self.skill_catalog, resolved_skill_id
                )
            elif direct_effect is not None:
                skill_name = direct_effect.display_name
            elif resolved_skill_id == status.skill_id and status_skill_name:
                skill_name = status_skill_name
            else:
                skill_name = _skill_display_name(
                    self.skill_catalog, resolved_skill_id
                )
            if target_scope == "player":
                target_name, target_job = self._actor_display_context(
                    status.target_actor_id
                )
                is_team_buff = (
                    _summon_skill_key(skill_name) in self.team_buff_skill_names
                    or source_actor_id != status.target_actor_id
                )
                effect_label = (
                    direct_effect.display_name
                    if direct_effect is not None
                    else (
                        f"{skill_name}增益"
                        if skill_name != str(resolved_skill_id)
                        else "待识别增益"
                    )
                )
                buff_event = BossStatusEvent(
                        timestamp_ms=timestamp_ms,
                        target_id=f"0x{status.target_actor_id:08X}",
                        source_id=source_id,
                        source_name=source_name,
                        job=job,
                        skill_id=str(resolved_skill_id),
                        skill_name=skill_name,
                        effect_key=(
                            f"角色增益:{resolved_skill_id}:"
                            f"{status.effect_code}:{status.parameters}"
                            + (f":class{status.effect_class_id}" if resolved_skill_id in AURA_SKILL_IDS else "")
                        ),
                        effect_label=(
                            "阿兹那之息增益"
                            if status.skill_id
                            in _AZUNA_BREATH_INTERNAL_STATUS_SKILL_IDS
                            else effect_label
                        ),
                        value=packet_value,
                        duration_ms=(0 if status.permanent else max(1, status.duration_ms)),
                        status_kind=(
                            "团队增益" if is_team_buff else "个人增益"
                        ),
                        effect_code=status.effect_code,
                        parameters=status.parameters,
                        target_name=target_name,
                        target_job=target_job,
                        target_scope="player",
                        permanent=status.permanent,
                        value_source=("packet" if packet_value is not None else "unavailable"),
                        duration_source=(
                            "packet" if status.permanent or status.duration_ms > 0
                            else "unavailable"
                        ),
                        effect_instance_id=(int(status.effect_instance_id or 0) if resolved_skill_id in AURA_SKILL_IDS else 0),
                        effect_class_id=(int(status.effect_class_id or 0) if resolved_skill_id in AURA_SKILL_IDS else 0),
                    )
                if resolved_skill_id in AURA_SKILL_IDS:
                    events.extend(self.aura_instances.observe(buff_event))
                else:
                    events.append(buff_event)
                if (
                    status.skill_id
                    in _AZUNA_BREATH_INTERNAL_STATUS_SKILL_IDS
                ):
                    events.extend(
                        self._observe_azuna_breath_status(
                            raw_source_actor_id=status.source_actor_id,
                            owner_actor_id=source_actor_id,
                            raw_skill_id=status.skill_id,
                            effect_code=status.effect_code,
                            parameters=status.parameters,
                            value=packet_value,
                            timestamp=packet.timestamp_seconds,
                        )
                    )
                continue
            target_id = self._monster_event_id(status.target_actor_id)
            target_name, target_job = self._monster_display_context(
                status.target_actor_id
            )
            target_max_hp = self._monster_max_hp(status.target_actor_id)
            catalog_job = self.skill_catalog.get(
                resolved_skill_id, ("", "", 0)
            )[1]
            candidates: list[_ResolvedBossDebuff] = []
            if direct_effect is not None and direct_effect.rule_key:
                resolved_value = (
                    packet_value
                    if packet_value is not None
                    else direct_effect.fallback_value
                )
                candidates.append(
                    _ResolvedBossDebuff(
                        effect_key=direct_effect.rule_key,
                        value=resolved_value,
                        duration_ms=(
                            status.duration_ms
                            if status.duration_ms > 0
                            else (direct_effect.fallback_duration_ms or 1)
                        ),
                        parameters=status.parameters,
                        stackable=direct_effect.stackable,
                        stack_group=direct_effect.stack_group,
                        value_source=(
                            "packet" if packet_value is not None else "preset_fallback"
                        ),
                        duration_source=(
                            "packet" if status.duration_ms > 0 else "preset_fallback"
                        ),
                    )
                )
            else:
                special_status = boss_status_override(
                    skill_name,
                    status.effect_code,
                    parameters=status.parameters,
                    duration_seconds=status.duration_ms / 1000.0,
                    effect_class_id=int(status.effect_class_id or 0),
                )
                observed_effect_key = ""
                if special_status is not None:
                    rule, readable_parameters = special_status
                    observed_effect_key = rule.key
                    resolved_packet_value = packet_effect_value_for_rule(
                        rule, status.parameters, packet_value
                    )
                    candidates.append(
                        _ResolvedBossDebuff(
                            effect_key=rule.key,
                            value=(
                                resolved_packet_value
                                if resolved_packet_value is not None
                                else rule.value
                            ),
                            duration_ms=(
                                status.duration_ms
                                if status.duration_ms > 0
                                else max(1, round(rule.duration_seconds * 1000))
                            ),
                            parameters=readable_parameters,
                            stackable=rule.stackable,
                            stack_group=rule.stack_group,
                            value_source=(
                                "packet"
                                if resolved_packet_value is not None
                                else "preset_fallback"
                            ),
                            duration_source=(
                                "packet" if status.duration_ms > 0 else "preset_fallback"
                            ),
                        )
                    )
                rules = boss_effect_rules_for_status_packet(
                    skill_name,
                    job,
                    packet_value=packet_value,
                    duration_seconds=status.duration_ms / 1000.0,
                    observed_effect_key=observed_effect_key,
                    catalog_job=catalog_job,
                    effect_code=status.effect_code,
                )
                if len(rules) > 1:
                    value_matches = tuple(
                        rule
                        for rule in rules
                        if packet_value is not None
                        and rule.value is not None
                        and abs(rule.value - packet_value) <= 1e-6
                    )
                    if value_matches:
                        rules = value_matches
                    else:
                        packet_duration = status.duration_ms / 1000.0
                        duration_matches = tuple(
                            rule
                            for rule in rules
                            if abs(rule.duration_seconds - packet_duration) <= 0.25
                        )
                        if duration_matches:
                            rules = duration_matches
                for rule in rules:
                    resolved_packet_value = packet_effect_value_for_rule(
                        rule, status.parameters, packet_value
                    )
                    resolved_value = (
                        resolved_packet_value
                        if resolved_packet_value is not None
                        else (
                            rule.value
                            if rule.value is not None
                            else packet_value
                        )
                    )
                    candidates.append(
                        _ResolvedBossDebuff(
                            effect_key=rule.key,
                            value=resolved_value,
                            duration_ms=boss_effect_duration_ms(
                                rule, status.duration_ms
                            ),
                            parameters=status.parameters,
                            stackable=rule.stackable,
                            stack_group=rule.stack_group,
                            value_source=(
                                "packet"
                                if resolved_packet_value is not None
                                or (rule.value is None and packet_value is not None)
                                else "preset_fallback"
                            ),
                            duration_source=(
                                "packet" if status.duration_ms > 0 else "preset_fallback"
                            ),
                        )
                    )

            evidence_job = (
                catalog_job
                if not job or job in {"未知职业", "待识别职业"}
                else job
            )
            evidence = build_debuff_evidence_key(
                job=evidence_job,
                skill_id=resolved_skill_id,
                status_code=status.effect_code,
                parameters=status.parameters,
            )
            resolved_debuffs = self.debuff_evidence_registry.resolve(
                evidence,
                candidates,
                fingerprint=_ResolvedBossDebuff.fingerprint,
            )
            if resolved_debuffs:
                for effect in resolved_debuffs:
                    events.append(
                        BossStatusEvent(
                            timestamp_ms=timestamp_ms,
                            target_id=target_id,
                            source_id=source_id,
                            source_name=source_name,
                            job=job,
                            skill_id=str(resolved_skill_id),
                            skill_name=skill_name,
                            effect_key=effect.effect_key,
                            effect_label=boss_effect_label(effect.effect_key),
                            value=effect.value,
                            duration_ms=effect.duration_ms,
                            status_kind="Boss减益",
                            effect_code=status.effect_code,
                            parameters=effect.parameters,
                            evidence_key=evidence.display(),
                            stackable=effect.stackable,
                            stack_group=effect.stack_group,
                            target_name=target_name,
                            target_job=target_job,
                            target_max_hp=target_max_hp,
                            value_source=effect.value_source,
                            duration_source=effect.duration_source,
                            permanent=status.permanent,
                            effect_instance_id=int(status.effect_instance_id or 0),
                            effect_class_id=int(status.effect_class_id or 0),
                            raw_parameters=status.parameters,
                            raw_duration_ms=status.duration_ms,
                        )
                    )
                continue
            status_kind = (
                "Boss自身增益"
                if status.source_actor_id == status.target_actor_id
                else "Boss附加状态"
            )
            effect_label = (
                status_kind
                if skill_name == str(resolved_skill_id)
                else f"{skill_name}状态"
            )
            events.append(
                BossStatusEvent(
                    timestamp_ms=timestamp_ms,
                    target_id=target_id,
                    source_id=source_id,
                    source_name=source_name,
                    job=job,
                    skill_id=str(resolved_skill_id),
                    skill_name=skill_name,
                    effect_key=f"附加状态:{status.effect_code}",
                    effect_label=effect_label,
                    value=packet_value,
                    duration_ms=status.duration_ms,
                    status_kind=status_kind,
                    effect_code=status.effect_code,
                    parameters=status.parameters,
                    evidence_key=evidence.display(),
                    target_name=target_name,
                    target_job=target_job,
                    target_max_hp=target_max_hp,
                    value_source=("packet" if packet_value is not None else "unavailable"),
                    duration_source=("packet" if status.duration_ms > 0 else "unavailable"),
                    permanent=status.permanent,
                    effect_instance_id=int(status.effect_instance_id or 0),
                    effect_class_id=int(status.effect_class_id or 0),
                )
            )
        for target_id, instance in aura_ends:
            removed = self.aura_instances.end(f"0x{target_id:08X}", instance, timestamp_ms)
            if removed is not None:
                events.append(removed)
        deduplicated: list[LiveDpsEvent] = []
        for event in events:
            if not isinstance(event, BossStatusEvent):
                deduplicated.append(event)
                continue
            if event.target_scope == "boss" and not event.target_name:
                try:
                    target_actor_id = int(event.target_id, 0)
                except ValueError:
                    target_actor_id = 0
                target_name, target_job = self._monster_display_context(
                    target_actor_id
                )
                if target_name:
                    event = replace(
                        event,
                        target_id=self._monster_event_id(target_actor_id),
                        target_name=target_name,
                        target_job=target_job,
                    )
            fingerprint = (
                event.target_id,
                event.source_id,
                event.skill_id,
                event.effect_key,
                event.effect_code,
                event.value,
                event.duration_ms,
                event.operation,
                event.effect_instance_id,
            )
            previous = self.recent_status_events.get(fingerprint)
            if (
                previous is not None
                and 0.0 <= packet.timestamp_seconds - previous <= 0.05
            ):
                continue
            self.recent_status_events[fingerprint] = packet.timestamp_seconds
            deduplicated.append(event)
        if len(self.recent_status_events) > 512:
            cutoff = packet.timestamp_seconds - 30.0
            self.recent_status_events = {
                key: seen_at
                for key, seen_at in self.recent_status_events.items()
                if seen_at >= cutoff
            }
        return self._canonicalize_runtime_alias_events(
            self._finalize_player_root_action_objects(
                self._finalize_late_field_hits(deduplicated, packet), packet
            )
        )


# One confirmed Punisher action is one four-hit round.
from engine.live.crippling_punisher_actions import install_decoder_hook
install_decoder_hook(V576DecoderCoreMixin)
