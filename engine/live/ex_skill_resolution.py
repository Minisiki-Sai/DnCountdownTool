from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .models import SkillCastEvent


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _name_key(value: object) -> str:
    return re.sub(r"\s+", "", _text(value)).casefold()


@dataclass(frozen=True, slots=True)
class ExSkillIdentity:
    """One final-job EX passive and the base action that activates it."""

    job: str
    base_skill_id: str
    base_skill_name: str
    ex_skill_id: str
    ex_skill_name: str


class ExSkillResolver:
    """Resolve base action packets to the EX form learned by a final job.

    Dragon Nest commonly keeps the base skill ID in the action packet after
    the EX passive has been learned.  The final-job skill tree is the stable
    source for this relationship: an EX row points at its base action through
    ``_ParentSkillID1``.  Resolution is job-scoped so inherited skills shared
    by two specialisations cannot leak an EX form to the wrong job.

    This resolver changes *cast/action identity only*.  Damage events retain
    their independently decoded packet/object/status identity; the action-hit
    fallback later publishes the configured canonical damage bucket.  That
    distinction is required for EX passives which modify the base hit instead
    of creating an independent damage source.
    """

    def __init__(self, identities: tuple[ExSkillIdentity, ...] = ()) -> None:
        self.identities = identities
        self._by_job_and_id: dict[tuple[str, str], ExSkillIdentity] = {}
        self._by_job_and_name: dict[tuple[str, str], ExSkillIdentity] = {}
        for row in identities:
            job_key = _text(row.job).casefold()
            if not job_key:
                continue
            self._by_job_and_id[(job_key, _text(row.base_skill_id))] = row
            self._by_job_and_name[(job_key, _name_key(row.base_skill_name))] = row

    @classmethod
    def from_full_skill_dir(cls, full_skill_dir: Path) -> "ExSkillResolver":
        jobs_dir = Path(full_skill_dir) / "jobs"
        identities: list[ExSkillIdentity] = []
        for path in sorted(jobs_dir.glob("job_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, TypeError, ValueError):
                continue
            skills = payload.get("skills")
            if not isinstance(skills, Mapping):
                continue
            tree = payload.get("tree")
            tree_rows = tree.get("tree") if isinstance(tree, Mapping) else ()
            job = ""
            if isinstance(tree_rows, list) and tree_rows:
                job_row = tree_rows[-1].get("job")
                if isinstance(job_row, Mapping):
                    job = _text(job_row.get("name"))
            if not job:
                continue

            parsed_skills: dict[str, Mapping[str, object]] = {
                _text(skill_id): row
                for skill_id, row in skills.items()
                if isinstance(row, Mapping)
            }
            for raw_ex_id, ex_row in parsed_skills.items():
                ex_name = _text(ex_row.get("name"))
                try:
                    skill_type = int(ex_row.get("_SkillType") or 0)
                    parent_id = int(ex_row.get("_ParentSkillID1") or 0)
                    ex_id = int(ex_row.get("_SkillTableID") or raw_ex_id)
                except (TypeError, ValueError):
                    continue
                if (
                    skill_type != 3
                    or parent_id <= 0
                    or not re.search(r"EX\s*$", ex_name, flags=re.IGNORECASE)
                ):
                    continue
                base_row = parsed_skills.get(str(parent_id))
                if base_row is None:
                    continue
                base_name = _text(base_row.get("name"))
                if not base_name:
                    continue
                identities.append(
                    ExSkillIdentity(
                        job=job,
                        base_skill_id=str(parent_id),
                        base_skill_name=base_name,
                        ex_skill_id=str(ex_id),
                        ex_skill_name=ex_name,
                    )
                )
        return cls(tuple(identities))

    def resolve(
        self, job: object, skill_id: object, skill_name: object
    ) -> ExSkillIdentity | None:
        job_key = _text(job).casefold()
        if not job_key:
            return None
        by_id = self._by_job_and_id.get((job_key, _text(skill_id)))
        by_name = self._by_job_and_name.get((job_key, _name_key(skill_name)))
        if by_id is not None and by_name is not None and by_id != by_name:
            return None
        return by_id or by_name

    def upgrade_cast(self, cast: SkillCastEvent) -> SkillCastEvent:
        identity = self.resolve(cast.job, cast.skill_id, cast.skill_name)
        if identity is None:
            return cast
        from dataclasses import replace
        return replace(cast, skill_id=identity.ex_skill_id, skill_name=identity.ex_skill_name)

