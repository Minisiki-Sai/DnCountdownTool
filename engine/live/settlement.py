"""Independent server settlement evidence; never a combat clock or map ID.

A structurally valid 0C05 result assigned to the current encounter is the
primary completion proof. A narrow fallback recognizes the terminal Boss death
when that result was lost from the capture, without depending on chests or loot.
NestEnd remains countdown-start timing evidence. The delayed result timestamp
is never reinterpreted as the clear-countdown timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Iterable

from .tcp_profile_codec import (
    decode_registered_server_tcp_envelope,
    decrypt_tcp_profile_body,
)


# Independently recovered from 8-player samples; validated on held-out captures.
# Scoped to 619-byte bodies, never installed as a global same-length key.
_RESULT_8_KEY = (0x72FA33C4, 0x790B0D28, 0x09AE50C4, 0x3E9EC50F)
_LABELS = {"cleared": "通关", "not_cleared": "未通关", "unknown": ""}
_MIN_RESULT_PARTICIPANTS = 1
_MAX_RESULT_PARTICIPANTS = 8
_RESULT_BODY_BASE_SIZE = 19
_RESULT_PARTICIPANT_SIZE = 75
_RESULT_ENVELOPE_SIZE = 3
_TERMINAL_BOSS_END_SIGNATURE = bytes.fromhex("f1110f00")
_TERMINAL_BOSS_HIT_WINDOW_MS = 100
_TERMINAL_BOSS_RESULT_GRACE_MS = 20_000
_RESULT_WIRE_LENGTHS = frozenset(
    _RESULT_ENVELOPE_SIZE + _RESULT_BODY_BASE_SIZE + _RESULT_PARTICIPANT_SIZE * count
    for count in range(_MIN_RESULT_PARTICIPANTS, _MAX_RESULT_PARTICIPANTS + 1)
)


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    timestamp_ms: int
    clear_table_id: int
    server_elapsed_ms: int
    actor_ids: tuple[str, ...]
    evidence_hash: str
    source: str = "s2c-tcp-dungeon-result-0c05-v576"
    trigger_index: int = -1
    trigger_grid: str = ""
    trigger_name: str = ""
    trigger_boss_id: str = ""
    trigger_map_id: int = 0
    trigger_requires_result: bool = False


def _valid_result_body(body: bytes) -> bool:
    if len(body) < _RESULT_BODY_BASE_SIZE or body[2:4] != b"\x0c\x05":
        return False
    count = body[4]
    if not _MIN_RESULT_PARTICIPANTS <= count <= _MAX_RESULT_PARTICIPANTS:
        return False
    if len(body) != _RESULT_BODY_BASE_SIZE + _RESULT_PARTICIPANT_SIZE * count:
        return False
    if int.from_bytes(body[:2], "little") != len(body):
        return False
    table_id = int.from_bytes(body[5:9], "little")
    elapsed_ms = int.from_bytes(body[9:13], "little")
    actors = [int.from_bytes(body[19 + 75*i:23 + 75*i], "little") for i in range(count)]
    return (
        0 < table_id < 1_000_000
        and 0 < elapsed_ms <= 7 * 24 * 3600 * 1000
        and len(set(actors)) == count
        and all(0x00FFFFFF < actor < 0xFFFFFFFF for actor in actors)
    )


def decode_settlement(packet: object, origin: float) -> SettlementEvent | None:
    if packet.transport.upper() != "TCP" or packet.inferred_direction() != "server_to_client":
        return None
    payload = packet.payload
    # The third envelope byte is variable; it is not a high length byte.
    body_size = len(payload) - _RESULT_ENVELOPE_SIZE
    if (
        body_size < _RESULT_BODY_BASE_SIZE
        or (body_size - _RESULT_BODY_BASE_SIZE) % _RESULT_PARTICIPANT_SIZE
        or not _MIN_RESULT_PARTICIPANTS
        <= (body_size - _RESULT_BODY_BASE_SIZE) // _RESULT_PARTICIPANT_SIZE
        <= _MAX_RESULT_PARTICIPANTS
        or int.from_bytes(payload[:2], "little") != len(payload)
    ):
        return None
    result = decode_registered_server_tcp_envelope(
        payload, connection_context=packet, validator=_valid_result_body,
    )
    body = result[0] if result else b""
    if not body and len(payload) == 622:
        body = decrypt_tcp_profile_body(payload[3:], _RESULT_8_KEY, 2)
    if not _valid_result_body(body):
        return None
    return SettlementEvent(
        timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
        clear_table_id=int.from_bytes(body[5:9], "little"),
        server_elapsed_ms=int.from_bytes(body[9:13], "little"),
        actor_ids=tuple(
            f"0x{int.from_bytes(body[19 + 75*i:23 + 75*i], 'little'):08X}"
            for i in range(body[4])
        ),
        evidence_hash=hashlib.sha256(body).hexdigest(),
    )


def may_contain_settlement(payload: bytes) -> bool:
    """Cheap envelope gate for every supported party size, not just 4/8."""
    return len(payload) in _RESULT_WIRE_LENGTHS


def decode_terminal_boss_end(
    packet: object, origin: float, *, map_id: int,
    boss_template_id: int, boss_id: str,
) -> SettlementEvent | None:
    """Record a final-Boss end candidate when the formal TCP result is lost.

    The short UDP end marker also occurs in intermediate rooms. It is *only*
    emitted for a catalogued terminal Boss. SettlementLedger additionally
    requires a same-actor Boss hit at the marker time and a result grace period.
    """
    from .clear_trigger import RULES

    payload = packet.payload
    if (
        packet.transport.upper() != "UDP"
        or packet.inferred_direction() != "server_to_client"
        or len(payload) != 18
        or payload[5:9] != _TERMINAL_BOSS_END_SIGNATURE
        or not boss_id
    ):
        return None
    rule = next((
        row for row in RULES
        if map_id in row.map_ids and boss_template_id == row.boss_template_id
    ), None)
    if rule is None:
        return None
    return SettlementEvent(
        timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
        clear_table_id=rule.clear_table_id,
        server_elapsed_ms=0,
        actor_ids=(),
        evidence_hash=hashlib.sha256(payload).hexdigest(),
        source="s2c-udp-terminal-boss-end-candidate-v576",
        trigger_index=-2,
        trigger_grid=rule.grid,
        trigger_name=rule.name,
        trigger_boss_id=boss_id,
        trigger_map_id=map_id,
    )


def decode_clear_trigger(packet: object, origin: float, *, map_id: int,
                         boss_template_id: int, boss_id: str) -> SettlementEvent | None:
    from .clear_trigger import match_clear_trigger

    rule = match_clear_trigger(packet, map_id, boss_template_id)
    if rule is None or not boss_id:
        return None
    return SettlementEvent(
        timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
        clear_table_id=rule.clear_table_id, server_elapsed_ms=0, actor_ids=(),
        evidence_hash=hashlib.sha256(packet.payload[3:]).hexdigest(),
        source="s2c-tcp-nestend-trigger-v576", trigger_index=rule.index,
        trigger_grid=rule.grid, trigger_name=rule.name, trigger_boss_id=boss_id,
        trigger_map_id=map_id, trigger_requires_result=not bool(map_id),
    )


def _actor_key(value: object) -> str:
    text = str(value or "").strip()
    try:
        return f"0X{int(text, 16):X}"
    except ValueError:
        return text.upper()


def decode_clear_reward_trigger(packet: object, origin: float, *, map_id: int,
                                boss_template_id: int, boss_id: str) -> SettlementEvent | None:
    from .clear_reward_trigger import match_clear_reward_trigger

    rule = match_clear_reward_trigger(packet, map_id, boss_template_id)
    if rule is None or not boss_id:
        return None
    return SettlementEvent(
        timestamp_ms=max(0, round((packet.timestamp_seconds - origin) * 1000)),
        clear_table_id=rule.clear_table_id, server_elapsed_ms=0, actor_ids=(),
        evidence_hash=hashlib.sha256(packet.payload[3:]).hexdigest(),
        source="s2c-tcp-verified-clear-reward-v576", trigger_index=rule.index,
        trigger_grid=rule.grid, trigger_name=rule.name, trigger_boss_id=boss_id,
        trigger_map_id=map_id,
    )


class SettlementLedger:
    """Bounded evidence, independent of damage totals and capture stop time."""

    def __init__(self) -> None:
        self._results: dict[str, SettlementEvent] = {}
        self._terminal_boss_ends: dict[str, SettlementEvent] = {}
        self._last_boss_damage_ms: int | None = None
        self._last_boss_damage_by_target: dict[str, int] = {}
        self._combat_actors: set[str] = set()
        self._countdowns: dict[str, SettlementEvent] = {}
        self._clear_rewards: dict[str, SettlementEvent] = {}
        self._last_boss_target_id = ""

    def observe(self, event: SettlementEvent) -> None:
        if event.source == "s2c-tcp-verified-clear-reward-v576":
            self._clear_rewards.setdefault(event.evidence_hash, event)
            if len(self._clear_rewards) > 64:
                oldest = min(self._clear_rewards, key=lambda key: self._clear_rewards[key].timestamp_ms)
                del self._clear_rewards[oldest]
            return
        if event.source == "s2c-udp-terminal-boss-end-candidate-v576":
            self._terminal_boss_ends.setdefault(event.evidence_hash, event)
            if len(self._terminal_boss_ends) > 64:
                oldest = min(self._terminal_boss_ends, key=lambda key: self._terminal_boss_ends[key].timestamp_ms)
                del self._terminal_boss_ends[oldest]
            return
        if event.trigger_index >= 0:
            self._countdowns.setdefault(event.evidence_hash, event)
            if len(self._countdowns) > 64:
                oldest = min(self._countdowns, key=lambda key: self._countdowns[key].timestamp_ms)
                del self._countdowns[oldest]
            return
        # Retransmissions and appended previous-session results are not new clears.
        if event.evidence_hash not in self._results:
            self._results[event.evidence_hash] = event
        if len(self._results) > 64:
            oldest = min(self._results, key=lambda key: self._results[key].timestamp_ms)
            del self._results[oldest]

    def observe_boss_damage(self, timestamp_ms: int, source_id: str, target_id: str = "") -> None:
        if self._last_boss_damage_ms is None or timestamp_ms >= self._last_boss_damage_ms:
            self._last_boss_target_id = target_id
        self._last_boss_damage_ms = max(self._last_boss_damage_ms or 0, timestamp_ms)
        if target_id:
            self._last_boss_damage_by_target[target_id] = max(
                self._last_boss_damage_by_target.get(target_id, 0), timestamp_ms,
            )
        if len(self._combat_actors) < 256:
            self._combat_actors.add(_actor_key(source_id))

    def latest_terminal_trigger(self) -> SettlementEvent | None:
        """Return a same-Boss NestEnd signal for the capture clock only.

        This does not change completion status: a formal result is still
        required to mark the encounter cleared.
        """
        if self._last_boss_damage_ms is None or not self._last_boss_target_id:
            return None
        for event in sorted(self._countdowns.values(), key=lambda row: row.timestamp_ms, reverse=True):
            if (event.trigger_boss_id == self._last_boss_target_id
                    and -1_000 <= event.timestamp_ms - self._last_boss_damage_ms <= 30_000):
                return event
        return None

    def snapshot(
        self,
        known_actor_ids: Iterable[str] = (),
        *,
        encounter_boss_damage_ms: int | None = None,
        encounter_boss_target_id: str = "",
        current_timestamp_ms: int | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "version": 1, "status": "unknown", "cleared": None,
            "label": "", "reason": "missing_settlement_evidence",
        }
        actors = self._combat_actors | {_actor_key(value) for value in known_actor_ids}
        last_boss_damage_ms = self._last_boss_damage_ms
        last_boss_target_id = self._last_boss_target_id
        if (
            encounter_boss_damage_ms is not None
            and (
                last_boss_damage_ms is None
                or encounter_boss_damage_ms >= last_boss_damage_ms
            )
        ):
            last_boss_damage_ms = max(0, int(encounter_boss_damage_ms))
            last_boss_target_id = str(encounter_boss_target_id or "").strip()
        # Resource-verified clear-only rewards can repair a lost result. Do
        # not put them in _countdowns: opening rewards is never the fight's
        # end time. A new encounter, wrong terminal actor, stale reward or
        # contradictory received result must not inherit an earlier clear.
        clear_reward = next((row for row in sorted(
            getattr(self, "_clear_rewards", {}).values(),
            key=lambda row: row.timestamp_ms, reverse=True,
        ) if last_boss_damage_ms is not None
            and last_boss_target_id and row.trigger_boss_id == last_boss_target_id
            and 0 <= row.timestamp_ms - last_boss_damage_ms <= 120_000
            and not any(report.timestamp_ms >= last_boss_damage_ms
                        for report in self._results.values())), None)

        def apply_clear_reward_fallback() -> None:
            if clear_reward is None:
                return
            result.update(
                status="cleared", cleared=True, label="通关",
                reason="verified_clear_reward_without_formal_result",
                clear_table_id=clear_reward.clear_table_id,
                trigger_index=clear_reward.trigger_index,
                trigger_grid=clear_reward.trigger_grid,
                trigger_name=clear_reward.trigger_name,
                trigger_map_id=clear_reward.trigger_map_id,
                evidence_source=clear_reward.source,
                evidence_hash=clear_reward.evidence_hash,
                settlement_timestamp_ms=clear_reward.timestamp_ms,
            )
        matched_countdown: SettlementEvent | None = None
        for countdown in sorted(self._countdowns.values(), key=lambda row: row.timestamp_ms, reverse=True):
            if (last_boss_damage_ms is None or not last_boss_target_id
                    or countdown.trigger_boss_id != last_boss_target_id
                    or not -1000 <= countdown.timestamp_ms - last_boss_damage_ms <= 30_000):
                continue
            matched_countdown = countdown
            matching_results = [row for row in self._results.values()
                                if row.clear_table_id == countdown.clear_table_id
                                and 0 <= row.timestamp_ms - countdown.timestamp_ms <= 30_000
                                and sum(_actor_key(actor) in actors for actor in row.actor_ids) >= 2]
            # NestEnd is accurate countdown timing evidence, not the formal
            # completion authority.  Every clear now requires a structurally
            # verified 0C05 result assigned to this encounter, even when the
            # trigger's map and terminal-Boss identity are already known.
            if not matching_results:
                continue
            report = min(matching_results, key=lambda row: row.timestamp_ms)
            result.update(status="cleared", cleared=True, label="通关",
                          reason="verified_matched_dungeon_result",
                          clear_table_id=countdown.clear_table_id,
                          countdown_timestamp_ms=countdown.timestamp_ms,
                          trigger_index=countdown.trigger_index, trigger_grid=countdown.trigger_grid,
                          trigger_name=countdown.trigger_name, trigger_map_id=countdown.trigger_map_id,
                          evidence_hash=report.evidence_hash,
                          evidence_source=report.source,
                          trigger_evidence_hash=countdown.evidence_hash,
                          trigger_evidence_source=countdown.source,
                          settlement_timestamp_ms=report.timestamp_ms,
                          server_elapsed_seconds=round(report.server_elapsed_ms / 1000, 3),
                          participant_count=len(report.actor_ids),
                          matched_participant_count=sum(
                              _actor_key(actor) in actors for actor in report.actor_ids
                          ))
            return result
        terminal_end = next((
            row for row in sorted(self._terminal_boss_ends.values(), key=lambda item: item.timestamp_ms, reverse=True)
            if abs(self._last_boss_damage_by_target.get(row.trigger_boss_id, -10**12) - row.timestamp_ms)
            <= _TERMINAL_BOSS_HIT_WINDOW_MS
        ), None)
        # A received result is stronger evidence than this packet-loss fallback.
        # If it fails the encounter/participant check below, do not turn that
        # contradictory formal evidence into a presumed clear.
        if terminal_end is not None and any(
            row.timestamp_ms >= terminal_end.timestamp_ms
            for row in self._results.values()
        ):
            terminal_end = None
        fallback_ready = (
            terminal_end is not None
            and current_timestamp_ms is not None
            and current_timestamp_ms >= terminal_end.timestamp_ms + _TERMINAL_BOSS_RESULT_GRACE_MS
        )
        def apply_terminal_end_fallback() -> None:
            if terminal_end is None:
                return
            result.update(
                status="cleared" if fallback_ready else "unknown",
                cleared=True if fallback_ready else None,
                label="通关" if fallback_ready else "",
                reason=("terminal_boss_end_without_formal_result" if fallback_ready
                        else "pending_terminal_boss_result"),
                clear_table_id=terminal_end.clear_table_id,
                trigger_map_id=terminal_end.trigger_map_id,
                trigger_grid=terminal_end.trigger_grid,
                trigger_name=terminal_end.trigger_name,
                trigger_evidence_hash=terminal_end.evidence_hash,
                trigger_evidence_source=terminal_end.source,
            )
        if not self._results:
            if terminal_end is not None:
                apply_terminal_end_fallback()
                return result
            if matched_countdown is not None:
                result.update(
                    reason="nestend_without_matched_formal_result",
                    countdown_timestamp_ms=matched_countdown.timestamp_ms,
                    trigger_index=matched_countdown.trigger_index,
                    trigger_grid=matched_countdown.trigger_grid,
                    trigger_name=matched_countdown.trigger_name,
                    trigger_map_id=matched_countdown.trigger_map_id,
                    trigger_evidence_hash=matched_countdown.evidence_hash,
                    trigger_evidence_source=matched_countdown.source,
                )
            elif last_boss_damage_ms is not None and last_boss_target_id:
                result.update(
                    status="not_cleared", cleared=False, label="未通关",
                    reason="missing_formal_result_and_nestend",
                )
            apply_clear_reward_fallback()
            return result
        event = max(self._results.values(), key=lambda row: row.timestamp_ms)
        result.update(
            clear_table_id=event.clear_table_id,
            server_elapsed_seconds=round(event.server_elapsed_ms / 1000, 3),
            settlement_timestamp_ms=event.timestamp_ms,
            participant_count=len(event.actor_ids),
            evidence_hash=event.evidence_hash,
            evidence_source=event.source,
        )
        matched = sum(_actor_key(value) in actors for value in event.actor_ids)
        result["matched_participant_count"] = matched
        required_matches = min(2, len(event.actor_ids))
        if (
            last_boss_damage_ms is None
            or not last_boss_target_id
            or matched < required_matches
        ):
            result["reason"] = "unmatched_encounter"
        elif last_boss_damage_ms > event.timestamp_ms:
            result["reason"] = "combat_after_settlement"
        elif event.timestamp_ms - last_boss_damage_ms > 60_000:
            # A result replayed much later cannot be assigned to the latest Boss.
            # This is a conservative matching guard, not a rule for capture stop.
            result["reason"] = "settlement_outside_encounter"
        else:
            # Accept every structurally valid result-table family.  Map/table
            # identifiers are deliberately not whitelisted: current and future
            # dungeons share this envelope and are guarded by encounter evidence.
            # Do not fabricate a countdown timestamp from the delayed result.
            result.update(
                status="cleared",
                cleared=True,
                label="通关",
                reason="verified_matched_dungeon_result",
            )
        if result["status"] != "cleared":
            if matched_countdown is not None:
                # A map-confirmed NestEnd without a formal result assigned to
                # this encounter remains ambiguous rather than becoming a
                # false failure.
                result.update(
                    status="unknown", cleared=None, label="",
                    reason="nestend_without_matched_formal_result",
                    countdown_timestamp_ms=matched_countdown.timestamp_ms,
                    trigger_index=matched_countdown.trigger_index,
                    trigger_grid=matched_countdown.trigger_grid,
                    trigger_name=matched_countdown.trigger_name,
                    trigger_map_id=matched_countdown.trigger_map_id,
                    trigger_evidence_hash=matched_countdown.evidence_hash,
                    trigger_evidence_source=matched_countdown.source,
                )
            elif last_boss_damage_ms is not None and last_boss_target_id:
                result.update(
                    status="not_cleared", cleared=False, label="未通关",
                )
        if result["status"] != "cleared" and terminal_end is not None:
            apply_terminal_end_fallback()
        if result["status"] != "cleared":
            apply_clear_reward_fallback()
        return result


def completion_upload_metadata(value: object) -> dict[str, object]:
    """Keep small, versioned evidence in existing score_detail JSONB."""
    raw = value if isinstance(value, dict) else {}
    status = raw.get("status")
    status = status if isinstance(status, str) and status in _LABELS else "unknown"
    result: dict[str, object] = {
        "version": 1, "status": status,
        "cleared": True if status == "cleared" else False if status == "not_cleared" else None,
        "label": _LABELS[status],
        "reason": str(raw.get("reason") or "missing_settlement_evidence")[:96],
    }
    for key in ("clear_table_id", "server_elapsed_seconds", "settlement_timestamp_ms", "participant_count", "matched_participant_count",
                "countdown_timestamp_ms", "trigger_index", "trigger_map_id"):
        number = raw.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
            result[key] = number
    for key in (
        "evidence_hash", "evidence_source", "trigger_evidence_hash",
        "trigger_evidence_source", "trigger_grid", "trigger_name",
    ):
        if isinstance(raw.get(key), str):
            result[key] = raw[key][:96]
    return result
