from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Generic, Hashable, Iterable, TypeVar


@dataclass(frozen=True, slots=True)
class DebuffEvidenceKey:
    """Stable identity for one observed Boss status packet family."""

    job: str
    skill_id: int
    status_code: str
    parameter_structure: str

    def display(self) -> str:
        return (
            f"{self.job}|{self.skill_id}|{self.status_code}|"
            f"{self.parameter_structure}"
        )


def status_parameter_structure(parameters: str) -> str:
    """Describe packet parameter shape without keying on volatile values."""

    raw = str(parameters or "")
    if not raw:
        return "0:"

    kinds: list[str] = []
    for raw_token in raw.split(";"):
        token = raw_token.strip()
        if not token:
            kinds.append("empty")
            continue
        try:
            number = float(token)
        except ValueError:
            kinds.append("text")
            continue
        if not math.isfinite(number):
            kinds.append("nonfinite")
        elif abs(number) < 1e-12:
            kinds.append("zero")
        elif number.is_integer():
            kinds.append("pos-int" if number > 0 else "neg-int")
        else:
            kinds.append("pos-float" if number > 0 else "neg-float")
    return f"{len(kinds)}:{','.join(kinds)}"


def build_debuff_evidence_key(
    *,
    job: str,
    skill_id: int,
    status_code: str,
    parameters: str,
) -> DebuffEvidenceKey:
    normalized_job = "".join(str(job or "").strip().split()) or "未知职业"
    normalized_code = str(status_code or "").strip().casefold() or "unknown"
    return DebuffEvidenceKey(
        job=normalized_job,
        skill_id=max(0, int(skill_id or 0)),
        status_code=normalized_code,
        parameter_structure=status_parameter_structure(parameters),
    )


T = TypeVar("T")


class DebuffEvidenceRegistry(Generic[T]):
    """Resolve repeated statuses by their four-part evidence identity.

    The first occurrence is translated by the versioned semantic catalog. All
    later occurrences use the evidence key. If one key ever resolves to a
    different semantic fingerprint, it is quarantined instead of guessing.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self._mapping: dict[DebuffEvidenceKey, tuple[tuple[Hashable, ...], ...]] = {}
        self._conflicts: set[DebuffEvidenceKey] = set()
        self.seeded = 0
        self.hits = 0
        self.conflicts = 0

    def resolve(
        self,
        evidence: DebuffEvidenceKey,
        candidates: Iterable[T],
        *,
        fingerprint: Callable[[T], Iterable[Hashable]],
    ) -> tuple[T, ...]:
        values = tuple(candidates)
        if not self.enabled:
            return values
        signatures = tuple(sorted((tuple(fingerprint(value)) for value in values)))
        if evidence in self._conflicts:
            return ()

        previous = self._mapping.get(evidence)
        if previous is None:
            self._mapping[evidence] = signatures
            self.seeded += 1
            return values
        if previous == signatures:
            self.hits += 1
            return values

        self._conflicts.add(evidence)
        self._mapping.pop(evidence, None)
        self.conflicts += 1
        return ()

    @property
    def key_count(self) -> int:
        return len(self._mapping)

    @property
    def conflict_keys(self) -> frozenset[DebuffEvidenceKey]:
        return frozenset(self._conflicts)
