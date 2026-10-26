from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class NestProfile:
    key: str
    label: str
    display_name: str
    hints: tuple[str, ...]
    entry_template_ids: frozenset[int]
    template_ids: frozenset[int]
    boss_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NestResolution:
    profile: NestProfile
    confidence: str
    template_id: int = 0


def _bundled_nest_profile_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "nest_profiles.json"
    )


def normalize_nest_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[·•・._\-—:：,，()（）\[\]【】]", "", text)


def canonical_nest_name(value: object) -> str:
    """Return the score/database name for an exact map display name.

    Existing score cohorts use the short profile label (for example
    ``黑龙经典``), while newly decoded maps expose the official display name.
    Exact aliases are collapsed here so the live badge, score benchmark and
    upload pipeline all use one stable value without splitting old samples.
    Maps not covered by the historical profile table keep their unpacked
    display name verbatim.
    """

    text = str(value or "").strip()
    normalized = normalize_nest_text(text)
    if not normalized:
        return ""
    for profile in load_nest_profiles():
        aliases = {
            normalize_nest_text(alias)
            for alias in (
                profile.label,
                profile.display_name,
                *profile.hints,
            )
            if str(alias or "").strip()
        }
        if normalized in aliases:
            return profile.label
    return text


def _positive_ints(values: object) -> frozenset[int]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    result: set[int] = set()
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(parsed)
    return frozenset(result)


@lru_cache(maxsize=1)
def load_nest_profiles() -> tuple[NestProfile, ...]:
    try:
        payload = json.loads(
            _bundled_nest_profile_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ()
    raw_profiles = payload.get("profiles", ()) if isinstance(payload, dict) else ()
    profiles: list[NestProfile] = []
    for row in raw_profiles:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", "") or "").strip()
        label = str(row.get("label", "") or "").strip()
        display_name = str(row.get("display_name", "") or label).strip()
        template_ids = _positive_ints(row.get("template_ids", ()))
        entry_template_ids = _positive_ints(
            row.get("entry_template_ids", ())
        ) & template_ids
        if not key or not label or not display_name or not template_ids:
            continue
        profiles.append(
            NestProfile(
                key=key,
                label=label,
                display_name=display_name,
                hints=tuple(
                    str(value).strip()
                    for value in row.get("hints", ())
                    if str(value).strip()
                ),
                entry_template_ids=entry_template_ids,
                template_ids=template_ids,
                boss_names=tuple(
                    str(value).strip()
                    for value in row.get("boss_names", ())
                    if str(value).strip()
                ),
            )
        )
    return tuple(profiles)


@lru_cache(maxsize=2)
def _template_profile_index(
    entry_only: bool,
) -> dict[int, tuple[NestProfile, ...]]:
    mutable: dict[int, list[NestProfile]] = {}
    for profile in load_nest_profiles():
        template_ids = (
            profile.entry_template_ids if entry_only else profile.template_ids
        )
        for template_id in template_ids:
            mutable.setdefault(template_id, []).append(profile)
    return {
        template_id: tuple(profiles)
        for template_id, profiles in mutable.items()
    }


def resolve_nest_template(template_id: object) -> NestResolution | None:
    """Resolve one formal-map entity without guessing shared templates.

    ``21 0C`` is a generic scene resource synchronization and carries no map
    identity, so it is deliberately absent here.  Recognition starts only
    after the formal map emits its 88-byte ``06 27 55 00``
    monster/environment spawn records.
    """

    try:
        parsed = int(template_id)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    entry_matches = _template_profile_index(True).get(parsed, ())
    if len(entry_matches) == 1:
        return NestResolution(entry_matches[0], "entry_template", parsed)
    matches = _template_profile_index(False).get(parsed, ())
    if len(matches) == 1:
        return NestResolution(matches[0], "template", parsed)
    return None


def _target_template_id(value: object) -> int:
    if isinstance(value, int):
        return value if value > 0 else 0
    text = str(value or "").strip()
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    return int(text) if text.isdigit() else 0


def _single_profile_from_templates(
    template_ids: set[int], *, entry_only: bool
) -> NestResolution | None:
    evidence: Counter[str] = Counter()
    profiles_by_key: dict[str, NestProfile] = {}
    first_template: dict[str, int] = {}
    index = _template_profile_index(entry_only)
    for template_id in template_ids:
        matches = index.get(template_id, ())
        # A template shared by multiple dungeons is evidence for neither.
        if len(matches) != 1:
            continue
        profile = matches[0]
        evidence[profile.key] += 1
        profiles_by_key[profile.key] = profile
        first_template.setdefault(profile.key, template_id)
    if len(evidence) != 1:
        return None
    key = next(iter(evidence))
    return NestResolution(
        profiles_by_key[key],
        "entry_template" if entry_only else "template_set",
        first_template[key],
    )


def resolve_nest_evidence(
    boss_names: Iterable[object] = (),
    *,
    capture_hint: object = "",
    target_ids: Iterable[object] = (),
    template_ids: Iterable[object] = (),
) -> NestResolution | None:
    """Resolve a dungeon from packet evidence, then names, then filename.

    Conflicting packet evidence is treated as an ambiguous multi-run capture;
    it is never hidden by a weak filename guess.
    """

    observed_template_ids = {
        parsed
        for value in (*tuple(target_ids), *tuple(template_ids))
        if (parsed := _target_template_id(value)) > 0
    }
    entry_resolution = _single_profile_from_templates(
        observed_template_ids, entry_only=True
    )
    if entry_resolution is not None:
        return entry_resolution
    template_resolution = _single_profile_from_templates(
        observed_template_ids, entry_only=False
    )
    if template_resolution is not None:
        return template_resolution

    profiles = load_nest_profiles()
    observed_names = {
        normalized
        for value in boss_names
        if (normalized := normalize_nest_text(value))
        and "未识别" not in normalized
    }
    name_profiles: dict[str, set[str]] = {}
    name_frequency: Counter[str] = Counter()
    for profile in profiles:
        names = {
            normalized
            for value in profile.boss_names
            if (normalized := normalize_nest_text(value))
        }
        name_profiles[profile.key] = names
        name_frequency.update(names)
    name_matches = [
        profile
        for profile in profiles
        if any(
            name_frequency[name] == 1
            for name in observed_names & name_profiles.get(profile.key, set())
        )
    ]
    if len(name_matches) == 1:
        return NestResolution(name_matches[0], "boss_name")
    if len(name_matches) > 1:
        return None

    hint = normalize_nest_text(capture_hint)
    ranked: list[tuple[int, NestProfile]] = []
    for profile in profiles:
        score = max(
            (
                len(normalized)
                for value in (*profile.hints, profile.display_name, profile.label)
                if (normalized := normalize_nest_text(value))
                and normalized in hint
            ),
            default=0,
        )
        if score > 0:
            ranked.append((score, profile))
    ranked.sort(key=lambda row: row[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return None
    return NestResolution(ranked[0][1], "capture_hint")
