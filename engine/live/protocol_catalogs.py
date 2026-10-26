from __future__ import annotations

import codecs
import copy
import ctypes
import dataclasses
import hashlib
import itertools
import json
import math
import os
import queue
import re
import shutil
import socket
import sqlite3
import struct
import threading
import time
import traceback
import uuid
from bisect import bisect_left, bisect_right, insort
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .client_native_skill_catalog import (
    CLIENT_NATIVE_SKILL_CATALOG, CLIENT_NATIVE_SKILL_PROFILES,
)

@lru_cache(maxsize=4)
def _load_monster_templates(data_root_text: str) -> dict[int, _MonsterTemplate]:
    path = (
        Path(data_root_text)
        / "profiles"
        / "monster_templates.json"
    )
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig")).get(
            "templates", {}
        )
    except (OSError, ValueError, AttributeError):
        return {}
    result: dict[int, _MonsterTemplate] = {}
    # Named mechanic add, not a main Boss. HP/stats remain unknown rather
    # than inventing values; creation recognition is template-ID based.
    rows.setdefault("4237036", {"name": "牛头怪骑士", "boss": False})
    for template_id_text, value in rows.items():
        if not isinstance(value, dict):
            continue
        try:
            template_id = int(template_id_text)
            result[template_id] = _MonsterTemplate(
                template_id=template_id,
                name=str(value.get("name", "")).strip()
                or f"怪物模板 {template_id}",
                level=int(value.get("level") or 0),
                hp=int(value.get("hp") or 0),
                physical_defense=int(value.get("physical_defense") or 0),
                magic_defense=int(value.get("magic_defense") or 0),
                critical_resistance=int(value.get("critical_resistance") or 0),
                fire_resistance=float(value.get("fire_resistance") or 0),
                ice_resistance=float(value.get("ice_resistance") or 0),
                light_resistance=float(value.get("light_resistance") or 0),
                dark_resistance=float(value.get("dark_resistance") or 0),
                actor_table_id=int(value.get("actor_table_id") or 0),
                boss=bool(value.get("boss", False)),
            )
        except (TypeError, ValueError):
            continue
    return result

@lru_cache(maxsize=4)
def _load_boss_action_catalog(
    data_root_text: str,
) -> dict[tuple[int, int], tuple[str, bool]]:
    """Load calibrated Boss action names without guessing unmapped codes."""
    path = (
        Path(data_root_text)
        / "profiles"
        / "boss_actions.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    templates = data.get("templates", {}) if isinstance(data, dict) else {}
    result: dict[tuple[int, int], tuple[str, bool]] = {}
    for raw_template_id, raw_template in (
        templates.items() if isinstance(templates, dict) else ()
    ):
        if not isinstance(raw_template, dict):
            continue
        try:
            template_id = int(raw_template_id)
        except (TypeError, ValueError):
            continue
        actions = raw_template.get("actions", {})
        for raw_action_code, raw_action in (
            actions.items() if isinstance(actions, dict) else ()
        ):
            try:
                action_code = int(raw_action_code)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_action, dict):
                name = str(raw_action.get("name") or "").strip()
                mapped = bool(raw_action.get("mapped", bool(name)))
            else:
                name = str(raw_action or "").strip()
                mapped = bool(name)
            result[(template_id, action_code)] = (name, mapped)
    return result

@lru_cache(maxsize=4)
def _load_boss_skill_catalog(data_root_text: str) -> dict[int, str]:
    """Load verified monster skill indices separately from runtime actions."""
    path = (
        Path(data_root_text)
        / "profiles"
        / "boss_skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    rows = data.get("skills", {}) if isinstance(data, dict) else {}
    result: dict[int, str] = {}
    for raw_skill_index, raw_skill in (
        rows.items() if isinstance(rows, dict) else ()
    ):
        try:
            skill_index = int(raw_skill_index)
        except (TypeError, ValueError):
            continue
        if isinstance(raw_skill, dict):
            name = str(raw_skill.get("name") or "").strip()
        else:
            name = str(raw_skill or "").strip()
        if skill_index > 0 and name:
            result[skill_index] = name
    return result

@lru_cache(maxsize=4)
def _load_player_buff_clear_boss_skill_ids(
    data_root_text: str,
) -> frozenset[int]:
    """Load monster statuses that authoritatively clear finite player buffs."""
    path = (
        Path(data_root_text)
        / "profiles"
        / "boss_skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return frozenset()
    rows = data.get("skills", {}) if isinstance(data, dict) else {}
    result: set[int] = set()
    for raw_skill_index, raw_skill in (
        rows.items() if isinstance(rows, dict) else ()
    ):
        if not isinstance(raw_skill, dict) or not raw_skill.get(
            "clears_player_buffs", False
        ):
            continue
        try:
            skill_index = int(raw_skill_index)
        except (TypeError, ValueError):
            continue
        if skill_index > 0:
            result.add(skill_index)
    effect_catalog_path = (
        Path(data_root_text)
        / "profiles"
        / "monster_buff_clear_skills.json"
    )
    try:
        effect_catalog = json.loads(
            effect_catalog_path.read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        effect_catalog = {}
    raw_skill_ids = (
        effect_catalog.get("skill_ids", [])
        if isinstance(effect_catalog, dict)
        else []
    )
    for raw_skill_id in raw_skill_ids if isinstance(raw_skill_ids, list) else ():
        try:
            skill_id = int(raw_skill_id)
        except (TypeError, ValueError):
            continue
        if skill_id > 0:
            result.add(skill_id)
    return frozenset(result)

@lru_cache(maxsize=4)
def _load_skill_catalog(data_root_text: str) -> dict[int, tuple[str, str, int]]:
    data_root = Path(data_root_text)
    tiny_dir = data_root / "modular" / "tables" / "skills" / "skill_damage_tiny_jobs"
    catalog: dict[int, tuple[str, str, int]] = {}
    for path in sorted(tiny_dir.glob("job_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        job = data.get("j", [])
        job_name = str(job[1]).strip() if isinstance(job, list) and len(job) > 1 else ""
        for value in data.get("n", []):
            if not isinstance(value, list) or len(value) < 2:
                continue
            try:
                skill_id = int(value[0])
            except (TypeError, ValueError):
                continue
            name = str(value[1]).strip()
            if name and skill_id not in catalog:
                catalog[skill_id] = (name, job_name, 3)

    jobs_dir = (
        data_root
        / "modular"
        / "tables"
        / "skills"
        / "dngamer_skill_sim_reborn_split"
        / "jobs"
    )
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        sections = data.get("tree", {}).get("tree", [])
        if not isinstance(sections, list):
            continue
        for section in sections:
            if not isinstance(section, dict):
                continue
            job = section.get("job", {})
            job_name = str(job.get("name", "")).strip()
            try:
                job_index = int(job.get("index", 99))
            except (TypeError, ValueError):
                job_index = 99
            for value in section.get("skill", []):
                if not isinstance(value, dict):
                    continue
                try:
                    skill_id = int(value.get("_SkillTableID") or value.get("_SkillID"))
                except (TypeError, ValueError):
                    continue
                name = str(value.get("name", "")).strip()
                if not name:
                    continue
                existing = catalog.get(skill_id)
                if existing is None:
                    catalog[skill_id] = (name, job_name, job_index)
    for skill_id, row in CLIENT_NATIVE_SKILL_CATALOG.items():
        catalog.setdefault(skill_id, row)
    return catalog

@lru_cache(maxsize=4)
def _load_damage_skill_ids(data_root_text: str) -> frozenset[int]:
    """Return skills that have an actual damage entry in the simulator data.

    Compound v576 hit packets contain many unrelated integers. Some of those
    integers happen to equal utility skill IDs from the attacker's own class
    tree, so the cross-job guard alone cannot reject them. The tiny damage
    tables are a stricter source of truth: a utility skill such as Aerial
    Evasion may be castable by the job, but it cannot own a damage record.
    """
    tiny_dir = (
        Path(data_root_text)
        / "modular"
        / "tables"
        / "skills"
        / "skill_damage_tiny_jobs"
    )
    skill_ids: set[int] = set()
    for path in sorted(tiny_dir.glob("job_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for value in data.get("n", []):
            if not isinstance(value, list) or not value:
                continue
            try:
                skill_ids.add(int(value[0]))
            except (TypeError, ValueError):
                continue
    return frozenset(skill_ids)

@lru_cache(maxsize=4)
def _load_status_skill_catalog(data_root_text: str) -> dict[int, str]:
    """Load v576 internal status-skill names without polluting hit attribution.

    Internal summon/status IDs are valid in server status records but are not
    player-cast damage skills. Keeping this as a separate catalog prevents an
    unrelated integer in a damage packet from being mistaken for one of them.
    """
    path = (
        Path(data_root_text)
        / "profiles"
        / "status_skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    rows = data.get("skills", {}) if isinstance(data, dict) else {}
    result: dict[int, str] = {}
    for skill_id, name in rows.items() if isinstance(rows, dict) else ():
        try:
            numeric_id = int(skill_id)
        except (TypeError, ValueError):
            continue
        clean_name = str(name or "").strip()
        if clean_name:
            result[numeric_id] = clean_name
    return result

@lru_cache(maxsize=4)
def _load_team_buff_skill_names(data_root_text: str) -> frozenset[str]:
    """Load simulator-owned team-buff names for live status classification."""
    data_root = Path(data_root_text)
    path = data_root / "modular" / "tables" / "skill_buff.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    names: set[str] = set()
    for key in data:
        parts = str(key).split("|")
        if len(parts) >= 2 and parts[0] == "团队buff":
            names.add(_summon_skill_key(parts[-1]))
    # Some party effects live in the manually maintained simulator mechanics
    # rather than skill_buff.json. Keep their live classification data-driven
    # so an observed status is not mislabeled as a personal buff.
    display_path = (
        data_root
        / "profiles"
        / "player_buff_display_catalog.json"
    )
    try:
        display_data = json.loads(
            display_path.read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        display_data = {}
    display_rows = (
        display_data.get("buffs", [])
        if isinstance(display_data, dict)
        else []
    )
    for row in display_rows if isinstance(display_rows, list) else ():
        if not isinstance(row, dict) or row.get("scope") != "team":
            continue
        skill_name = str(row.get("skill", "")).strip()
        if skill_name:
            names.add(_summon_skill_key(skill_name))
    # These two are modeled by the cooldown scheduler rather than skill_buff,
    # but their live statuses are still party-wide buffs.
    names.update({_summon_skill_key("精神爆发"), _summon_skill_key("时间加速")})
    return frozenset(names)

@lru_cache(maxsize=4)
def _load_status_effect_catalog(
    data_root_text: str,
) -> dict[int, _StatusEffectDefinition]:
    path = (
        Path(data_root_text)
        / "profiles"
        / "status_effect_catalog.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    groups = data.get("groups", {}) if isinstance(data, dict) else {}
    statuses = data.get("statuses", {}) if isinstance(data, dict) else {}
    if not isinstance(groups, dict) or not isinstance(statuses, dict):
        return {}

    result: dict[int, _StatusEffectDefinition] = {}
    for raw_skill_id, raw_status in statuses.items():
        if not isinstance(raw_status, dict):
            continue
        try:
            skill_id = int(raw_skill_id)
            group_id = str(int(raw_status.get("group") or 0))
        except (TypeError, ValueError):
            continue
        group = groups.get(group_id, {})
        if not isinstance(group, dict):
            continue
        rule = group.get("rule", {})
        if not isinstance(rule, dict):
            rule = {}
        try:
            raw_value = raw_status.get("fallback_value")
            fallback_value = (
                float(raw_value) if raw_value is not None else None
            )
        except (TypeError, ValueError):
            fallback_value = None
        try:
            raw_duration = raw_status.get("fallback_duration_ms")
            fallback_duration = (
                int(raw_duration) if raw_duration is not None else None
            )
        except (TypeError, ValueError):
            fallback_duration = None
        display_name = str(group.get("display_name") or "").strip()
        if not display_name:
            continue
        result[skill_id] = _StatusEffectDefinition(
            display_name=display_name,
            source_type="equipment_suffix",
            rule_key=str(rule.get("key") or "").strip(),
            fallback_value=fallback_value,
            fallback_duration_ms=fallback_duration,
            stackable=bool(rule.get("stackable", False)),
            stack_group=str(rule.get("group") or display_name).strip(),
        )
    return result

@lru_cache(maxsize=4)
def _load_periodic_damage_catalog(
    data_root_text: str,
) -> dict[int, tuple[_PeriodicDamageRule, ...]]:
    path = (
        Path(data_root_text)
        / "profiles"
        / "periodic_damage_skills.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    rows = data.get("skills", {}) if isinstance(data, dict) else {}
    result: dict[int, tuple[_PeriodicDamageRule, ...]] = {}
    for raw_skill_id, raw_rules in rows.items() if isinstance(rows, dict) else ():
        try:
            skill_id = int(raw_skill_id)
        except (TypeError, ValueError):
            continue
        parsed: list[_PeriodicDamageRule] = []
        for raw_rule in raw_rules if isinstance(raw_rules, list) else ():
            if not isinstance(raw_rule, dict):
                continue
            try:
                source_skill_id = int(raw_rule.get("source_skill_id") or skill_id)
                effect_class = int(raw_rule.get("effect_class") or 0)
            except (TypeError, ValueError):
                continue
            variants: set[tuple[str, int]] = set()
            for variant in raw_rule.get("variants", ()):
                if not isinstance(variant, dict):
                    continue
                try:
                    duration_ms = int(variant.get("duration_ms") or 0)
                except (TypeError, ValueError):
                    continue
                parameters = _normalize_status_parameters(
                    str(variant.get("parameters") or "")
                )
                if parameters and duration_ms > 0:
                    variants.add((parameters, duration_ms))
            source_skill_name = str(
                raw_rule.get("source_skill_name") or source_skill_id
            ).strip()
            kind = str(raw_rule.get("kind") or "periodic").strip()
            label = str(raw_rule.get("label") or "持续伤害").strip()
            parsed.append(
                _PeriodicDamageRule(
                    source_skill_id=source_skill_id,
                    source_skill_name=source_skill_name,
                    kind=kind,
                    label=label,
                    effect_class=effect_class,
                    variants=frozenset(variants),
                )
            )
        if parsed:
            result[skill_id] = tuple(parsed)
    return result

@lru_cache(maxsize=4)
def _load_skill_profiles(data_root_text: str) -> dict[int, frozenset[str]]:
    """Return the second-job trees in which each numeric skill ID is valid.

    Compound combat packets contain many unrelated integers. A value can
    accidentally equal a real skill ID from another class, so global catalog
    membership alone is not sufficient evidence that it owns the hit.
    """
    data_root = Path(data_root_text)
    jobs_dir = (
        data_root
        / "modular"
        / "tables"
        / "skills"
        / "dngamer_skill_sim_reborn_split"
        / "jobs"
    )
    profiles: dict[int, set[str]] = defaultdict(set)
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        profile_id = path.stem
        sections = data.get("tree", {}).get("tree", [])
        if not isinstance(sections, list):
            continue
        for section in sections:
            if not isinstance(section, dict):
                continue
            for value in section.get("skill", []):
                if not isinstance(value, dict):
                    continue
                try:
                    skill_id = int(value.get("_SkillTableID") or value.get("_SkillID"))
                except (TypeError, ValueError):
                    continue
                profiles[skill_id].add(profile_id)
    for skill_id, profile_ids in CLIENT_NATIVE_SKILL_PROFILES.items():
        profiles[skill_id].update(profile_ids)
    return {
        skill_id: frozenset(profile_ids)
        for skill_id, profile_ids in profiles.items()
    }

@lru_cache(maxsize=4)
def _load_profile_jobs(data_root_text: str) -> dict[str, str]:
    jobs_dir = (
        Path(data_root_text)
        / "modular"
        / "tables"
        / "skills"
        / "dngamer_skill_sim_reborn_split"
        / "jobs"
    )
    result: dict[str, str] = {}
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        sections = data.get("tree", {}).get("tree", [])
        if not isinstance(sections, list):
            continue
        names = [
            str(section.get("job", {}).get("name", "")).strip()
            for section in sections
            if isinstance(section, dict)
        ]
        names = [name for name in names if name]
        if names:
            result[path.stem] = names[-1]
    return result

@lru_cache(maxsize=4)
def _load_inspect_job_codes(data_root_text: str) -> dict[int, tuple[str, int]]:
    """Map the unpacked `_NeedJobClass` code to its most specific job."""
    jobs_dir = (
        Path(data_root_text)
        / "modular"
        / "tables"
        / "skills"
        / "dngamer_skill_sim_reborn_split"
        / "jobs"
    )
    result: dict[int, tuple[str, int]] = {}
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        sections = data.get("tree", {}).get("tree", [])
        for section in sections if isinstance(sections, list) else []:
            if not isinstance(section, dict):
                continue
            job = section.get("job", {})
            try:
                job_code = int(job.get("id", 0))
                job_index = int(job.get("index", 99))
            except (TypeError, ValueError):
                continue
            job_name = str(job.get("name", "")).strip()
            previous = result.get(job_code)
            if job_code > 0 and job_name and (
                previous is None or job_index < previous[1]
            ):
                result[job_code] = (job_name, job_index)
    return result

@lru_cache(maxsize=4)
def _load_profile_lineages(
    data_root_text: str,
) -> dict[str, tuple[tuple[str, int], ...]]:
    """Return every base/first/second job name carried by each skill tree."""
    jobs_dir = (
        Path(data_root_text)
        / "modular"
        / "tables"
        / "skills"
        / "dngamer_skill_sim_reborn_split"
        / "jobs"
    )
    result: dict[str, tuple[tuple[str, int], ...]] = {}
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        rows: list[tuple[str, int]] = []
        sections = data.get("tree", {}).get("tree", [])
        for section in sections if isinstance(sections, list) else []:
            if not isinstance(section, dict):
                continue
            job = section.get("job", {})
            name = str(job.get("name", "")).strip()
            try:
                index = int(job.get("index", 99))
            except (TypeError, ValueError):
                index = 99
            if name:
                rows.append((name, index))
        if rows:
            result[path.stem] = tuple(rows)
    return result


def bind_host_globals(host_globals: dict[str, object]) -> None:
    protected = {'_load_monster_templates', '_load_status_skill_catalog', '_load_player_buff_clear_boss_skill_ids', '_load_damage_skill_ids', '_load_skill_profiles', '_load_periodic_damage_catalog', '_load_profile_jobs', '_load_skill_catalog', '_load_status_effect_catalog', '_load_inspect_job_codes', '_load_boss_action_catalog', '_load_boss_skill_catalog', 'bind_host_globals', '_load_profile_lineages', '_load_team_buff_skill_names'}
    for name, value in host_globals.items():
        if name.startswith('__') or name in protected:
            continue
        globals()[name] = value


# Extracted from sources.py; algorithm bodies are unchanged.
