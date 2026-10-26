from __future__ import annotations

from .dungeon_map_catalog import (
    has_configured_battle_continuation,
    is_battle_map,
    resolve_dungeon_map_id,
)
from .nest_recognizer import resolve_nest_template


class V576AutoCaptureStopDetector:
    """Freeze the fight at NestEnd; keep recording until settlement or safe exit.

    Damage silence and inferred Boss death are intentionally excluded.  Long
    mechanics therefore cannot stop a recording.  MapType 1 is a town,
    MapType 2 a world-zone/dungeon entrance, and MapType 3 a dungeon battle
    map. Moving between two tier-3 rooms keeps one recording. A verified
    tier-1/2 exit ends an ordinary recording; after NestEnd it instead arms
    the missing-result fallback, leaving time for the formal result. The
    short transition acknowledgement or teardown alone remains unsafe
    because both also occur between dungeon rooms.
    """
    # Some accelerator/guest captures contain neither the TCP scene teardown
    # nor the authoritative Heaven map record.  A terminal-map transition
    # request plus a scene acknowledgement is still ambiguous (retry and room
    # changes exist), so only accept it after a full minute without new damage.
    _TERMINAL_TRANSITION_QUIET_SECONDS = 60.0
    _TERMINAL_CANDIDATE_AFTER_ACK_SECONDS = 2.0
    # A NestEnd freezes the displayed fight clock, but is not itself the
    # authoritative completion packet. Never close the capture on a fixed
    # NestEnd timer while the player is still in the instance: the 0C05 TCP
    # result may still be in flight or waiting behind the decoder queue.
    _TERMINAL_RESULT_WAIT_SECONDS = 60.0
    _TERMINAL_EXIT_GRACE_SECONDS = 15.0

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._battle_seen = False
        self._battle_observed_at = -1.0
        self._processed_map_rows = 0
        self._seen_town_scene_load_revision = 0
        self._seen_hub_return_revision = 0
        self._seen_scene_ack_revision = 0
        self._pending_scene_ack_at = -1.0
        self._pending_terminal_ack_at = -1.0
        self._pending_terminal_candidate_id = 0
        self._terminal_trigger = None
        self._terminal_verified_exit_at = -1.0

    @property
    def pending_stage_end_seconds(self) -> float | None:
        trigger = self._terminal_trigger
        return trigger.timestamp_ms / 1000.0 if trigger is not None else None

    @staticmethod
    def _map_tier(row: object) -> tuple[int, int, str]:
        """Return ``(tier, map_id, display_name)`` for one map-history row."""

        if not isinstance(row, dict):
            return 0, 0, ""
        try:
            map_id = int(row.get("map_id") or row.get("inferred_map_id") or 0)
            tier = int(row.get("map_type") or 0)
        except (TypeError, ValueError):
            return 0, 0, ""
        entry = resolve_dungeon_map_id(map_id)
        if tier not in (1, 2, 3) and entry is not None:
            tier = int(entry.map_type or 0)
        name = str(row.get("map_name") or "").strip()
        if not name and entry is not None:
            name = str(entry.display_name or entry.official_name).strip()
        return tier, map_id, name

    def evaluate(
        self,
        aggregator: object,
        elapsed_seconds: float,
        *,
        decoder: object | None = None,
        decode_backlog_seconds: float = 0.0,
    ) -> str:
        """Return a reason after NestEnd settlement or a verified map exit.

        Ordinary damage silence is never an end condition. ``elapsed_seconds``
        is used only for the narrowly-scoped terminal-transition fallback
        documented above.
        """
        if decoder is None:
            return ""

        # Probe handoff can establish a nest from a verified monster template
        # without an exact map packet (e.g. 1018 Sea run 7). Remember that
        # battle evidence before an authoritative town/hub row arrives. A
        # client selection, display-name guess or unknown template cannot arm
        # this path; the template alone still never stops recording.
        if not self._battle_seen and str(getattr(decoder, "detected_nest_confidence", "")) in {
            "entry_template", "template", "template_set", "dominant_template_set",
        }:
            try:
                template_id = int(getattr(decoder, "detected_nest_template_id", 0) or 0)
                observed_at = float(getattr(decoder, "detected_nest_observed_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                template_id, observed_at = 0, -1.0
            resolution = resolve_nest_template(template_id)
            if (
                resolution is not None
                and str(getattr(decoder, "detected_nest_key", "")) == resolution.profile.key
                and 0.0 <= observed_at <= elapsed_seconds
            ):
                self._battle_seen = True
                self._battle_observed_at = observed_at

        # A client map-entry candidate is only a selection. It must not arm
        # automatic stop before the server actually loads the battle scene.
        try:
            candidate_id = int(
                getattr(decoder, "detected_map_candidate_id", 0) or 0
            )
            candidate_observed_at = float(
                getattr(decoder, "detected_map_candidate_observed_at", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            candidate_id = 0
            candidate_observed_at = 0.0
        candidate_layout = str(
            getattr(decoder, "detected_map_candidate_layout", "") or ""
        )
        ledger = getattr(aggregator, "_settlement_ledger", None)
        if self._battle_seen and ledger is not None:
            terminal = ledger.latest_terminal_trigger()
            if terminal is not None and self._terminal_trigger is None:
                self._terminal_trigger = terminal
        decoded_elapsed = elapsed_seconds - max(0.0, decode_backlog_seconds)
        if self._terminal_trigger is not None:
            terminal = self._terminal_trigger
            completion = ledger.snapshot() if ledger is not None else {}
            if completion.get("status") == "cleared" and (
                completion.get("countdown_timestamp_ms") == terminal.timestamp_ms
                or completion.get("settlement_timestamp_ms", -1) >= terminal.timestamp_ms
            ):
                return "检测到关卡结束并收到正式结算包"

        history = getattr(decoder, "detected_map_history", ()) or ()
        for row in history[self._processed_map_rows :]:
            try:
                observed_at = float(row.get("observed_at") or 0.0)
            except (AttributeError, TypeError, ValueError):
                continue
            map_type, map_id, map_name = self._map_tier(row)
            if map_type == 3:
                self._battle_seen = True
                self._battle_observed_at = max(
                    self._battle_observed_at,
                    observed_at,
                )
                if observed_at > self._pending_terminal_ack_at:
                    self._pending_scene_ack_at = -1.0
                    self._pending_terminal_ack_at = -1.0
                    self._pending_terminal_candidate_id = 0
                continue
            if (
                self._battle_seen
                and map_type in (1, 2)
                and observed_at >= self._battle_observed_at
            ):
                self._processed_map_rows = len(history)
                if self._terminal_trigger is not None:
                    self._terminal_verified_exit_at = max(
                        self._terminal_verified_exit_at, observed_at,
                    )
                    continue
                destination = map_name or f"地图 {map_id}"
                return f"检测到离开巢穴并进入{destination}"
        self._processed_map_rows = len(history)

        # 03 18 is the server's complete town-scene initialization. It can
        # arrive before the shorter exact 03 01 town-map packet, which may be
        # missing after a TCP gap. Unlike a client leave request, this proves
        # the destination scene is being loaded, and it is available to a
        # non-leader as well as to the party leader.
        town_revision = int(
            getattr(decoder, "detected_town_scene_load_revision", 0) or 0
        )
        if town_revision > self._seen_town_scene_load_revision:
            self._seen_town_scene_load_revision = town_revision
            town_at = float(
                getattr(decoder, "detected_town_scene_load_observed_at", 0.0)
                or 0.0
            )
            if self._battle_seen and town_at >= self._battle_observed_at:
                if self._terminal_trigger is not None:
                    self._terminal_verified_exit_at = max(
                        self._terminal_verified_exit_at, town_at,
                    )
                else:
                    return "检测到离开巢穴并加载城镇场景"

        # Fallback for captures such as 0918经典冰灵: the battle was formally
        # identified, then UDP carried a scene-transition acknowledgement and
        # a terminal-map transition candidate, but the accelerator hid both
        # the TCP teardown and the authoritative Heaven map.  Do not accept an
        # acknowledgement by itself, and cancel the fallback if any new damage
        # appears after it (retry/next-scene case).
        try:
            ack_revision = int(
                getattr(decoder, "detected_scene_transition_ack_revision", 0)
                or 0
            )
            ack_observed_at = float(
                getattr(decoder, "detected_scene_transition_ack_observed_at", 0.0)
                or 0.0
            )
        except (TypeError, ValueError):
            ack_revision = 0
            ack_observed_at = 0.0
        if ack_revision > self._seen_scene_ack_revision:
            self._seen_scene_ack_revision = ack_revision
            self._pending_scene_ack_at = (
                ack_observed_at
                if self._battle_seen
                and ack_observed_at >= self._battle_observed_at
                else -1.0
            )
            self._pending_terminal_ack_at = -1.0
            self._pending_terminal_candidate_id = 0

        candidate_entry = resolve_dungeon_map_id(candidate_id)
        candidate_delta = candidate_observed_at - self._pending_scene_ack_at
        if (
            self._pending_scene_ack_at >= 0.0
            and candidate_layout.startswith("c2s-udp-map-transition-")
            and 0.0 <= candidate_delta
            <= self._TERMINAL_CANDIDATE_AFTER_ACK_SECONDS
            and is_battle_map(candidate_entry)
            and not has_configured_battle_continuation(candidate_id)
        ):
            self._pending_terminal_ack_at = self._pending_scene_ack_at
            self._pending_terminal_candidate_id = candidate_id

        if self._pending_terminal_ack_at >= 0.0:
            ack_ms = round(self._pending_terminal_ack_at * 1000.0)
            combat_events = getattr(aggregator, "_combat_events", ()) or ()
            damage_after_ack = any(
                int(getattr(event, "amount", 0) or 0) > 0
                and int(getattr(event, "timestamp_ms", 0) or 0) > ack_ms + 1_000
                for event in combat_events
            )
            if damage_after_ack:
                self._pending_scene_ack_at = -1.0
                self._pending_terminal_ack_at = -1.0
                self._pending_terminal_candidate_id = 0
            elif (
                float(elapsed_seconds) - self._pending_terminal_ack_at
                >= self._TERMINAL_TRANSITION_QUIET_SECONDS
            ):
                verified_ack_at = self._pending_terminal_ack_at
                self._pending_scene_ack_at = -1.0
                self._pending_terminal_ack_at = -1.0
                self._pending_terminal_candidate_id = 0
                if self._terminal_trigger is not None:
                    self._terminal_verified_exit_at = max(
                        self._terminal_verified_exit_at,
                        verified_ack_at,
                    )
                else:
                    return (
                        "检测到终局场景切换，且60秒内未重新进入战斗，"
                        "判定已返回庭院/天堂"
                    )

        # Exact map packets are not guaranteed for a non-leader or through an
        # accelerator.  A configured formal-map entity still safely arms the
        # detector; it cannot itself stop capture.
        try:
            return_revision = int(
                getattr(
                    decoder,
                    "detected_hub_return_revision",
                    getattr(decoder, "detected_lobby_return_revision", 0),
                )
                or 0
            )
        except (TypeError, ValueError):
            return_revision = 0
        if return_revision > self._seen_hub_return_revision:
            self._seen_hub_return_revision = return_revision
            if self._battle_seen:
                try:
                    return_observed_at = float(
                        getattr(decoder, "detected_lobby_return_observed_at", 0.0)
                        or 0.0
                    )
                except (TypeError, ValueError):
                    return_observed_at = 0.0
                if (
                    return_observed_at <= 0.0
                    or return_observed_at >= self._battle_observed_at
                ):
                    if self._terminal_trigger is not None:
                        self._terminal_verified_exit_at = max(
                            self._terminal_verified_exit_at,
                            return_observed_at or elapsed_seconds,
                        )
                    else:
                        return "检测到离开巢穴并返回庭院/天堂"

        # A missing result may be genuine packet loss. Keep capturing until a
        # verified exit and the latest valid result window have both passed.
        # This fallback only controls capture lifetime; SettlementLedger keeps
        # its existing completion-status policy.
        if self._terminal_trigger is not None and self._terminal_verified_exit_at >= 0.0:
            safe_end = max(
                self._terminal_trigger.timestamp_ms / 1000.0
                + self._TERMINAL_RESULT_WAIT_SECONDS,
                self._terminal_verified_exit_at
                + self._TERMINAL_EXIT_GRACE_SECONDS,
            )
            if decoded_elapsed >= safe_end:
                return "检测到离开关卡，正式结算包安全等待结束"
        return ""
