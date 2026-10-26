from __future__ import annotations

import hashlib
import importlib.util
import marshal
from functools import lru_cache
from pathlib import Path
from typing import Iterable


FINGERPRINT_SCHEMA = "dragon-nest-version-fingerprint-v1"

_REPLAY_MODULES = (
    "engine.live.circle_swing_composition",
    "engine.live.circle_swing_forms",
    "engine.live.nightmare_sea_dragon",
    "engine.live.crippling_punisher_actions",
    "engine.live.holy_hammer_actions",
    "engine.live.aura_state",
    "engine.live.character_identity_sessions",
    "engine.live.client_native_skill_catalog",
    "engine.live.skill_plate_damage",
    "engine.live.player_root_action_objects",
    "engine.live.generated_player_root_action_specs",
    "engine.live.generated_player_root_full_action_specs",
    "engine.live.entity_damage_families",
    "engine.live.shock_damage_identity",
    "engine.live.compound_skill_responses",
    "engine.live.native_damage_skill_keys",
    "engine.live.settlement",
    "engine.live.clear_trigger",
    "engine.live.clear_reward_trigger",
    "engine.data_loader",
    "engine.effect_stacking",
    "engine.team_support_rules",
    "engine.live.action_parser",
    "engine.live.aggregator",
    "engine.live.mechanic_damage",
    "engine.live.self_hp_cost",
    "engine.live.boss_action",
    "engine.live.boss_hp",
    "engine.live.boss_health_wire",
    "engine.live.boss_recognizer",
    "engine.live.cast_timeline",
    "engine.live.damage_attribution",
    "engine.live.damage_object_origins",
    "engine.live.generated_attack_instance_specs",
    "engine.live.damage_ledger",
    "engine.live.damage_wire_evidence",
    "engine.live.entity_damage_owner",
    "engine.live.field_effect_instances",
    "engine.live.instant_action_damage",
    "engine.live.skill_attribution",
    "engine.live.udp_damage_records",
    "engine.live.udp_damage_identity",
    "engine.live.debuff_evidence",
    "engine.live.effects",
    "engine.live.event_ledger",
    "engine.live.healing_attribution",
    "engine.live.periodic_health_mechanism",
    "engine.live.inspect_catalog",
    "engine.live.models",
    "engine.live.nest_recognizer",
    "engine.live.profile",
    "engine.live.protocol",
    "engine.live.tls_probe",
    "engine.live.capture_health",
    "engine.live.udp_direction",
    "engine.live.dungeon_map_catalog",
    "engine.live.sources",
    "engine.live.summon_presence",
    "engine.live.entity_owner_evidence",
    "engine.live.autonomous_attack_families",
    "engine.live.compact_entity_object_evidence",
    "engine.live.pve_scene_evidence",
    "engine.live.summon_attack_runtime",
    "engine.live.summon_self_attack_evidence",
    "engine.live.generated_summon_self_attack_specs_v26",
    "engine.live.generated_mirror_self_attack_specs",
    "engine.live.damage_object_shaman_illusion",
    "engine.live.capture_sources",
    "engine.live.prebattle_state",
    "engine.live.replay_duplicate_hit_evidence",
    "engine.live.protocol_catalogs",
    "engine.live.replay_normalization",
    "engine.live.tcp_profile_codec",
    "engine.live.timeline_evidence",
    "engine.live.wire.actor_state",
    "engine.live.wire.combat_decoder",
    "engine.live.wire.core_decoder",
    "engine.live.wire.profile_decoder",
    "engine.live.wire.status_decoder",
)

_SCORE_MODULES = (
    "engine.mixins.skill_timeline_effects",
    "engine.live.team_score_sync",
    "engine.job_mechanics",
    "engine.cooldown",
    "engine.native_timeline",
    "engine.mixins.skill_timeline_support",
    "engine.mixins.skill_timeline_optimizer",
    "engine.mixins.skill_timeline_baseline_policy",
    "engine.mixins.timeline_runtime",
    "engine.equipment_constants",
    "engine.equipment_resonance",
    "engine.live.score_equipment_defaults",
    "engine.live.score_equipment",
    "engine.live.score_equipment_uniform",
    "engine.calculator",
    "engine.data_loader",
    "engine.effect_stacking",
    "engine.hp_conditional",
    "engine.live.aggregator",
    "engine.live.healing_attribution",
    "engine.live.periodic_health_mechanism",
    "engine.live.nest_recognizer",
    "engine.live.damage_ledger",
    "engine.live.damage_object_origins",
    "engine.live.field_effect_instances",
    "engine.live.instant_action_damage",
    "engine.live.skill_attribution",
    "engine.live.generated_attack_instance_specs",
    "engine.live.protocol",
    "engine.live.score_engine",
    "engine.live.castability",
    "engine.fury_planner",
    "engine.live.score",
    "engine.live.score_body_policy",
    "engine.live.score_burst",
    "engine.live.calibration_boss_scope",
    "engine.live.skill_realization",
    "engine.live.hit_parallel",
    "engine.live.full_run_score",
    "engine.live.score_hit_evidence",
    "engine.live.score_glyphs",
    "engine.live.score_glyph_selection",
    "engine.live.score_glyph_objective",
    "engine.live.score_intervals",
    "engine.live.score_profiles",
    "engine.live.score_role_duties",
    "engine.live.healer_demand",
    "engine.live.healer_rescue",
    "engine.live.duty_groups",
    "engine.live.score_rotation",
    "engine.live.score_support",
    "engine.live.score_support_allocation",
    "engine.live.score_support_catalog",
    "engine.live.score_support_context",
    "engine.live.score_support_counterfactual",
    "engine.live.score_survival",
    "engine.live.scoring",
    "engine.live.cast_timeline",
    "engine.live.combat_glyph_inference",
    "engine.live.cooldown_glyph_evidence",
    "engine.live.combat_skill_levels",
    "engine.live.damage_attribution",
    "engine.live.timeline_evidence",
    "engine.live.wire.core_decoder",
    "engine.live.wire.status_decoder",
    "engine.live.theory_planner",
    "engine.live.theory_plan_builder",
    "engine.live.timing_ablation",
    "engine.mechanism_timeline",
    "engine.native.overlay_service",
    "engine.native.replay_score_service",
    "engine.profile_build_sync",
    "engine.shared",
    "engine.skill_adjustments",
    "engine.skill_glyph_policy",
    "engine.mixins.extreme_engine",
    "engine.mixins.skill_build",
    "engine.mixins.skill_simulation",
    "engine.mixins.skill_timeline_engine",
    "engine.mixins.skill_timeline_local_optimizer",
    "engine.mixins.skill_timeline_output",
    "engine.mixins.skill_timeline_policy_1_6",
    "engine.mixins.skill_timeline_schedule",
    "engine.mixins.skill_timeline_scheme_1",
    "engine.mixins.skill_timeline_state",
    "engine.mixins.team_effects",
    "engine.mixins.team_practical",
    "engine.mixins.timeline_presenter",
    "engine.team_support_rules",
    "engine.timeline_engine",
    "engine.timeline_parallel",
    "engine.timeline_parallel",
    "engine.ui.live_page",
    "engine.core",
    "engine.legacy_timeline",
)


def _update_file(digest: "hashlib._Hash", path: Path, label: str) -> None:
    digest.update(label.encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
    except OSError:
        digest.update(b"<missing>")
    digest.update(b"\0")


@lru_cache(maxsize=8)
def module_code_fingerprint(module_names: tuple[str, ...]) -> str:
    """Hash importable bytecode, including frozen/PyInstaller modules."""

    digest = hashlib.sha256(FINGERPRINT_SCHEMA.encode("ascii"))
    for module_name in sorted(set(module_names)):
        digest.update(module_name.encode("utf-8"))
        digest.update(b"\0")
        try:
            spec = importlib.util.find_spec(module_name)
            loader = spec.loader if spec is not None else None
            get_code = getattr(loader, "get_code", None)
            code = get_code(module_name) if callable(get_code) else None
            if code is not None:
                digest.update(marshal.dumps(code))
            elif spec is not None and spec.origin:
                _update_file(digest, Path(spec.origin), module_name)
            else:
                digest.update(b"<unavailable>")
        except Exception as exc:
            digest.update(type(exc).__name__.encode("ascii", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def _iter_data_files(data_root: Path) -> Iterable[Path]:
    for relative in ("profiles", "modular"):
        directory = data_root / relative
        if directory.is_dir():
            yield from (
                path
                for path in sorted(directory.rglob("*"))
                if path.is_file() and path.suffix.casefold() in {".json", ".gz"}
            )
    for name in ("dragon_nest_data.json", "skill_adjustments.json"):
        path = data_root / name
        if path.is_file():
            yield path


@lru_cache(maxsize=16)
def _data_content_fingerprint_cached(
    data_root_text: str,
    manifest: tuple[tuple[str, int, int], ...],
) -> str:
    data_root = Path(data_root_text)
    digest = hashlib.sha256(FINGERPRINT_SCHEMA.encode("ascii"))
    for label, _size, _modified_ns in manifest:
        path = data_root / label
        _update_file(digest, path, label)
    return digest.hexdigest()


def data_content_fingerprint(data_root_text: str) -> str:
    """Hash rule-bearing data and notice replacements during one app run."""

    data_root = Path(data_root_text)
    manifest = tuple(
        (
            path.relative_to(data_root).as_posix(),
            int(path.stat().st_size),
            int(path.stat().st_mtime_ns),
        )
        for path in _iter_data_files(data_root)
    )
    return _data_content_fingerprint_cached(data_root_text, manifest)


@lru_cache(maxsize=16)
def _replay_decoder_fingerprint_cached(
    data_root_text: str, data_fingerprint: str
) -> str:
    digest = hashlib.sha256(b"replay-decoder-v1\0")
    digest.update(module_code_fingerprint(_REPLAY_MODULES).encode("ascii"))
    digest.update(data_fingerprint.encode("ascii"))
    return digest.hexdigest()


def replay_decoder_fingerprint(data_root_text: str) -> str:
    return _replay_decoder_fingerprint_cached(
        data_root_text, data_content_fingerprint(data_root_text)
    )


@lru_cache(maxsize=16)
def _team_score_rules_fingerprint_cached(
    data_root_text: str,
    replay_fingerprint: str,
    data_fingerprint: str,
) -> str:
    digest = hashlib.sha256(b"team-score-rules-v1\0")
    digest.update(replay_fingerprint.encode("ascii"))
    digest.update(module_code_fingerprint(_SCORE_MODULES).encode("ascii"))
    digest.update(data_fingerprint.encode("ascii"))
    return digest.hexdigest()


def team_score_rules_fingerprint(data_root_text: str) -> str:
    data_fingerprint = data_content_fingerprint(data_root_text)
    return _team_score_rules_fingerprint_cached(
        data_root_text,
        _replay_decoder_fingerprint_cached(data_root_text, data_fingerprint),
        data_fingerprint,
    )
