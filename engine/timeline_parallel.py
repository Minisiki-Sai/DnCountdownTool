from __future__ import annotations

import atexit
import copy
import ctypes
import hashlib
import multiprocessing
import os
import pickle
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import (
    ProcessPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable
from .background_priority import (
    completed_score_futures, install_worker_priority_signal,
    score_checkpoint, worker_priority_signal,
)

from .timeline_engine import (
    PureTimelineRuntime,
    TimelineSimulationSnapshot,
    TimelineValue,
)


@dataclass(frozen=True, slots=True)
class ExtremeTimelineTask:
    """Serializable input for one independently calculated character axis."""

    owner_snapshot: TimelineSimulationSnapshot
    job: str
    team: tuple[str, ...]
    effects: tuple[dict[str, Any], ...]
    hand_multiplier: float
    window: float
    first_accel_start: float
    cast_schedule: dict[tuple[str, str], list[tuple[float, float]]] | None = None
    include_display_windows: bool = True
    build_override: dict[str, Any] | None = None
    blocked_intervals: tuple[Any, ...] = ()
    partial_cast_schedule: bool = False
    fast_long_timeline: bool = False
    long_sim_planner: bool = False
    score_refinement: bool = True
    same_environment_theory: bool = False
    provider_override: str = ""
    score_actual_casts_by_source: dict[str, list[dict[str, Any]]] | None = None
    team_objective_profile: dict[str, Any] | None = None
    boss_hp_segments: tuple[tuple[tuple[float, float], ...], ...] | None = None
    player_buff_clear_times: tuple[float, ...] = ()
    summon_removal_schedule: tuple[tuple[float, str], ...] = ()
    excluded_skill_keys: tuple[str, ...] = ()
    score_exclusion_context: Any = None
    timing_ablation_glyphs: tuple[dict[str, Any], ...] | None = None
    precomputed_timing_result: dict[str, Any] | None = None
    include_cost_hint: bool = False


_WORKER_STORE: Any = None
_WORKER_CALCULATOR: Any = None
_WORKER_COOLDOWN_ENGINE: Any = None
_WORKER_EDITOR_TYPE: type | None = None
_WORKER_SCORE_ENGINE: Any = None
_WORKER_OWNERS: OrderedDict[str, PureTimelineRuntime] = OrderedDict()
_WORKER_SCORE_SNAPSHOTS: OrderedDict[str, Any] = OrderedDict()
_EXECUTOR: ProcessPoolExecutor | None = None
_EXECUTOR_WORKERS = 0
_EXECUTOR_LOCK = threading.Lock()
_WARMUP_FUTURES: set[Any] = set()
_WARMUP_EXECUTOR_ID = 0
_LAST_TEAM_SCORE_PLAYER_DIAGNOSTICS: dict[str, Any] = {}
_WORKER_LIMIT = threading.local()
_FORMAL_SCORE_WORKER_BUDGET = threading.local()
_EXECUTOR_LAST_ACTIVE = time.monotonic()
_EXECUTOR_MONITOR_THREAD: threading.Thread | None = None
_EXECUTOR_MONITOR_STOP = threading.Event()
# ``_GAME_ACTIVE`` now means an active combat capture, not merely that the
# Dragon Nest process exists. Formal scoring may use four below-normal-priority
# workers during live combat; generic background work still obeys memory limits.
_GAME_ACTIVE = False
_GAME_PRESENT = False


@contextmanager
def timeline_worker_limit(max_workers: int):
    """Temporarily cap process-pool parallelism for latency-sensitive work."""

    previous = getattr(_WORKER_LIMIT, "value", 0)
    _WORKER_LIMIT.value = max(0, int(max_workers))
    try:
        yield
    finally:
        _WORKER_LIMIT.value = previous


@contextmanager
def formal_score_worker_budget(worker_count: int):
    """Freeze one formal score to a deterministic 4--12 process budget.

    ``configured_timeline_workers`` is also used by live calibration and small
    UI jobs, so raising its global post-capture cap would make unrelated work
    compete with the game.  A thread-local formal budget lets every phase of
    one score reuse the same pool size without changing any calculation input,
    ordering or result aggregation.
    """

    previous = getattr(_FORMAL_SCORE_WORKER_BUDGET, "value", 0)
    _FORMAL_SCORE_WORKER_BUDGET.value = max(0, int(worker_count))
    try:
        yield
    finally:
        _FORMAL_SCORE_WORKER_BUDGET.value = previous


def stable_score_worker_budget(function):
    """Do not rebuild the warm pool between phases of one replay score.

    Memory freed by one phase otherwise looks like room for more workers and
    causes a pool replacement, losing all prepared inputs. A new score may
    negotiate a new budget; game/memory guards can still restrict each phase.
    """
    @wraps(function)
    def wrapped(*args, **kwargs):
        # ``score_replay`` and the theory-plan builder are both public entry
        # points and therefore both carry this decorator.  When one calls the
        # other, keep the already negotiated budget.  Re-reading free memory
        # after decoding has allocated the combat snapshot used to downgrade
        # an outer 6/8-worker score to four workers halfway through the same
        # request, defeating both pool reuse and the progress estimate.
        active_budget = max(
            0,
            int(getattr(_FORMAL_SCORE_WORKER_BUDGET, "value", 0) or 0),
        )
        negotiated = (
            active_budget
            if active_budget > 0
            else configured_formal_score_workers(12)
        )
        with formal_score_worker_budget(negotiated):
            return function(*args, **kwargs)
    return wrapped


def _set_worker_process_low_priority() -> None:
    """Keep simulator workers below the game client's scheduler priority."""

    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetPriorityClass.restype = ctypes.c_int
        # BELOW_NORMAL_PRIORITY_CLASS
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)
    except (AttributeError, OSError):
        pass


def _score_phase_timeout_seconds() -> float:
    """Bound a process-pool score phase so a lost future cannot hang the UI."""

    try:
        configured = float(
            os.getenv("DRAGON_NEST_SCORE_PHASE_TIMEOUT_SECONDS", "900") or 900
        )
    except ValueError:
        configured = 900.0
    return max(30.0, configured)


def _available_physical_memory() -> tuple[int, int] | None:
    """Read (available, total) bytes without a polling thread or dependency."""
    if os.name != "nt":
        return None
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (name, ctypes.c_uint64) for name in (
                "total_physical", "available_physical", "total_pagefile", "available_pagefile",
                "total_virtual", "available_virtual", "available_extended_virtual",
            )
        ]
    try:
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.available_physical), int(status.total_physical)
    except (AttributeError, OSError):
        pass
    return None


def configured_timeline_workers(task_count: int = 8) -> int:
    """Return the requested worker count, bounded by tasks and CPU capacity."""

    formal_budget = max(
        0,
        int(getattr(_FORMAL_SCORE_WORKER_BUDGET, "value", 0) or 0),
    )
    if formal_budget > 0:
        limit = max(0, int(getattr(_WORKER_LIMIT, "value", 0) or 0))
        wanted = min(formal_budget, limit) if limit > 0 else formal_budget
        return max(1, min(12, int(task_count), wanted))

    try:
        configured = int(os.getenv("DRAGON_NEST_TIMELINE_WORKERS", "0") or 0)
    except ValueError:
        configured = 0
    cpu_count = max(1, os.cpu_count() or 1)
    if configured > 0:
        wanted = configured
    elif task_count >= 16 and cpu_count >= 24:
        # Full team scoring owns 16 independent theory tasks. High-thread-count
        # machines can execute them in one wave while still leaving at least
        # eight logical CPUs for the UI, capture service, and the game.
        wanted = min(12, cpu_count)
    elif task_count >= 12 and cpu_count >= 12:
        wanted = min(12, cpu_count)
    else:
        wanted = min(8, cpu_count)
    if _GAME_ACTIVE:
        # Packet ingestion and the game client share CPU and memory bandwidth.
        # Four-way live scoring was explicitly selected. Keep the memory guard
        # below, and never request more workers than available logical CPUs.
        wanted = min(wanted, min(4, max(1, cpu_count)))
    elif _GAME_PRESENT:
        # Once capture has stopped, release the combat-only 3 GiB reserve and
        # allow the completed encounter to score with a modest 2--4 workers.
        wanted = min(wanted, 4)
    else:
        wanted = min(wanted, max(1, cpu_count - 2))
    memory = _available_physical_memory()
    if memory is not None:
        available, total = memory
        # A warm worker has already paid its memory cost. Budget only growth,
        # avoiding repeated pool teardown as available RAM falls during startup.
        # Preserve the game/UI/decoder reserve while playing. Offline replay
        # scoring leaves 1.5--2 GiB for the UI/OS and still budgets 768 MiB for
        # each NEW worker; never bypass the memory guard to meet a time target.
        reserve = (
            min(3 * 1024**3, max(2 * 1024**3, total // 5))
            if _GAME_ACTIVE else min(2 * 1024**3, max(1536 * 1024**2, total // 12))
        )
        additional = max(0, available - reserve) // (768 * 1024**2)
        wanted = min(wanted, max(1, _EXECUTOR_WORKERS + additional))
    limit = max(0, int(getattr(_WORKER_LIMIT, "value", 0) or 0))
    if limit > 0:
        wanted = min(wanted, limit)
    return max(1, min(12, int(task_count), wanted))


def configured_formal_score_workers(task_count: int = 12) -> int:
    """Negotiate the stable worker budget for one formal team score.

    Formal scoring keeps four workers when at least four tasks and CPUs exist,
    including during live combat, and may grow to eight outside combat
    according to available RAM. The minimum is intentional: ordinary
    low-priority/background work retains the stricter generic policy above.
    """

    tasks = max(1, int(task_count))
    cpu_count = max(1, os.cpu_count() or 1)
    try:
        configured = int(os.getenv("DRAGON_NEST_TIMELINE_WORKERS", "0") or 0)
    except ValueError:
        configured = 0
    requested_max = min(12, cpu_count, configured if configured > 0 else 12)
    # Four processes are the predictable baseline on ordinary 16 GiB
    # machines.  Growing one worker for every nominal 512 MiB of free memory
    # was too optimistic: the replay snapshot, WebView and game allocations
    # are not reflected in a worker's private working set, so a score commonly
    # negotiated six/seven processes and then paged heavily.  Only move to the
    # eight-process tier when the machine has enough *currently available*
    # physical memory for the measured complete worker set.
    wanted = min(4, requested_max)
    if _GAME_ACTIVE:
        wanted = min(requested_max, 4)
    else:
        memory = _available_physical_memory()
        if memory is not None:
            available, _total = memory
            if available >= 10 * 1024**3:
                wanted = requested_max
            elif available >= 6 * 1024**3:
                wanted = min(requested_max, 8)
            elif available >= 11 * 1024**3 // 2:
                wanted = min(requested_max, 6)
            else:
                wanted = min(requested_max, 4)
    if tasks >= 4 and cpu_count >= 4:
        wanted = max(4, wanted)
    elif tasks >= 2 and cpu_count >= 2:
        wanted = max(2, wanted)
    limit = max(0, int(getattr(_WORKER_LIMIT, "value", 0) or 0))
    if limit > 0:
        wanted = min(wanted, limit)
    return max(1, min(tasks, wanted))


def _executor_idle_timeout_seconds() -> float:
    try:
        configured = float(
            os.getenv("DRAGON_NEST_TIMELINE_IDLE_SECONDS", "60") or 60
        )
    except ValueError:
        configured = 60.0
    return max(0.1, configured)


def _executor_busy(executor: ProcessPoolExecutor | None) -> bool:
    if executor is None:
        return False
    try:
        if bool(getattr(executor, "_pending_work_items", {})):
            return True
    except (AttributeError, TypeError):
        return True
    return any(not future.done() for future in tuple(_WARMUP_FUTURES))


def _dispose_executor(
    executor: ProcessPoolExecutor | None,
    processes: tuple[Any, ...],
) -> None:
    if executor is None:
        return
    executor.shutdown(wait=False, cancel_futures=True)
    # shutdown(wait=False) leaves already-running broken workers alive. These
    # PIDs belong exclusively to this executor, so terminate only that set.
    for process in processes:
        try:
            if process.is_alive():
                process.terminate()
        except (AttributeError, OSError, ValueError):
            continue
    for process in processes:
        try:
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
        except (AttributeError, OSError, ValueError):
            continue


def _detach_executor_locked() -> tuple[
    ProcessPoolExecutor | None, tuple[Any, ...]
]:
    global _EXECUTOR, _EXECUTOR_WORKERS, _WARMUP_EXECUTOR_ID
    executor = _EXECUTOR
    processes = tuple(
        process
        for process in getattr(executor, "_processes", {}).values()
        if process is not None
    ) if executor is not None else ()
    _EXECUTOR = None
    _EXECUTOR_WORKERS = 0
    _WARMUP_EXECUTOR_ID = 0
    _WARMUP_FUTURES.clear()
    return executor, processes


def _timeline_pool_idle_monitor() -> None:
    global _EXECUTOR_LAST_ACTIVE
    while not _EXECUTOR_MONITOR_STOP.wait(0.5):
        detached: tuple[ProcessPoolExecutor | None, tuple[Any, ...]] | None = None
        now = time.monotonic()
        with _EXECUTOR_LOCK:
            executor = _EXECUTOR
            if executor is None:
                continue
            if _executor_busy(executor):
                _EXECUTOR_LAST_ACTIVE = now
                continue
            if now - _EXECUTOR_LAST_ACTIVE >= _executor_idle_timeout_seconds():
                detached = _detach_executor_locked()
        if detached is not None:
            _dispose_executor(*detached)


def _ensure_timeline_pool_monitor_locked() -> None:
    global _EXECUTOR_MONITOR_THREAD
    if _EXECUTOR_MONITOR_THREAD is not None and _EXECUTOR_MONITOR_THREAD.is_alive():
        return
    _EXECUTOR_MONITOR_STOP.clear()
    _EXECUTOR_MONITOR_THREAD = threading.Thread(
        target=_timeline_pool_idle_monitor,
        name="DragonNestScorePoolIdleMonitor",
        daemon=True,
    )
    _EXECUTOR_MONITOR_THREAD.start()


def set_timeline_game_runtime_state(
    game_present: bool,
    combat_active: bool,
) -> None:
    """Apply separate idle-client and live-combat worker policies."""

    global _GAME_ACTIVE, _GAME_PRESENT, _EXECUTOR_LAST_ACTIVE
    game_present = bool(game_present)
    combat_active = bool(combat_active) and game_present
    detached: tuple[ProcessPoolExecutor | None, tuple[Any, ...]] | None = None
    with _EXECUTOR_LOCK:
        if _GAME_PRESENT == game_present and _GAME_ACTIVE == combat_active:
            return
        _GAME_PRESENT = game_present
        _GAME_ACTIVE = combat_active
        _EXECUTOR_LAST_ACTIVE = time.monotonic()
        executor = _EXECUTOR
        if (
            game_present
            and executor is not None
            and not _executor_busy(executor)
            and _EXECUTOR_WORKERS > configured_timeline_workers(10_000)
        ):
            detached = _detach_executor_locked()
    if detached is not None:
        _dispose_executor(*detached)


def set_timeline_game_active(active: bool) -> None:
    """Backward-compatible shorthand treating active as live combat."""

    set_timeline_game_runtime_state(active, active)


def timeline_process_pool_status() -> dict[str, Any]:
    """Return compact diagnostics without retaining any worker handles."""

    with _EXECUTOR_LOCK:
        executor = _EXECUTOR
        processes = tuple(
            process
            for process in getattr(executor, "_processes", {}).values()
            if process is not None
        ) if executor is not None else ()
        return {
            "active": executor is not None,
            "workers": int(_EXECUTOR_WORKERS),
            "startedWorkers": len(processes),
            "aliveWorkers": sum(
                1
                for process in processes
                if bool(getattr(process, "is_alive", lambda: False)())
            ),
            "busy": _executor_busy(executor),
            "gameActive": bool(_GAME_ACTIVE),
            "gamePresent": bool(_GAME_PRESENT),
            "combatActive": bool(_GAME_ACTIVE),
            "idleSeconds": max(
                0.0,
                time.monotonic() - _EXECUTOR_LAST_ACTIVE,
            ),
            "idleTimeoutSeconds": _executor_idle_timeout_seconds(),
        }


def _initialize_timeline_worker(priority_signal=None) -> None:
    global _WORKER_STORE, _WORKER_CALCULATOR, _WORKER_COOLDOWN_ENGINE, _WORKER_EDITOR_TYPE

    _set_worker_process_low_priority()
    if priority_signal is not None:
        install_worker_priority_signal(priority_signal)
    score_checkpoint(force=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from .calculator import BuildCalculator
    from .cooldown import SkillCooldownEngine
    from .data_loader import DataStore
    from .legacy_timeline import _load_legacy_method_source_type

    _WORKER_STORE = DataStore.load()
    _WORKER_CALCULATOR = BuildCalculator(_WORKER_STORE)
    _WORKER_COOLDOWN_ENGINE = SkillCooldownEngine(
        _WORKER_STORE.skill_cooldown_rules(),
        _WORKER_STORE.skill_reference_cooldown,
    )
    _WORKER_EDITOR_TYPE = _load_legacy_method_source_type()


def _worker_owner(snapshot: TimelineSimulationSnapshot) -> PureTimelineRuntime:
    owner = _WORKER_OWNERS.get(snapshot.fingerprint)
    if owner is not None:
        _WORKER_OWNERS.move_to_end(snapshot.fingerprint)
        return owner
    if _WORKER_EDITOR_TYPE is None:
        _initialize_timeline_worker()
    owner = PureTimelineRuntime(
        snapshot,
        store=_WORKER_STORE,
        calculator=_WORKER_CALCULATOR,
        cooldown_engine=_WORKER_COOLDOWN_ENGINE,
        method_source_type=_WORKER_EDITOR_TYPE,
        level_cap=70,
    )
    owner._extreme_timeline_process_events = False
    owner._extreme_timeline_progress_callback = None
    owner._extreme_timeline_result_cache = {}
    owner._extreme_timeline_seed_cache = {}
    owner._extreme_timeline_panel_cache_pool = {}
    _WORKER_OWNERS[snapshot.fingerprint] = owner
    while len(_WORKER_OWNERS) > 2:
        _WORKER_OWNERS.popitem(last=False)
    return owner


@contextmanager
def ephemeral_timing_axis_cache(owner: Any, enabled: bool):
    """Retain prepared inputs, but do not retain six groups of full timelines."""
    if not enabled:
        yield
        return
    missing = object()
    previous = getattr(owner, "_extreme_timeline_result_cache", missing)
    owner._extreme_timeline_result_cache = dict(previous) if isinstance(previous, dict) else {}
    try:
        yield
    finally:
        if previous is missing:
            delattr(owner, "_extreme_timeline_result_cache")
        else:
            owner._extreme_timeline_result_cache = previous


def summarize_timing_axis(result: dict[str, Any], glyphs: Any) -> dict[str, Any]:
    from .live.theory_plan_builder import _team_score_apply_fixed_axis_damage_glyphs
    _team_score_apply_fixed_axis_damage_glyphs(result, list(glyphs))
    summary = {"dps": result.get("dps"), "_timing_glyphs_applied": True}
    if any(isinstance(row, dict) and row.get('kind') == 'damage' for row in result.get('segments', ())):
        summary['_timing_damage_events'] = tuple(
            (row['start'], row['effective_score']) for row in result['segments']
            if isinstance(row, dict) and row.get('kind') == 'damage'
            and 'start' in row and 'effective_score' in row
        )
    return summary


def calculate_timing_axis_group(tasks: tuple[ExtremeTimelineTask, ...]) -> list[dict[str, Any]]:
    """Keep a recipient's variants on one worker; create a fresh axis each time."""
    if any(task.timing_ablation_glyphs is None for task in tasks):
        raise ValueError("Only timing ablations may use grouped execution")
    return [calculate_extreme_timeline_task(task) for task in tasks]


def calculate_extreme_timeline_task(task: ExtremeTimelineTask) -> dict[str, Any]:
    """Calculate one axis inside a worker without touching any Qt widget."""

    score_checkpoint(force=True)
    if task.precomputed_timing_result is not None:
        return task.precomputed_timing_result
    profile_task = os.getenv("DRAGON_NEST_TIMELINE_PROFILE") == "1"
    owner_started = time.perf_counter()
    owner = _worker_owner(task.owner_snapshot)
    owner_elapsed = time.perf_counter() - owner_started
    owner.skill_long_accel_start = TimelineValue(task.first_accel_start)
    owner._extreme_timeline_process_events = False
    owner._extreme_timeline_progress_callback = None
    def simulate_raw(
        excluded_skill_keys: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        return owner._extreme_team_timeline_result(
            task.job,
            list(task.team),
            list(task.effects),
            task.hand_multiplier,
            task.window,
            cast_schedule=task.cast_schedule,
            include_display_windows=task.include_display_windows,
            build_override=task.build_override,
            blocked_intervals=list(task.blocked_intervals),
            partial_cast_schedule=task.partial_cast_schedule,
            fast_long_timeline=task.fast_long_timeline,
            long_sim_planner=task.long_sim_planner,
            score_refinement=task.score_refinement,
            same_environment_theory=task.same_environment_theory,
            provider_override=task.provider_override,
            score_actual_casts_by_source=task.score_actual_casts_by_source,
            team_objective_profile=task.team_objective_profile,
            boss_hp_segments=task.boss_hp_segments,
            player_buff_clear_times=task.player_buff_clear_times,
            summon_removal_schedule=task.summon_removal_schedule,
            **({"timing_summary_only": True} if task.timing_ablation_glyphs is not None else {}),
            excluded_skill_keys=set(
                task.excluded_skill_keys
                if excluded_skill_keys is None
                else excluded_skill_keys
            ),
        )

    def simulate(excluded_skill_keys: tuple[str, ...] | None = None) -> dict[str, Any]:
        with ephemeral_timing_axis_cache(owner, task.timing_ablation_glyphs is not None):
            return simulate_raw(excluded_skill_keys)

    simulation_started = time.perf_counter()
    profile_job = os.getenv("DRAGON_NEST_TIMELINE_PROFILE_JOB", "")
    profile_dir = os.getenv("DRAGON_NEST_TIMELINE_PROFILE_DIR", "")
    if profile_dir and profile_job == task.job:
        import cProfile
        from pathlib import Path

        profiler = cProfile.Profile()
        result = profiler.runcall(simulate)
        phase = "display" if task.include_display_windows else "neutral"
        profile_path = Path(profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(
            str(profile_path / f"timeline_{task.job}_{phase}_{os.getpid()}.prof")
        )
    else:
        result = simulate()
    exclusion_context = task.score_exclusion_context
    if (
        isinstance(exclusion_context, dict)
        and not task.excluded_skill_keys
    ):
        prefetch_started = time.perf_counter()
        try:
            from .live.event_platform import event_platform_for
            from .live.scoring import TeamScoreEngine
            from .live.theory_plan_builder import (
                _team_score_apply_fixed_axis_damage_glyphs,
                _team_score_refine_fixed_axis_damage_glyphs,
            )

            selected_rows = copy.deepcopy(
                exclusion_context.get("glyph_rows", [])
            )
            if bool(exclusion_context.get("glyph_auto", True)):
                selected_rows = _team_score_refine_fixed_axis_damage_glyphs(
                    _WORKER_STORE,
                    str(exclusion_context.get("real_job", task.job)),
                    selected_rows,
                    result,
                    count=4,
                )
            _team_score_apply_fixed_axis_damage_glyphs(result, selected_rows)
            for key, value in exclusion_context.get("plan_metadata", {}).items():
                result[str(key)] = copy.deepcopy(value)
            result["_score_glyphs"] = copy.deepcopy(selected_rows)
            result["_score_pipeline_glyphs_prepared"] = True

            score_snapshot = _materialize_score_snapshot(
                exclusion_context.get("snapshot")
            )
            event_platform_for(score_snapshot)
            score_engine = TeamScoreEngine(_WORKER_STORE)
            score_engine.set_boss_profile(
                str(exclusion_context.get("boss_name", "红龙佩达")),
                copy.deepcopy(exclusion_context.get("boss_profile", {})),
            )
            output_windows = tuple(
                tuple(row)
                for row in exclusion_context.get("output_windows", ())
            )
            boss_target_ids = exclusion_context.get("boss_target_ids")
            players_by_source = {
                str(player.source_id): player
                for player in event_platform_for(score_snapshot).players
            }
            replan_cache: dict[
                tuple[str, ...], dict[str, Any]
            ] = {}
            prefetched: dict[str, dict[str, Any]] = {}
            for source_id in exclusion_context.get("source_ids", ()):
                normalized_source = str(source_id)
                player = players_by_source.get(normalized_source)
                if player is None:
                    continue
                exclusion = score_engine._score_player(
                    score_snapshot,
                    player,
                    output_windows,
                    result,
                    boss_target_ids,
                    None,
                    (),
                    (),
                    True,
                    exclusion_only=True,
                )
                if (
                    not isinstance(exclusion, tuple)
                    or len(exclusion) != 2
                    or not isinstance(exclusion[0], frozenset)
                ):
                    continue
                excluded = tuple(sorted(str(skill) for skill in exclusion[0]))
                if not excluded:
                    continue
                if excluded not in replan_cache:
                    replan_cache[excluded] = simulate(excluded)
                prefetched[normalized_source] = {
                    "excluded_skill_keys": excluded,
                    "high_value_skill_keys": tuple(
                        sorted(str(skill) for skill in exclusion[1])
                    ),
                    "result": copy.deepcopy(replan_cache[excluded]),
                }
            result["_score_pipeline_exclusion_results"] = prefetched
            result["_score_pipeline_prefetch_seconds"] = (
                time.perf_counter() - prefetch_started
            )
        except Exception as exc:
            # The parent always repeats the official exclusion decision and
            # performs the original full replan whenever this optimization is
            # unavailable or mismatched.
            result["_score_pipeline_exclusion_error"] = repr(exc)
    if task.timing_ablation_glyphs is not None:
        result = summarize_timing_axis(result, task.timing_ablation_glyphs)
    # Scheduling hint only: never used by the planner or scoring formula.
    if task.include_cost_hint:
        result["_parallel_cost_seconds"] = time.perf_counter() - simulation_started
    if profile_task:
        result["_parallel_profile"] = {
            "pid": os.getpid(),
            "owner_seconds": owner_elapsed,
            "simulation_seconds": time.perf_counter() - simulation_started,
        }
    return result


@dataclass(frozen=True, slots=True)
class SupportBenchmarkChunkTask:
    """A small group of profession support benchmarks sharing one context."""

    jobs: tuple[str, ...]
    context: Any
    output_windows: tuple[tuple[float, float], ...]
    modifier_windows: tuple[tuple[float, float, float, float], ...]
    end_seconds: float
    boss_name: str
    boss_profile: dict[str, float]
    crit_profiles_by_source: dict[str, tuple[float, float]]
    neutral_profiles_by_source: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeamScorePlayerTask:
    """One independent player score against a shared immutable snapshot."""

    snapshot: Any
    player: Any
    output_windows: tuple[tuple[float, float], ...]
    theory_plan: dict[str, Any] | None
    boss_target_ids: frozenset[str] | None
    support_context: Any
    specialty_windows: tuple[tuple[float, float], ...]
    death_windows: tuple[tuple[float, float], ...]
    authoritative_cast_axis: bool
    boss_name: str
    boss_profile: dict[str, float]
    crit_profiles_by_source: dict[str, tuple[float, float]]
    neutral_profiles_by_source: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeamScorePlayerBatchTask:
    """Several player scores sharing one serialized immutable context."""

    snapshot: Any
    items: tuple[tuple[int, Any, dict[str, Any] | None], ...]
    output_windows: tuple[tuple[float, float], ...]
    boss_target_ids: frozenset[str] | None
    support_context: Any
    specialty_windows: tuple[tuple[float, float], ...]
    death_windows: tuple[tuple[float, float], ...]
    authoritative_cast_axis: bool
    boss_name: str
    boss_profile: dict[str, float]
    crit_profiles_by_source: dict[str, tuple[float, float]]
    exclusion_only: bool = False
    neutral_profiles_by_source: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SerializedScoreSnapshot:
    """One pre-serialized immutable score snapshot shared by batch tasks."""

    key: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class SerializedScoreValue:
    """Pre-pickled shared input that must remain task-local after loading."""

    payload: bytes


def serialized_score_snapshot(snapshot: Any) -> SerializedScoreSnapshot:
    """Serialize one immutable score snapshot for reuse by worker tasks."""

    payload = pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL)
    return SerializedScoreSnapshot(
        key=hashlib.sha256(payload).hexdigest(),
        payload=payload,
    )


def _materialize_score_snapshot(reference: Any) -> Any:
    if not isinstance(reference, SerializedScoreSnapshot):
        return reference
    cached = _WORKER_SCORE_SNAPSHOTS.get(reference.key)
    if cached is not None:
        _WORKER_SCORE_SNAPSHOTS.move_to_end(reference.key)
        return cached
    snapshot = pickle.loads(reference.payload)
    _WORKER_SCORE_SNAPSHOTS[reference.key] = snapshot
    while len(_WORKER_SCORE_SNAPSHOTS) > 2:
        _WORKER_SCORE_SNAPSHOTS.popitem(last=False)
    return snapshot


def _materialize_score_value(reference: Any) -> Any:
    if isinstance(reference, SerializedScoreValue):
        return pickle.loads(reference.payload)
    return reference


def calculate_support_benchmark_chunk(
    task: SupportBenchmarkChunkTask,
) -> dict[str, float]:
    """Evaluate independent all-job support calibration in a warm worker."""

    global _WORKER_SCORE_ENGINE
    score_checkpoint(force=True)
    if _WORKER_STORE is None:
        _initialize_timeline_worker()
    if _WORKER_SCORE_ENGINE is None:
        from .live.scoring import TeamScoreEngine

        _WORKER_SCORE_ENGINE = TeamScoreEngine(_WORKER_STORE)
    _WORKER_SCORE_ENGINE.set_boss_profile(task.boss_name, task.boss_profile)
    _WORKER_SCORE_ENGINE._score_crit_profiles_by_source = copy.deepcopy(
        task.crit_profiles_by_source
    )
    _WORKER_SCORE_ENGINE._score_neutral_profiles_by_source = dict(task.neutral_profiles_by_source)
    _WORKER_SCORE_ENGINE._configured_support_recipient_gain_cache.clear()
    context = _materialize_score_value(task.context)
    return {
        job: _WORKER_SCORE_ENGINE._counterfactual_support_index_for_job(
            job,
            context,
            task.output_windows,
            task.modifier_windows,
            task.end_seconds,
        )
        for job in task.jobs
    }


def calculate_team_score_player(task: TeamScorePlayerTask) -> Any:
    """Calculate one player's unchanged score formula in a warm worker."""

    score_checkpoint(force=True)
    started = time.perf_counter()
    if _WORKER_STORE is None:
        _initialize_timeline_worker()
    from .live.scoring import TeamScoreEngine

    # Per-player scoring mutates several transient caches. A fresh engine keeps
    # worker scheduling from affecting the result while the expensive store and
    # process remain warm and shared.
    score_engine = TeamScoreEngine(_WORKER_STORE)
    score_engine.set_boss_profile(task.boss_name, task.boss_profile)
    score_engine._score_crit_profiles_by_source = copy.deepcopy(
        task.crit_profiles_by_source
    )
    score_engine._score_neutral_profiles_by_source = dict(task.neutral_profiles_by_source)
    score = score_engine._score_player(
        task.snapshot,
        task.player,
        task.output_windows,
        task.theory_plan,
        task.boss_target_ids,
        task.support_context,
        task.specialty_windows,
        task.death_windows,
        task.authoritative_cast_axis,
    )
    return score, time.perf_counter() - started


def calculate_team_score_player_batch(
    task: TeamScorePlayerBatchTask,
) -> tuple[tuple[int, Any, float], ...]:
    """Score a balanced player batch after serializing shared inputs once."""

    score_checkpoint(force=True)
    if _WORKER_STORE is None:
        _initialize_timeline_worker()
    from .live.event_platform import event_platform_for
    from .live.scoring import TeamScoreEngine

    snapshot = _materialize_score_snapshot(task.snapshot)
    # Materialize and index the shared immutable snapshot once per worker
    # batch. Fresh TeamScoreEngine instances below then perform indexed
    # lookups instead of rescanning every combat packet for every player.
    event_platform_for(snapshot)
    support_context = _materialize_score_value(task.support_context)
    resolved: list[tuple[int, Any, float]] = []
    for player_index, player, theory_plan in task.items:
        score_checkpoint(force=True)
        started = time.perf_counter()
        score_engine = TeamScoreEngine(_WORKER_STORE)
        score_engine.set_boss_profile(task.boss_name, task.boss_profile)
        score_engine._score_crit_profiles_by_source = copy.deepcopy(
            task.crit_profiles_by_source
        )
        score_engine._score_neutral_profiles_by_source = dict(task.neutral_profiles_by_source)
        score = score_engine._score_player(
            snapshot,
            player,
            task.output_windows,
            theory_plan,
            task.boss_target_ids,
            support_context,
            task.specialty_windows,
            task.death_windows,
            task.authoritative_cast_axis,
            exclusion_only=task.exclusion_only,
        )
        resolved.append(
            (player_index, score, time.perf_counter() - started)
        )
    return tuple(resolved)


def calculate_team_score_players_parallel(
    snapshot: Any,
    players: tuple[Any, ...],
    output_windows: tuple[tuple[float, float], ...],
    theory_plans: dict[str, dict[str, Any]],
    boss_target_ids: frozenset[str] | None,
    support_context: Any,
    specialty_windows: tuple[tuple[float, float], ...],
    authoritative_cast_axis: bool,
    boss_name: str,
    boss_profile: dict[str, float],
    crit_profiles_by_source: dict[str, tuple[float, float]],
    death_windows: tuple[tuple[float, float], ...] = (),
    exclusion_only: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
    neutral_profiles_by_source: dict[str, float] | None = None,
) -> tuple[Any, ...] | None:
    """Calculate independent player scores concurrently with exact inputs."""

    global _LAST_TEAM_SCORE_PLAYER_DIAGNOSTICS
    if len(players) <= 1 or configured_timeline_workers(len(players)) <= 1:
        return None
    executor = timeline_process_pool(len(players))
    if executor is None:
        return None
    player_entries = [
        (
            player_index,
            player,
            theory_plans.get(player.source_id),
        )
        for player_index, player in enumerate(players)
    ]
    use_serialized_snapshot = (
        os.getenv("DRAGON_NEST_SCORE_SERIALIZED_SNAPSHOT", "1").strip()
        not in {"0", "false", "off", "no"}
    )
    if use_serialized_snapshot:
        task_snapshot: Any = serialized_score_snapshot(snapshot)
    else:
        task_snapshot = snapshot
    task_support_context: Any = support_context
    if support_context is not None and use_serialized_snapshot:
        task_support_context = SerializedScoreValue(
            pickle.dumps(
                support_context,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        )
    try:
        configured_batches = int(
            os.getenv("DRAGON_NEST_SCORE_PLAYER_BATCHES", "0") or 0
        )
    except ValueError:
        configured_batches = 0
    batch_count = min(
        configured_timeline_workers(len(players)),
        (
            max(1, configured_batches)
            if configured_batches > 0
            # The immutable CombatSnapshot is serialized once and reused by
            # workers, so one batch per party member now provides the best CPU
            # utilization without repeating the former multi-megabyte parent
            # serialization cost.  Keep the override for diagnostics and
            # machines configured with fewer worker processes.
            else max(1, min(8, len(players)))
        ),
    )
    batch_items: list[list[tuple[int, Any, dict[str, Any] | None]]] = [
        [] for _index in range(batch_count)
    ]
    batch_loads = [0 for _index in range(batch_count)]

    def estimated_load(
        entry: tuple[int, Any, dict[str, Any] | None],
    ) -> int:
        _player_index, player, plan = entry
        segments = plan.get("segments", ()) if isinstance(plan, dict) else ()
        return max(1, len(segments)) + 10 * len(getattr(player, "skills", ()))

    for entry in sorted(player_entries, key=estimated_load, reverse=True):
        target = min(range(batch_count), key=lambda index: batch_loads[index])
        batch_items[target].append(entry)
        batch_loads[target] += estimated_load(entry)
    tasks = tuple(
        TeamScorePlayerBatchTask(
            snapshot=task_snapshot,
            items=tuple(sorted(items, key=lambda entry: entry[0])),
            output_windows=output_windows,
            boss_target_ids=boss_target_ids,
            support_context=task_support_context,
            specialty_windows=specialty_windows,
            death_windows=death_windows,
            authoritative_cast_axis=authoritative_cast_axis,
            boss_name=boss_name,
            boss_profile=copy.deepcopy(boss_profile),
            crit_profiles_by_source=copy.deepcopy(crit_profiles_by_source),
            exclusion_only=exclusion_only,
            neutral_profiles_by_source=dict(neutral_profiles_by_source or {}),
        )
        for items in batch_items
        if items
    )
    started = time.perf_counter()
    try:
        futures = [
            executor.submit(calculate_team_score_player_batch, task)
            for task in tasks
        ]
    except Exception:
        return None
    resolved: list[tuple[int, Any, float]] = []
    completed_players = 0
    try:
        completed_futures = completed_score_futures(
            futures,
            timeout=_score_phase_timeout_seconds(),
        )
        for future in completed_futures:
            try:
                batch = future.result()
            except Exception:
                reset_timeline_process_pool()
                return None
            for player_index, score, worker_seconds in sorted(
                batch, key=lambda item: item[0]
            ):
                resolved.append((player_index, score, worker_seconds))
                completed_players += 1
                if progress_callback is not None:
                    # Report from the parent process as each worker result arrives.
                    # Calling this only after collecting every future made the UI
                    # jump directly from 0/8 to 8/8.
                    progress_callback(
                        completed_players,
                        len(players),
                        players[player_index].job,
                    )
    except FuturesTimeoutError:
        # A broken/lost result must not leave the single heavy-job worker
        # blocked forever.  Discard the pool and let the caller run the exact
        # sequential fallback.
        reset_timeline_process_pool()
        return None
    resolved.sort(key=lambda item: item[0])
    _LAST_TEAM_SCORE_PLAYER_DIAGNOSTICS = {
        "wall_seconds": time.perf_counter() - started,
        "batch_count": len(tasks),
        "tasks": tuple(
            {
                "job": players[player_index].job,
                "worker_seconds": worker_seconds,
            }
            for player_index, _score, worker_seconds in resolved
        ),
    }
    return tuple(score for _player_index, score, _worker_seconds in resolved)


def last_team_score_player_diagnostics() -> dict[str, Any]:
    return copy.deepcopy(_LAST_TEAM_SCORE_PLAYER_DIAGNOSTICS)


def calculate_support_benchmarks_parallel(
    jobs: tuple[str, ...],
    context: Any,
    output_windows: tuple[tuple[float, float], ...],
    modifier_windows: tuple[tuple[float, float, float, float], ...],
    end_seconds: float,
    boss_name: str,
    boss_profile: dict[str, float],
    crit_profiles_by_source: dict[str, tuple[float, float]],
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    neutral_profiles_by_source: dict[str, float] | None = None,
) -> dict[str, float] | None:
    """Run exact profession benchmarks concurrently, preserving all formulas."""

    worker_count = configured_timeline_workers(len(jobs))
    if worker_count <= 1 or len(jobs) <= 1:
        return None
    executor = timeline_process_pool(worker_count)
    if executor is None:
        return None
    chunks = tuple(
        tuple(jobs[index::worker_count]) for index in range(worker_count)
    )
    # This large immutable context is identical for every profession chunk.
    # Encode it once instead of traversing the complete event graph again for
    # each worker submission. Each worker still receives a private value.
    task_context = SerializedScoreValue(
        pickle.dumps(context, protocol=pickle.HIGHEST_PROTOCOL)
    )
    tasks = tuple(
        SupportBenchmarkChunkTask(
            jobs=chunk,
            context=task_context,
            output_windows=output_windows,
            modifier_windows=modifier_windows,
            end_seconds=end_seconds,
            boss_name=boss_name,
            boss_profile=copy.deepcopy(boss_profile),
            crit_profiles_by_source=copy.deepcopy(crit_profiles_by_source),
            neutral_profiles_by_source=dict(neutral_profiles_by_source or {}),
        )
        for chunk in chunks
        if chunk
    )
    try:
        futures = [
            executor.submit(calculate_support_benchmark_chunk, task)
            for task in tasks
        ]
        merged: dict[str, float] = {}
        completed_jobs = 0
        for future in completed_score_futures(futures, _score_phase_timeout_seconds()):
            chunk_result = future.result()
            merged.update(chunk_result)
            completed_jobs += len(chunk_result)
            if progress_callback is not None:
                latest_job = next(reversed(chunk_result), "")
                progress_callback(
                    min(len(jobs), completed_jobs),
                    len(jobs),
                    f"辅助基准：{latest_job}",
                )
        return merged
    except Exception:
        # The caller retains the exact sequential implementation as a safe
        # fallback, including environments where multiprocessing is disabled.
        reset_timeline_process_pool()
        return None


def timeline_process_pool(task_count: int = 8) -> ProcessPoolExecutor | None:
    """Return the persistent timeline pool, or None when parallelism is disabled."""

    global _EXECUTOR, _EXECUTOR_WORKERS, _EXECUTOR_LAST_ACTIVE
    workers = configured_timeline_workers(task_count)
    if workers <= 1:
        return None
    with _EXECUTOR_LOCK:
        _EXECUTOR_LAST_ACTIVE = time.monotonic()
        _ensure_timeline_pool_monitor_locked()
        # A small two/four-axis request must not tear down an already warm
        # eight-worker pool. Team scoring commonly follows one of those small
        # requests and would otherwise pay the full Windows spawn/data-load
        # cost again.
        strict_limit = max(
            0,
            int(getattr(_WORKER_LIMIT, "value", 0) or 0),
        )
        runtime_limit = strict_limit
        if _GAME_PRESENT:
            game_limit = configured_timeline_workers(10_000)
            runtime_limit = (
                min(runtime_limit, game_limit)
                if runtime_limit > 0
                else game_limit
            )
        if (
            _EXECUTOR is not None
            and _EXECUTOR_WORKERS >= workers
            and (runtime_limit <= 0 or _EXECUTOR_WORKERS <= runtime_limit)
        ):
            return _EXECUTOR
        if _EXECUTOR is not None and _executor_busy(_EXECUTOR):
            # Never terminate another score that is still resolving. The
            # active limit is applied as soon as that pool becomes idle.
            return _EXECUTOR
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False, cancel_futures=True)
        _EXECUTOR = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_timeline_worker,
            initargs=(worker_priority_signal(),),
        )
        _EXECUTOR_WORKERS = workers
        return _EXECUTOR


def _timeline_worker_warmup(delay_seconds: float = 0.25) -> int:
    """Force one executor slot through its initializer and return its PID."""

    time.sleep(max(0.0, float(delay_seconds)))
    return os.getpid()


def warm_timeline_process_pool(task_count: int = 8) -> None:
    """Start timeline workers in the background before the first score run."""

    global _WARMUP_EXECUTOR_ID
    executor = timeline_process_pool(task_count)
    if executor is None:
        return
    with _EXECUTOR_LOCK:
        if _WARMUP_EXECUTOR_ID == id(executor):
            return
        _WARMUP_EXECUTOR_ID = id(executor)
        for _index in range(_EXECUTOR_WORKERS):
            future = executor.submit(_timeline_worker_warmup)
            _WARMUP_FUTURES.add(future)
            future.add_done_callback(_WARMUP_FUTURES.discard)


def reset_timeline_process_pool() -> None:
    global _EXECUTOR_LAST_ACTIVE
    with _EXECUTOR_LOCK:
        detached = _detach_executor_locked()
        _EXECUTOR_LAST_ACTIVE = time.monotonic()
    _dispose_executor(*detached)


def _shutdown_timeline_process_pool() -> None:
    _EXECUTOR_MONITOR_STOP.set()
    reset_timeline_process_pool()


atexit.register(_shutdown_timeline_process_pool)
