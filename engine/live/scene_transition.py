from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address

from .tcp_profile_codec import decode_registered_server_tcp_envelope

from .dungeon_map_catalog import (
    has_configured_battle_continuation,
    is_battle_map,
    resolve_dungeon_map_id,
)


@dataclass(frozen=True, slots=True)
class VerifiedV576HubReturn:
    """A hub return proven by multiple independent scene records.

    The v576 protocol does not always publish an exact Garden map ID to a
    non-leader.  A short transition acknowledgement is also emitted between
    rooms of the same dungeon, so it is not sufficient by itself.  This
    A record is produced only after several independent signals agree: an
    exact leave or scene teardown, the transition acknowledgement, and either
    a fresh 21 0C initialization or an exact entry into a different dungeon.
    """

    observed_at: float
    battle_map_id: int
    battle_left_at: float
    transition_ack_at: float
    route_code: int
    scene_teardown_at: float | None = None
    next_battle_map_id: int | None = None
    destination_map_id: int | None = None
    evidence_layout: str = (
        "battle-map-leave+scene-transition-ack+resource-sync-210c-v576"
    )


def _has_configured_continuation(map_id: int) -> bool:
    """Return whether ``map_id`` has a later configured room in its dungeon.

    This deliberately fails closed.  If a non-leader misses the next room's
    exact map packet, a 21 0C initialization must not turn an ordinary room
    change into a destructive auto-stop boundary.
    """

    return has_configured_battle_continuation(map_id)


def _is_configured_internal_transition(
    source_map_id: int,
    destination_map_id: int,
) -> bool:
    """Return whether two exact maps are rooms of the same configured run."""

    if source_map_id <= 0 or destination_map_id <= 0:
        return False
    if source_map_id == destination_map_id:
        # Re-entering the exact same terminal map is a new run/retry candidate,
        # not movement to a configured later room.
        return False
    source = resolve_dungeon_map_id(source_map_id)
    destination = resolve_dungeon_map_id(destination_map_id)
    if not is_battle_map(source) or not is_battle_map(destination):
        return False
    source_root = int(source.enter_condition_table_id or 0)
    destination_root = int(destination.enter_condition_table_id or 0)
    if source_root <= 0 or source_root != destination_root:
        return False
    source_name = str(source.display_name or source.official_name).strip()
    destination_name = str(
        destination.display_name or destination.official_name
    ).strip()
    return source_name == destination_name


@dataclass(frozen=True, slots=True)
class _BattleExitEvidence:
    map_id: int
    left_at: float
    scene_teardown_at: float | None = None
    context_key: str = ""
    # Some world-server routes omit both the map-leave row and the scene
    # teardown for a non-leader.  In that case an acknowledgement may seed a
    # *pending* exit from the exact battle map that is still active locally.
    # This flag never proves a return on its own; only a later independent
    # gateway catalog or an authoritative server destination may complete
    # that reduced evidence chain.
    implicit_from_active_battle: bool = False


@dataclass(frozen=True, slots=True)
class _PendingSceneTransition:
    exit: _BattleExitEvidence
    ack_at: float
    route_code: int
    next_battle_map_id: int | None = None
    next_battle_entered_at: float | None = None
    next_battle_is_candidate: bool = False


class V576SceneReturnTracker:
    """Correlate map, transition and resource packets into safe boundaries."""

    _ACK_AFTER_LEAVE_LIMIT_SECONDS = 20.0
    _RESOURCE_AFTER_ACK_LIMIT_SECONDS = 45.0
    # The first route catalog in a real post-run gateway can arrive near the
    # generic resource-sync deadline (42.51 s in 0923 Guardian Soul Eater 1).
    # It is stronger evidence than a resource sync, so give it a separate
    # window without delaying acceptance when the row arrives earlier.
    _GATEWAY_AFTER_ACK_LIMIT_SECONDS = 90.0
    _DUPLICATE_ACK_WINDOW_SECONDS = 1.0
    _BATTLE_CONTEXT_MAX_AGE_SECONDS = 4.0 * 60.0 * 60.0

    def __init__(self) -> None:
        self._current_battle_map_id = 0
        self._current_battle_map_active = False
        self._last_battle_context_key = ""
        self._last_battle_context_at: float | None = None
        self._last_battle_leave: _BattleExitEvidence | None = None
        self._pending_ack: _PendingSceneTransition | None = None
        self._last_ack_at: float | None = None

    def observe_battle_context(
        self,
        *,
        observed_at: float,
        context_key: str = "",
    ) -> None:
        """Remember structurally verified formal-map monster evidence."""

        self._last_battle_context_at = float(observed_at)
        if str(context_key or "").strip():
            self._last_battle_context_key = str(context_key).strip()

    def observe_scene_teardown(self, *, observed_at: float) -> None:
        """Record a scene close without claiming its destination."""

        timestamp = float(observed_at)
        previous = self._last_battle_leave
        if (
            previous is not None
            and 0.0 <= timestamp - previous.left_at <= 2.0
        ):
            self._last_battle_leave = _BattleExitEvidence(
                map_id=previous.map_id,
                left_at=previous.left_at,
                scene_teardown_at=timestamp,
                context_key=previous.context_key,
                implicit_from_active_battle=(
                    previous.implicit_from_active_battle
                ),
            )
            return
        has_battle_context = (
            self._current_battle_map_active
            or (
                self._last_battle_context_at is not None
                and 0.0
                <= timestamp - self._last_battle_context_at
                <= self._BATTLE_CONTEXT_MAX_AGE_SECONDS
            )
        )
        if not has_battle_context:
            return
        self._last_battle_leave = _BattleExitEvidence(
            map_id=(
                self._current_battle_map_id
                if self._current_battle_map_active
                else 0
            ),
            left_at=timestamp,
            scene_teardown_at=timestamp,
            context_key=self._last_battle_context_key,
        )
        self._current_battle_map_active = False

    def observe_map(
        self,
        map_id: int,
        *,
        active: bool,
        observed_at: float,
    ) -> VerifiedV576HubReturn | None:
        entry = resolve_dungeon_map_id(map_id)
        if not is_battle_map(entry):
            if not active or entry is None:
                return None

            # Map 11/36/11120 is an authoritative server-side destination, unlike
            # the leader-only client selection records.  Preserve the battle
            # context before clearing it and publish the return immediately.
            # This is particularly important for a non-leader capture whose
            # only entry evidence was a formal-map entity: without a MapType-3
            # row the old code consumed the exact town row and never armed the
            # automatic stop detector.
            timestamp = float(observed_at)
            pending = self._pending_ack
            previous_leave = self._last_battle_leave
            battle_context_recent = (
                self._last_battle_context_at is not None
                and 0.0
                <= timestamp - self._last_battle_context_at
                <= self._BATTLE_CONTEXT_MAX_AGE_SECONDS
            )
            safe_hub_destination = int(map_id) in {11, 36, 11120}
            observation: VerifiedV576HubReturn | None = None
            if safe_hub_destination and (
                pending is not None
                or previous_leave is not None
                or self._current_battle_map_active
                or battle_context_recent
            ):
                exit_evidence = (
                    pending.exit
                    if pending is not None
                    else previous_leave
                )
                source_map_id = int(
                    exit_evidence.map_id
                    if exit_evidence is not None
                    else self._current_battle_map_id
                )
                left_at = float(
                    exit_evidence.left_at
                    if exit_evidence is not None
                    else (
                        self._last_battle_context_at
                        if self._last_battle_context_at is not None
                        else timestamp
                    )
                )
                observation = VerifiedV576HubReturn(
                    observed_at=timestamp,
                    battle_map_id=source_map_id,
                    battle_left_at=left_at,
                    transition_ack_at=(
                        float(pending.ack_at)
                        if pending is not None
                        else timestamp
                    ),
                    route_code=(
                        int(pending.route_code)
                        if pending is not None
                        else 0
                    ),
                    scene_teardown_at=(
                        exit_evidence.scene_teardown_at
                        if exit_evidence is not None
                        else None
                    ),
                    evidence_layout=(
                        "formal-battle-context+exact-server-hub-map-entry-v576"
                    ),
                    destination_map_id=int(map_id),
                )

            self._current_battle_map_id = 0
            self._current_battle_map_active = False
            self._last_battle_context_key = ""
            self._last_battle_context_at = None
            self._last_battle_leave = None
            self._pending_ack = None
            return observation
        timestamp = float(observed_at)
        if active:
            pending = self._pending_ack
            self._current_battle_map_id = int(map_id)
            self._current_battle_map_active = True
            if pending is None or timestamp < pending.ack_at:
                return None
            if timestamp - pending.ack_at > self._RESOURCE_AFTER_ACK_LIMIT_SECONDS:
                self._pending_ack = None
                self._last_battle_leave = None
                return None
            source_map_id = int(pending.exit.map_id)
            if _is_configured_internal_transition(source_map_id, int(map_id)):
                # A configured later room proves that this was an internal
                # transition.  Do not let a later resource sync revive it.
                self._pending_ack = None
                self._last_battle_leave = None
                return None
            if source_map_id > 0 and source_map_id != int(map_id):
                # A different dungeon/root after a teardown cannot be an
                # internal room continuation. The intervening hub is proven
                # even if its exact MapType-2 packet was not visible.
                self._pending_ack = None
                self._last_battle_leave = None
                self._last_battle_context_key = ""
                self._last_battle_context_at = None
                return self._verified_from_pending(
                    pending,
                    observed_at=pending.ack_at,
                    next_battle_map_id=int(map_id),
                    evidence_layout=(
                        "battle-scene-teardown+scene-transition-ack+"
                        "new-dungeon-map-entry-v576"
                    ),
                )
            # Same-map re-entry, or a source known only from formal-map
            # entities, remains ambiguous until the fresh scene resource sync.
            self._pending_ack = _PendingSceneTransition(
                exit=pending.exit,
                ack_at=pending.ack_at,
                route_code=pending.route_code,
                next_battle_map_id=int(map_id),
                next_battle_entered_at=timestamp,
                next_battle_is_candidate=False,
            )
            return None
        self._current_battle_map_id = int(map_id)
        self._current_battle_map_active = False
        self._last_battle_leave = _BattleExitEvidence(
            map_id=int(map_id),
            left_at=timestamp,
            context_key=self._last_battle_context_key,
        )
        return None

    def observe_map_candidate(
        self,
        map_id: int,
        *,
        observed_at: float,
    ) -> None:
        """Correlate a client-selected destination without claiming entry.

        A selection alone can be cancelled and must never change the active
        map. After a verified battle exit and transition acknowledgement it
        is still useful as a candidate: a later fresh 21 0C scene resource
        sync can complete the independent evidence chain. A same-root client
        candidate is deliberately ignored here: real captures show 14301 and
        14512 immediately after completed runs even though the player returned
        to the Garden. Only an authoritative server map entry may prove an
        internal room transition.
        """

        entry = resolve_dungeon_map_id(map_id)
        if not is_battle_map(entry):
            return
        pending = self._pending_ack
        if pending is None:
            return
        timestamp = float(observed_at)
        if timestamp < pending.ack_at:
            return
        if timestamp - pending.ack_at > self._RESOURCE_AFTER_ACK_LIMIT_SECONDS:
            self._pending_ack = None
            self._last_battle_leave = None
            return
        source_map_id = int(pending.exit.map_id)
        if _is_configured_internal_transition(source_map_id, int(map_id)):
            # Do not clear the pending exit. This client-originated packet is
            # a route request/candidate, not proof that the next room loaded.
            # A later server map confirmation will still clear the transition
            # through ``observe_map``; a gateway catalog row can instead prove
            # that the battle scene returned to a hub.
            return
        self._pending_ack = _PendingSceneTransition(
            exit=pending.exit,
            ack_at=pending.ack_at,
            route_code=pending.route_code,
            next_battle_map_id=int(map_id),
            next_battle_entered_at=timestamp,
            next_battle_is_candidate=True,
        )

    def observe_gateway_route_catalog(
        self,
        *,
        observed_at: float,
    ) -> VerifiedV576HubReturn | None:
        """Confirm that a closed battle scene reached the route gateway.

        The catalog contains available routes rather than the exact visible
        destination, so this method intentionally reports a generic hub
        return.  It is accepted either after the full leave/teardown/ack
        chain, or after an ack seeded from an exact battle map that was still
        active because a guest missed both exit records.  An isolated catalog
        refresh or acknowledgement can never stop capture.
        """

        pending = self._pending_ack
        if pending is None:
            return None
        has_explicit_teardown = pending.exit.scene_teardown_at is not None
        has_active_battle_seed = (
            pending.exit.implicit_from_active_battle
            and pending.exit.map_id > 0
        )
        if not has_explicit_teardown and not has_active_battle_seed:
            return None
        timestamp = float(observed_at)
        delta = timestamp - pending.ack_at
        if delta < 0.0:
            return None
        if delta > self._GATEWAY_AFTER_ACK_LIMIT_SECONDS:
            self._pending_ack = None
            self._last_battle_leave = None
            return None
        self._pending_ack = None
        self._last_battle_leave = None
        self._current_battle_map_id = 0
        self._current_battle_map_active = False
        self._last_battle_context_key = ""
        self._last_battle_context_at = None
        return self._verified_from_pending(
            pending,
            observed_at=timestamp,
            evidence_layout=(
                (
                    "battle-map-leave+scene-teardown+"
                    "scene-transition-ack+gateway-route-catalog-v576"
                )
                if has_explicit_teardown
                else (
                    "active-battle-map+scene-transition-ack+"
                    "gateway-route-catalog-v576"
                )
            ),
        )

    @staticmethod
    def _verified_from_pending(
        pending: _PendingSceneTransition,
        *,
        observed_at: float,
        next_battle_map_id: int | None = None,
        evidence_layout: str,
    ) -> VerifiedV576HubReturn:
        return VerifiedV576HubReturn(
            observed_at=float(observed_at),
            battle_map_id=int(pending.exit.map_id),
            battle_left_at=float(pending.exit.left_at),
            transition_ack_at=float(pending.ack_at),
            route_code=int(pending.route_code),
            scene_teardown_at=pending.exit.scene_teardown_at,
            next_battle_map_id=next_battle_map_id,
            evidence_layout=evidence_layout,
        )

    def observe_transition_ack(
        self,
        *,
        observed_at: float,
        route_code: int,
    ) -> bool:
        """Observe the generic acknowledgement and return whether it is new."""

        timestamp = float(observed_at)
        if (
            self._last_ack_at is not None
            and 0.0
            <= timestamp - self._last_ack_at
            < self._DUPLICATE_ACK_WINDOW_SECONDS
        ):
            return False
        self._last_ack_at = timestamp
        leave = self._last_battle_leave
        if leave is not None:
            delta = timestamp - leave.left_at
        else:
            delta = -1.0
        if leave is not None and 0.0 <= delta <= self._ACK_AFTER_LEAVE_LIMIT_SECONDS:
            self._pending_ack = _PendingSceneTransition(
                exit=leave,
                ack_at=timestamp,
                route_code=int(route_code),
            )
        elif (
            self._current_battle_map_active
            and self._current_battle_map_id > 0
        ):
            # Guest/non-leader captures can miss both the authoritative leave
            # row and scene teardown.  Preserve an exact active battle map as
            # a pending source, but deliberately do not clear the active map
            # and do not publish a return here.  A configured internal-room
            # server entry will cancel this pending path; only a fresh route
            # gateway catalog may confirm that the battle scene really ended.
            self._pending_ack = _PendingSceneTransition(
                exit=_BattleExitEvidence(
                    map_id=int(self._current_battle_map_id),
                    left_at=timestamp,
                    context_key=self._last_battle_context_key,
                    implicit_from_active_battle=True,
                ),
                ack_at=timestamp,
                route_code=int(route_code),
            )
        return True

    def consume_configured_terminal_return(
        self,
    ) -> VerifiedV576HubReturn | None:
        """Finish a route whose terminal destination is catalog-verified.

        The short acknowledgement is generic and must never identify a hub
        by itself.  For a configured terminal map, however, an exact map
        leave plus the independent TCP scene teardown and this acknowledgement
        are sufficient even when an accelerator hides the following town-map
        initialization and no fresh 21 0C resource packet is emitted.
        """

        pending = self._pending_ack
        if pending is None or pending.exit.scene_teardown_at is None:
            return None
        source = resolve_dungeon_map_id(pending.exit.map_id)
        destination_map_id = int(
            getattr(source, "return_map_id", 0) or 0
        )
        destination = resolve_dungeon_map_id(destination_map_id)
        if (
            not is_battle_map(source)
            or destination is None
            or is_battle_map(destination)
        ):
            return None
        self._pending_ack = None
        self._last_battle_leave = None
        self._current_battle_map_id = 0
        self._current_battle_map_active = False
        self._last_battle_context_key = ""
        self._last_battle_context_at = None
        return VerifiedV576HubReturn(
            observed_at=float(pending.ack_at),
            battle_map_id=int(pending.exit.map_id),
            battle_left_at=float(pending.exit.left_at),
            transition_ack_at=float(pending.ack_at),
            route_code=int(pending.route_code),
            scene_teardown_at=pending.exit.scene_teardown_at,
            destination_map_id=destination_map_id,
            evidence_layout=(
                "battle-map-leave+scene-teardown+scene-transition-ack+"
                "configured-terminal-destination-v576"
            ),
        )

    def observe_resource_sync(
        self,
        *,
        observed_at: float,
    ) -> VerifiedV576HubReturn | None:
        pending = self._pending_ack
        if pending is None:
            return None
        timestamp = float(observed_at)
        delta = timestamp - pending.ack_at
        if delta < 0.0:
            return None
        if delta > self._RESOURCE_AFTER_ACK_LIMIT_SECONDS:
            self._pending_ack = None
            return None
        if pending.exit.implicit_from_active_battle:
            # 21 0C is emitted during ordinary scene initialization and is
            # not independent enough to compensate for *both* missing exit
            # records. Keep the pending transition alive for a gateway row or
            # an authoritative server map entry instead.
            return None
        if pending.next_battle_map_id is not None:
            self._pending_ack = None
            self._last_battle_leave = None
            self._last_battle_context_key = ""
            self._last_battle_context_at = None
            return self._verified_from_pending(
                pending,
                # Put the inferred hub before the next battle row in history;
                # confirmation itself arrives on this resource packet.
                observed_at=pending.ack_at,
                next_battle_map_id=pending.next_battle_map_id,
                evidence_layout=(
                    "battle-scene-teardown+scene-transition-ack+" + (
                        "selected-map-candidate+resource-sync-210c-v576"
                        if pending.next_battle_is_candidate
                        else "new-scene-map-entry+resource-sync-210c-v576"
                    )
                ),
            )
        self._pending_ack = None
        self._last_battle_leave = None
        map_id = int(pending.exit.map_id)
        if map_id <= 0 or _has_configured_continuation(map_id):
            return None
        self._current_battle_map_id = 0
        self._current_battle_map_active = False
        self._last_battle_context_key = ""
        self._last_battle_context_at = None
        return self._verified_from_pending(
            pending,
            observed_at=timestamp,
            evidence_layout=(
                "battle-map-leave+scene-transition-ack+"
                "resource-sync-210c-v576"
            ),
        )


def inferred_hub_history_row(
    observation: VerifiedV576HubReturn,
) -> dict[str, object]:
    """Build a location row without fabricating an exact destination map ID."""

    destination = resolve_dungeon_map_id(observation.destination_map_id or 0)
    destination_name = (
        str(destination.display_name).strip()
        if destination is not None
        else ""
    )

    row: dict[str, object] = {
        "map_id": 0,
        "map_name": (
            f"{destination_name}（场景链识别）"
            if destination_name
            else "庭院/天堂（场景返回）"
        ),
        "nest_name": "",
        "official_name": "",
        "map_type": 2,
        "content_kind": "hub_return",
        "observed_at": float(observation.observed_at),
        "layout": observation.evidence_layout,
        "confidence": "verified_scene_return_sequence",
        "inferred": True,
        "source_map_id": int(observation.battle_map_id),
        "battle_left_at": float(observation.battle_left_at),
        "transition_ack_at": float(observation.transition_ack_at),
        "route_code": int(observation.route_code),
        "scene_teardown_at": observation.scene_teardown_at,
        "next_battle_map_id": observation.next_battle_map_id,
    }
    if destination is not None:
        row["official_name"] = destination.official_name
        row["map_type"] = destination.map_type
        row["inferred_map_id"] = destination.map_id
    return row


def promote_hub_row_to_time_space_garden(row: dict[str, object]) -> None:
    """Promote a hub row when the same capture subsequently enters a nest."""

    if row.get("content_kind") != "hub_return":
        return
    row.update(
        {
            # Keep map_id zero: the destination is inferred from the complete
            # scene path and was not carried as an exact map-id field.
            "map_name": "时空庭院（70级，场景链识别）",
            "official_name": "时空庭院（70级）",
            "content_kind": "world_zone",
            "confidence": "scene_return_followed_by_nest_entry",
            "inferred_map_id": 36,
        }
    )


class V576TcpWorldReturnTracker:
    """Confirm a terminal battle's cross-server return without a town map row.

    Some Heaven returns send 03 13 + 02 01 on the battle TCP connection,
    then either a 03 18 town scene or a fresh 03 03 player scene on the routed
    world server. The usual 03 18/03 01 town packets can be absent even though
    both TCP flows were captured. A generic player scene is only conclusive
    after a terminal battle map; neither a route nor a player spawn alone
    proves a return.
    """

    _ROUTE_AFTER_LEAVE_SECONDS = 3.0
    _BOOTSTRAP_AFTER_ROUTE_SECONDS = 45.0

    def __init__(self) -> None:
        self._leave_at = -1.0
        self._source_flow: tuple[str, int] | None = None
        self._route_at = -1.0
        self._route_ip = ""
        self._battle_map_id = 0

    @classmethod
    def is_world_transfer_start(cls, packet: object) -> bool:
        return cls._plaintext(
            packet, 28, b"\x03\x13",
            lambda body: body[4:8] == b"\x02\x00\x00\x00"
            and body[8:20] == b"\x00" * 12
            and body[24:28] == b"\x00" * 4,
        ) is not None

    @staticmethod
    def _plaintext(packet: object, length: int, opcode: bytes, extra) -> bytes | None:
        payload = getattr(packet, "payload", b"")
        if (
            str(getattr(packet, "transport", "")).upper() != "TCP"
            or packet.inferred_direction() != "server_to_client"
            or len(payload) != length + 3
            or int.from_bytes(payload[:2], "little") != len(payload)
        ):
            return None
        decoded = decode_registered_server_tcp_envelope(
            payload,
            connection_context=packet,
            validator=lambda body: (
                len(body) == length and body[2:4] == opcode and extra(body)
            ),
        )
        return decoded[0] if decoded is not None else None

    def observe(
        self, packet: object, *, observed_at: float, active_battle_map_id: int
    ) -> VerifiedV576HubReturn | None:
        map_id = int(active_battle_map_id or 0)
        if not is_battle_map(resolve_dungeon_map_id(map_id)):
            return None
        timestamp = float(observed_at)
        source_flow = (str(packet.source_ip), int(packet.source_port))
        if self.is_world_transfer_start(packet):
            self._leave_at = timestamp
            self._source_flow = source_flow
            self._route_at = -1.0
            self._route_ip = ""
            self._battle_map_id = map_id
            return None
        if (
            self._leave_at >= 0.0
            and self._source_flow == source_flow
            and 0.0 <= timestamp - self._leave_at <= self._ROUTE_AFTER_LEAVE_SECONDS
        ):
            route = self._plaintext(
                packet, 56, b"\x02\x01",
                lambda body: body[8:40].split(b"\x00", 1)[0].isascii(),
            )
            if route is not None:
                route_ip = route[8:40].split(b"\x00", 1)[0].decode("ascii")
                try:
                    ip_address(route_ip)
                except ValueError:
                    pass
                else:
                    self._route_at = timestamp
                    self._route_ip = route_ip
                    return None
        if not (
            self._route_at >= 0.0
            and str(packet.source_ip) == self._route_ip
            and source_flow != self._source_flow
            and 0.0 <= timestamp - self._route_at <= self._BOOTSTRAP_AFTER_ROUTE_SECONDS
        ):
            return None
        town_scene_load = self._plaintext(
            packet, 3140, b"\x03\x18",
            lambda body: body[4:8] == b"\x01\x00\x00\x00",
        ) is not None
        player_bootstrap = self._plaintext(
            packet, 1805, b"\x03\x03",
            lambda body: int.from_bytes(body[4:8], "little") > 0,
        ) is not None
        # The verified town-scene load proves a return even from an earlier
        # room. A generic player bootstrap does not: an internal room may
        # transfer to another server and bootstrap players the same way.
        if not town_scene_load and not (
            player_bootstrap and not has_configured_battle_continuation(map_id)
        ):
            return None
        observation = VerifiedV576HubReturn(
            observed_at=timestamp,
            battle_map_id=self._battle_map_id,
            battle_left_at=self._leave_at,
            transition_ack_at=self._route_at,
            route_code=0,
            evidence_layout=(
                "battle+tcp-world-transfer+town-scene-load-v576"
                if town_scene_load else
                "terminal-battle+tcp-world-transfer+"
                "new-scene-player-bootstrap-v576"
            ),
        )
        self._leave_at = -1.0
        self._route_at = -1.0
        self._route_ip = ""
        return observation
