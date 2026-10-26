"""Snapshot-only cooldown evidence for score preparation, not packet readers.

Short cast spacing is never sufficient by itself: charge stock, finite reset
credits and cooldown modifiers must first explain as much of it as possible.
The model deliberately gives an unplated skill every initial charge, even when
capture began mid-fight. Missing casts then weaken evidence, not invent plates.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from statistics import median
from typing import Any

from ..data_loader import as_number, norm
from .skill_identity import canonical_skill


MIN_INDEPENDENT_CYCLES = 5
MIN_RECHARGE_MATCHES = 3
_BURST_KEYS = frozenset({"精神爆发", "灼热火焰", "时间加速"})
_STOCK_PATTERN = re.compile(r"冷却时间(?:可以|可)储存\s*(\d+)\s*次")
_RESET_PATTERN = re.compile(r"冷却时间额外重置\s*(\d+)\s*次")


class CooldownClock:
    """Map wall seconds to cooldown work using the scoring CDR stacking rule.

    Build once per player; each interval lookup is O(log buff boundaries),
    rather than replaying every Buff window for every candidate and cast pair.
    """
    def __init__(self, windows=()):
        edges: dict[float, float] = {}
        for start, end, factor, _speed in windows:
            start, end = max(0.0, float(start)), max(0.0, float(end))
            reduction = max(0.0, 1.0 - float(factor))
            if end <= start or reduction <= 0:
                continue
            edges[start] = edges.get(start, 0.0) + reduction
            edges[end] = edges.get(end, 0.0) - reduction
        self.starts = [0.0]
        self.work = [0.0]
        self.rates = [1.0]
        reduction = 0.0
        for seconds, delta in sorted(edges.items()):
            amount = self.work[-1] + (seconds - self.starts[-1]) * self.rates[-1]
            reduction += delta
            rate = 1.0 / max(0.30, 1.0 - reduction)
            if seconds == self.starts[-1]:
                self.rates[-1] = rate
            else:
                self.starts.append(seconds)
                self.work.append(amount)
                self.rates.append(rate)

    def at(self, seconds: float) -> float:
        index = bisect_right(self.starts, seconds) - 1
        return self.work[index] + (seconds - self.starts[index]) * self.rates[index]


def skill_cooldown_mechanics(store: Any, job: str, candidate: dict, tree: dict) -> dict:
    key = candidate["skill_key"]
    skills = tree.get("skills", {}) if isinstance(tree, dict) else {}
    entry = skills.get(candidate["skill_id"], {})
    if not entry:
        entry = next((row for row in skills.values()
                      if canonical_skill(row.get("name")) == key), {})
    levels = [row for row in entry.get("levels", ())
              if as_number(row.get("required_level")) <= 70]
    descriptions = [norm(row.get("desc")) for row in levels]
    if not descriptions:
        descriptions = [norm(row.get("desc")) for row in store.full_skill_levels(
            job, candidate["skill"], max_required_level=70)]
    stock = max([int(n) for text in descriptions for n in _STOCK_PATTERN.findall(text)] or [1])
    reset = max([int(n) for text in descriptions for n in _RESET_PATTERN.findall(text)] or [0])
    # Unknown skill level: the largest legal stock is the conservative null
    # hypothesis. A lower-level skill cannot get more false plate evidence.
    capacity = max(stock, reset + 1)
    rules = store.skill_cooldown_rules().get("jobs", {}).get(job, {})
    rule = next((row for skill, row in rules.items()
                 if canonical_skill(skill) == key), {})
    special_rule = bool(rule.get("sources") or as_number(rule.get("passive_percent"))
                        or as_number(rule.get("self_refund_seconds"))
                        or as_number(rule.get("extra_resets")))
    remaining_descriptions = [re.sub(r"\[?冷却时间\]?\s*重置次数增加", "",
                                    _RESET_PATTERN.sub("", text)) for text in descriptions]
    special_prose = any(re.search(
        r"冷却时间.{0,8}(?:减少|缩短|重置)|(?:减少|缩短|重置).{0,8}冷却时间",
        text) for text in remaining_descriptions)
    base = store.skill_reference_cooldown(job, candidate["skill"], 0.0)
    # Use the fastest legal base level when levels have different CDs. Slower
    # levels must not be mistaken for a plate on a different observed level.
    details_fn = getattr(store, "skill_reference_cooldown_details", None)
    source = details_fn(job, candidate["skill"], 0.0).get("source") if details_fn else ""
    level_cds = [as_number(row.get("cooldown")) for row in levels
                 if as_number(row.get("cooldown")) > 0]
    if level_cds and source not in {"skill_adjustment", "buff_activation_rule"}:
        base = min(base, min(level_cds))
    return {"base_cooldown": base, "capacity": capacity, "extra_reset_count": reset,
            "models": ("serial", "reset_stock") if reset else ("serial",),
            "excluded": special_rule or special_prose or capacity > 32}


def _simulate_stock(times: list[float], cooldown: float, capacity: int,
                    model: str, tolerance: float, reset_indices: set[int]) -> dict:
    stock = capacity
    next_ready = None
    violations = []
    recharge_matches = []
    stock_casts = 0
    last_evidence_index = -capacity
    for index, seconds in enumerate(times):
        if index in reset_indices:
            stock, next_ready = capacity, None
        latest_recharge = None
        while next_ready is not None and next_ready <= seconds + tolerance:
            latest_recharge = next_ready
            stock = capacity if model == "reset_stock" else min(capacity, stock + 1)
            next_ready = None if stock == capacity else next_ready + cooldown
        if stock <= 0:
            # Count a blocked recharge once, then give the null model fresh
            # stock. A single unexplained reset/burst cannot multiply into
            # five allegedly independent plate observations.
            violations.append({"cast_index": index, "early_by_work_seconds": next_ready - seconds})
            stock, next_ready = capacity, None
        elif latest_recharge is not None and abs(seconds - latest_recharge) <= tolerance:
            if index - last_evidence_index >= capacity:
                recharge_matches.append(seconds - latest_recharge)
                last_evidence_index = index
        else:
            stock_casts += 1
        stock -= 1
        if next_ready is None:
            next_ready = seconds + cooldown
    return {"violations": violations, "recharge_matches": recharge_matches,
            "stock_casts": stock_casts}


def cooldown_plate_evidence(candidate: dict, timestamps: list[float], mechanics: dict,
                            clock: CooldownClock, reset_times=(), unknown_windows=()) -> dict:
    """Explain a skill's entire cast series under both equipment hypotheses."""
    base = mechanics["base_cooldown"]
    expected = base * (1.0 - candidate["value"] / 100.0)
    reduction = base - expected
    report = {"skill": candidate["skill"], "skill_id": candidate["skill_id"],
              "value": candidate["value"], "base_cooldown": base,
              "expected_cooldown": expected, "capacity": mechanics["capacity"],
              "extra_reset_count": mechanics["extra_reset_count"],
              "cast_count": len(timestamps), "inferred": False, "models": []}
    if mechanics["excluded"]:
        report["reason"] = "skill_specific_refund_or_reset_not_safely_modelled"
        return report
    if base <= 1.0 or reduction < 0.25 or len(timestamps) < MIN_INDEPENDENT_CYCLES + 1:
        report["reason"] = "insufficient_measurable_cycles"
        return report
    tolerance = min(max(0.15, expected * 0.015, reduction * 0.20), reduction * 0.45)
    clock = CooldownClock() if candidate["skill_key"] in _BURST_KEYS else clock
    times = [clock.at(seconds) for seconds in timestamps]
    reset_indices = {index for index in range(1, len(timestamps)) if any(
        timestamps[index - 1] < boundary <= timestamps[index] for boundary in reset_times)}
    # Unknown CDR labels cannot be assigned an invented factor. Restart with
    # optimistic stock after them and ignore their observations instead.
    usable = []
    usable_indices = []
    usable_resets = set()
    reset_next = False
    for index, seconds in enumerate(timestamps):
        obscured = candidate["skill_key"] not in _BURST_KEYS and any(
            left < seconds and right > (timestamps[index - 1] if index else seconds)
            for left, right in unknown_windows)
        if obscured:
            reset_next = True
            continue
        if reset_next or index in reset_indices:
            usable_resets.add(len(usable))
        usable.append(times[index])
        usable_indices.append(index)
        reset_next = False
    capacity = mechanics["capacity"]
    for model in mechanics["models"]:
        no_plate = _simulate_stock(usable, base, capacity, model, tolerance, usable_resets)
        with_plate = _simulate_stock(usable, expected, capacity, model, tolerance, usable_resets)
        examples = [{**row, "timestamp_seconds": timestamps[usable_indices[row["cast_index"]]]}
                    for row in no_plate["violations"][:5]]
        report["models"].append({"model": model,
            "no_plate_violation_count": len(no_plate["violations"]),
            "plate_violation_count": len(with_plate["violations"]),
            "recharge_match_count": len(with_plate["recharge_matches"]),
            "stock_cast_count": with_plate["stock_casts"],
            "no_plate_examples": examples,
            "median_recharge_error": median(map(abs, with_plate["recharge_matches"]))
                if with_plate["recharge_matches"] else None})
    report["ignored_cast_count"] = len(times) - len(usable)
    if any(row["no_plate_violation_count"] < MIN_INDEPENDENT_CYCLES for row in report["models"]):
        report["reason"] = ("normal_cooldown_or_initial_stock_can_explain_casts"
                            if any(row["no_plate_violation_count"] == 0 for row in report["models"])
                            else "insufficient_independent_no_plate_contradictions")
        return report
    # Deliberately waiting after a refill is legal. Five independent null
    # contradictions are the proof count; not every refill has to be pressed
    # on its earliest frame. Still require three near-plate refills, so an
    # unrelated 2% passive reduction cannot masquerade as a 20% plate.
    valid = [row for row in report["models"] if row["plate_violation_count"] == 0
             and row["recharge_match_count"] >= MIN_RECHARGE_MATCHES]
    if not valid:
        report["reason"] = "plate_still_violates_casts_or_lacks_recharge_cycles"
        return report
    best = max(valid, key=lambda row: row["recharge_match_count"])
    proof_count = min(row["no_plate_violation_count"] for row in report["models"])
    report.update(inferred=True, reason="repeated_exclusive_recharge_evidence",
                  recharge_model=best["model"], evidence_count=proof_count,
                  recharge_match_count=best["recharge_match_count"],
                  median_error=best["median_recharge_error"],
                  confidence=round(min(0.98, 0.93 + (proof_count - 5) * 0.005), 4))
    return report
