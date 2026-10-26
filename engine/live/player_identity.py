from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import re
from typing import Any, Iterable, Mapping


_HEX_ID = re.compile(r"^0x[0-9A-Fa-f]{8,16}$")
_GENERIC_NAME = re.compile(r"^(?:角色\s*)?0x[0-9A-Fa-f]+$|^(?:未知职业|未知角色)$")


def _text(value: object) -> str:
    return str(value or "").strip()


def _value(row: object, key: str, default: object = "") -> object:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _canonical_id(value: object) -> str:
    text = _text(value)
    if _HEX_ID.fullmatch(text):
        return f"0x{text[2:].upper()}"
    return text


def _usable_hex_id(value: object) -> str:
    canonical = _canonical_id(value)
    if not _HEX_ID.fullmatch(canonical):
        return ""
    try:
        return canonical if int(canonical, 16) > 0 else ""
    except ValueError:
        return ""


def _usable_name(value: object) -> str:
    text = _text(value)
    if not text or _GENERIC_NAME.fullmatch(text):
        return ""
    return text


def _profile_fields(row: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _value(row, "fields", ()) or ():
        if isinstance(item, Mapping):
            key = _text(item.get("key") or item.get("name"))
            value = _text(item.get("value"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            key, value = _text(item[0]), _text(item[1])
        else:
            continue
        if key:
            result[key] = value
    return result


def stable_profile_id(row: object) -> str:
    fields = _profile_fields(row)
    for key in ("角色档案标识", "稳定档案ID", "profile_id"):
        value = _usable_hex_id(fields.get(key, ""))
        if value:
            return value
    target_id = _text(_value(row, "target_id"))
    match = re.search(r"profile:(0x[0-9A-Fa-f]{8,16})", target_id)
    return _usable_hex_id(match.group(1)) if match else ""


def profile_source_id(row: object) -> str:
    fields = _profile_fields(row)
    for key in ("实时ID", "角色实时ID", "source_id"):
        value = _usable_hex_id(fields.get(key, ""))
        if value:
            return value
    return _usable_hex_id(_value(row, "target_id"))


@dataclass(frozen=True, slots=True)
class OcrPartyMember:
    slot: int
    character_name: str
    job: str = ""
    name_confidence: float = 0.0
    job_confidence: float = 0.0

    @classmethod
    def from_value(cls, value: object) -> "OcrPartyMember | None":
        name = _usable_name(_value(value, "characterName")) or _usable_name(
            _value(value, "character_name")
        )
        if not name:
            return None
        return cls(
            slot=max(0, int(float(_value(value, "slot", 0) or 0))),
            character_name=name,
            job=_text(_value(value, "job")),
            name_confidence=max(
                0.0,
                min(1.0, float(_value(value, "nameConfidence", 0.0) or 0.0)),
            ),
            job_confidence=max(
                0.0,
                min(1.0, float(_value(value, "jobConfidence", 0.0) or 0.0)),
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedPlayerIdentity:
    source_id: str
    job: str
    character_name: str = ""
    profile_id: str = ""
    party_position: int = 0
    status: str = "unresolved"
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    upload_allowed: bool = False
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["sourceId"] = data.pop("source_id")
        data["characterName"] = data.pop("character_name")
        data["profileId"] = data.pop("profile_id")
        data["partyPosition"] = data.pop("party_position")
        data["uploadAllowed"] = data.pop("upload_allowed")
        data["evidence"] = list(data["evidence"])
        return data


def resolve_player_identities(
    players: Iterable[object],
    party_members: Iterable[object],
    profiles: Iterable[object],
    ocr_members: Iterable[object] = (),
) -> tuple[ResolvedPlayerIdentity, ...]:
    """Resolve names, session profile aliases and combat source IDs conservatively.

    Direct actor evidence always wins.  Job-only matching is accepted only when
    a job occurs exactly once on both sides.  Duplicate jobs therefore fail
    closed unless a source ID, party position or exact already-proven name makes
    the pairing unique.
    """

    player_rows = list(players)
    sources: dict[str, dict[str, Any]] = {}
    for row in player_rows:
        source_id = _canonical_id(_value(row, "source_id") or _value(row, "sourceId"))
        if not _HEX_ID.fullmatch(source_id):
            continue
        sources[source_id] = {
            "job": _text(_value(row, "job")),
            "name": _usable_name(_value(row, "name")),
            "profile_id": "",
            "position": 0,
            "evidence": [],
            "confidence": 0.0,
        }

    party_by_source: dict[str, object] = {}
    for row in party_members:
        source_id = _canonical_id(_value(row, "actor_id") or _value(row, "actorId"))
        if not _HEX_ID.fullmatch(source_id):
            continue
        sources.setdefault(
            source_id,
            {
                "job": _text(_value(row, "job")),
                "name": _usable_name(_value(row, "name")),
                "profile_id": "",
                "position": 0,
                "evidence": [],
                "confidence": 0.0,
            },
        )
        party_by_source[source_id] = row
        candidate = sources[source_id]
        name = _usable_name(_value(row, "name"))
        job = _text(_value(row, "job"))
        if name:
            candidate["name"] = name
            candidate["evidence"].append("party_actor_name")
        if job:
            candidate["job"] = job
            candidate["evidence"].append("party_actor_job")
        candidate["position"] = max(
            0, int(float(_value(row, "party_position", 0) or 0))
        )
        candidate["confidence"] = max(
            candidate["confidence"], float(_value(row, "confidence", 0.0) or 0.0)
        )

    profiles_by_job: dict[str, list[object]] = defaultdict(list)
    unmatched_profiles: list[object] = []
    for profile in profiles:
        job = _text(_value(profile, "job"))
        if job:
            profiles_by_job[job].append(profile)
        source_id = profile_source_id(profile)
        if _HEX_ID.fullmatch(source_id):
            sources.setdefault(
                source_id,
                {
                    "job": job,
                    "name": _usable_name(_value(profile, "target_name")),
                    "profile_id": "",
                    "position": 0,
                    "evidence": [],
                    "confidence": 0.0,
                },
            )
        if source_id in sources:
            candidate = sources[source_id]
            profile_id = stable_profile_id(profile)
            name = _usable_name(_value(profile, "target_name"))
            if profile_id:
                candidate["profile_id"] = profile_id
                candidate["evidence"].append("inspect_direct_profile")
            if name:
                candidate["name"] = name
                candidate["evidence"].append("inspect_direct_name")
            candidate["confidence"] = max(candidate["confidence"], 0.99)
        else:
            unmatched_profiles.append(profile)

    ocr_rows = [
        parsed
        for value in ocr_members
        if (parsed := OcrPartyMember.from_value(value)) is not None
    ]
    sources_by_job: dict[str, list[str]] = defaultdict(list)
    for source_id, candidate in sources.items():
        if candidate["job"]:
            sources_by_job[candidate["job"]].append(source_id)

    # Exact names from already proven party packets are safe even for duplicate jobs.
    for ocr in ocr_rows:
        name_matches = [
            source_id
            for source_id, candidate in sources.items()
            if candidate["name"] and candidate["name"] == ocr.character_name
            and (not ocr.job or not candidate["job"] or candidate["job"] == ocr.job)
        ]
        position_matches = [
            source_id
            for source_id, candidate in sources.items()
            if ocr.slot > 0 and candidate["position"] == ocr.slot
            and (not ocr.job or not candidate["job"] or candidate["job"] == ocr.job)
        ]
        # Party row positions help the UI explain a likely match, but are not a
        # stable identity proof: one missed OCR row shifts every following slot.
        # Therefore position is only usable when the job is unique in this party.
        safe_position_matches = (
            position_matches
            if ocr.job and len(sources_by_job.get(ocr.job, ())) == 1
            else []
        )
        matches = name_matches or safe_position_matches
        if len(matches) == 1:
            candidate = sources[matches[0]]
            candidate["name"] = ocr.character_name
            candidate["position"] = candidate["position"] or ocr.slot
            candidate["evidence"].append(
                "ocr_exact_name" if name_matches else "ocr_party_position"
            )
            candidate["confidence"] = max(candidate["confidence"], ocr.name_confidence)
            continue
        if ocr.job and len(sources_by_job.get(ocr.job, ())) == 1:
            candidate = sources[sources_by_job[ocr.job][0]]
            candidate["name"] = ocr.character_name
            candidate["position"] = candidate["position"] or ocr.slot
            candidate["evidence"].append("ocr_unique_job")
            candidate["confidence"] = max(
                candidate["confidence"], min(ocr.name_confidence, ocr.job_confidence)
            )

    # Bind inspect-only profile aliases by exact name, then by a unique job.
    for profile in unmatched_profiles:
        job = _text(_value(profile, "job"))
        name = _usable_name(_value(profile, "target_name"))
        exact = [
            source_id
            for source_id, candidate in sources.items()
            if name and candidate["name"] == name
            and (not job or not candidate["job"] or candidate["job"] == job)
        ]
        candidates = exact or list(sources_by_job.get(job, ()))
        if len(candidates) != 1:
            continue
        source_id = candidates[0]
        candidate = sources[source_id]
        profile_id = stable_profile_id(profile)
        if profile_id:
            candidate["profile_id"] = profile_id
            candidate["evidence"].append(
                "inspect_exact_name" if exact else "inspect_unique_job"
            )
            candidate["confidence"] = max(candidate["confidence"], 0.96 if exact else 0.82)

    job_counts = Counter(candidate["job"] for candidate in sources.values() if candidate["job"])
    results: list[ResolvedPlayerIdentity] = []
    for source_id, candidate in sources.items():
        job = candidate["job"]
        duplicate_job = bool(job and job_counts[job] > 1)
        evidence = tuple(dict.fromkeys(candidate["evidence"]))
        has_direct_identity = bool(
            {
                "party_actor_name",
                "inspect_direct_profile",
                "inspect_exact_name",
                "ocr_exact_name",
            }
            & set(evidence)
        )
        ambiguous = duplicate_job and not has_direct_identity
        if ambiguous:
            status = "ambiguous"
            reason = "队伍存在同职业，当前证据无法唯一对应角色名、profile_id 与 source_id"
        elif candidate["profile_id"] and candidate["name"]:
            status = "confirmed"
            reason = "本场角色名、档案别名和战斗实时 ID 已形成唯一对应"
        elif candidate["name"] or candidate["profile_id"]:
            status = "partial"
            reason = "已取得部分身份，等待查看信息或队伍 OCR 补齐"
        else:
            status = "unresolved"
            reason = "尚未取得可验证的角色名或本场 profileId"
        upload_allowed = bool(
            not ambiguous
            and job
            and job != "未知职业"
            and (has_direct_identity or candidate["profile_id"] or job_counts[job] == 1)
        )
        results.append(
            ResolvedPlayerIdentity(
                source_id=source_id,
                job=job,
                character_name=candidate["name"],
                profile_id=candidate["profile_id"],
                party_position=candidate["position"],
                status=status,
                confidence=min(1.0, max(0.0, candidate["confidence"])),
                evidence=evidence,
                upload_allowed=upload_allowed,
                reason=reason,
            )
        )
    return tuple(sorted(results, key=lambda row: (row.party_position or 99, row.source_id)))
