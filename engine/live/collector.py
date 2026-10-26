from __future__ import annotations

from pathlib import Path
from typing import Callable

from .aggregator import CombatAggregator
from .history_null import NullHistoryStore
from .models import CombatSnapshot
from .npcap import NpcapPacketStream
from .profile import ClientProfile
from .sources import (
    CombatEventSource,
    JsonlReplaySource,
    V576MemorySource,
    V576DiagnosticReplaySource,
    V576NpcapSource,
    V576PcapReplaySource,
    V576PktmonTextStreamSource,
)


class LiveDpsCollector:
    def __init__(
        self,
        profile_path: Path,
        history_store: NullHistoryStore | None = None,
        toggles=None,
    ) -> None:
        self.profile_path = profile_path
        self.profile = ClientProfile.load(profile_path)
        self.aggregator = CombatAggregator(retain_source_events=True)
        # The lightweight build persists nothing; an explicit store can still
        # be injected by tests or tooling.
        self.history = history_store or NullHistoryStore()
        self.source: CombatEventSource | None = None
        self.toggles = toggles
        self.status = "未连接"

    def live_overlay_snapshot(self, effect_elapsed_seconds: float) -> dict[str, object]:
        """Defer non-overlay skill reconciliation until capture has stopped."""
        return self.aggregator.overlay_snapshot(
            max(0, round(effect_elapsed_seconds * 1000)),
            refresh_composition=False,
        )

    def freeze_live_overlay(self, effect_elapsed_seconds: float) -> CombatAggregator:
        return self.aggregator.freeze_live_overlay(
            max(0, round(effect_elapsed_seconds * 1000)),
        )

    def start_memory(self) -> None:
        self.stop()
        self.aggregator.reset()
        self.source = V576MemorySource(self.profile)
        self.history.start_session("memory")
        self.status = "正在采集"

    def load_replay(self, path: Path) -> None:
        self.stop()
        self.aggregator.reset()
        self.source = JsonlReplaySource(path)
        self.status = f"已载入：{path.name}"
        self.poll()

    def load_capture(self, path: Path) -> None:
        self.stop()
        self.aggregator.reset()
        self.source = V576PcapReplaySource(
            path,
            self.profile_path.parents[1],
            self.profile.known_players,
        )
        self.status = f"已载入抓包：{path.name}"
        self.poll()

    def load_diagnostic_capture(self, path: Path) -> None:
        self.stop()
        self.aggregator.reset()
        self.source = V576DiagnosticReplaySource(
            path,
            self.profile_path.parents[1],
            self.profile.known_players,
        )
        self.status = f"已载入统计记录：{path.name}"
        self.poll()

    def install_replay_result(
        self,
        aggregator: CombatAggregator,
        status: str,
    ) -> None:
        """Install an already decoded replay without archiving it again."""
        self.stop()
        self.aggregator = aggregator
        self.source = None
        self.status = status

    def start_pktmon_stream(self, path: Path) -> None:
        self.stop()
        self.aggregator.reset()
        self.source = V576PktmonTextStreamSource(
            path,
            self.profile_path.parents[1],
            self.profile.known_players,
        )
        self.history.start_session("pktmon")
        self.status = "正在等待实时数据"

    def start_npcap(
        self,
        udp_ports: list[int],
        tcp_ports: list[int] | None = None,
        port_provider: Callable[[], tuple[list[int], list[int]]] | None = None,
        *,
        stream: NpcapPacketStream | None = None,
        preloaded_frames: tuple[tuple[float, bytes, int, int], ...] = (),
        known_actor_names: dict[int, str] | None = None,
        identity_capture_frames: tuple[
            tuple[float, bytes, int, int], ...
        ] = (),
        session_evidence_frames: tuple[
            tuple[float, bytes, int, int], ...
        ] = (),
        prebattle_state: dict[str, object] | None = None,
        prebattle_tracker: object = None,
        toggles: object = None,
        seed_monster_entities: dict[int, int] | None = None,
    ) -> None:
        self.stop()
        self.aggregator.reset()
        # The capture controller hands the current 采集内容开关 to a live start
        # so a freshly started stream never replays events the user disabled.
        if toggles is not None:
            self.toggles = toggles
        self.source = V576NpcapSource(
            udp_ports,
            self.profile_path.parents[1],
            self.profile.known_players,
            tcp_ports=tcp_ports,
            port_provider=port_provider,
            stream=stream,
            preloaded_frames=preloaded_frames,
            known_actor_names=known_actor_names,
            identity_capture_frames=identity_capture_frames,
            session_evidence_frames=session_evidence_frames,
            prebattle_state=prebattle_state,
            prebattle_tracker=prebattle_tracker,
            toggles=self.toggles,
            # 伤害事件不进聚合器（不做 DPS），但攻击目标要单独记账：
            # 分边副本靠它判断「我在打哪只 Boss」。
            attack_target_sink=self.aggregator.observe_attack_target,
            # 待机实体探针的 Boss 档案：进本后才开统计时靠它认出 Boss。
            seed_monster_entities=seed_monster_entities,
        )
        self.history.start_session("npcap")
        self.status = "Npcap 实时统计中"

    def poll(self) -> int:
        events = self.read_events()
        self.apply_events(events)
        self.archive_events(events)
        return len(events)

    def read_events(self):
        """The controller drains the ordered source outside its statistics lock."""
        return self.source.poll() if self.source is not None else []

    def apply_events(self, events) -> None:
        for event in events:
            self.aggregator.ingest(event)

    def archive_events(self, events) -> None:
        self.history.append(events)

    def snapshot(
        self,
        effect_elapsed_seconds: float | None = None,
        *,
        include_events: bool = True,
        live_clock: bool = False,
    ) -> CombatSnapshot:
        if (
            effect_elapsed_seconds is None
            and include_events
            and not live_clock
            and self.source is None
        ):
            cached = getattr(self.aggregator, "_replay_snapshot", None)
            if isinstance(cached, CombatSnapshot):
                return cached
        current_ms = (
            max(0, round(effect_elapsed_seconds * 1000))
            if effect_elapsed_seconds is not None
            else None
        )
        return self.aggregator.snapshot(
            current_ms,
            include_events=include_events,
            live_clock=live_clock,
        )

    def overlay_snapshot(
        self,
        effect_elapsed_seconds: float,
    ) -> dict[str, object]:
        """Return the aggregator's bounded incremental overlay projection."""

        return self.aggregator.overlay_snapshot(
            max(0, round(effect_elapsed_seconds * 1000))
        )

    def request_stop(self) -> None:
        """Stop capture producers without synchronously draining the decoder."""

        source_stop = getattr(self.source, "request_stop", None)
        if callable(source_stop):
            source_stop()

    def set_toggles(self, toggles) -> None:
        """Apply feature-category switches to this collector and its live source.

        Updating a running source takes effect on the next decoded event, so a
        user can enable/disable a category mid-capture without restarting.
        """
        self.toggles = toggles
        source = getattr(self, "source", None)
        if source is not None and hasattr(source, "_toggles"):
            source._toggles = toggles

    def reset(self) -> None:
        self.history.finish_session(self.aggregator.snapshot(include_events=False))
        self.aggregator.reset()

    def stop(self, *, finalize_history: bool = True) -> None:
        try:
            if self.source is not None:
                flush_pending = getattr(self.source, "flush_pending", None)
                if callable(flush_pending):
                    pending_events = flush_pending()
                    self.history.append(pending_events)
                    for event in pending_events:
                        self.aggregator.ingest(event)
                self.source.close()
                self.source = None
        finally:
            if finalize_history and getattr(self.history, "_session_id", ""):
                self.history.finish_session(
                    self.aggregator.snapshot(include_events=False)
                )
        self.status = "未连接"

    def finish_history(self, snapshot: CombatSnapshot) -> None:
        """Finalize SQLite summaries with an already-built final snapshot."""

        self.history.finish_session(snapshot)
        # Finalization runs after live ownership was released. Wait here, never
        # in the live ingest critical section, and surface persistence errors.
        self.history.flush()

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self.history.close()

    @property
    def history_path(self) -> Path:
        return self.history.path
