"""Bounded immediate notifications from the ordered decoder, not combat snapshots."""
from collections import deque
from threading import Lock
import time

CLAW = frozenset({34849, 734017, 735017})
ACTION_ONLY = frozenset({325430, 325431, 325423, 50411, 50460, 50408, 50409, 50407, 50456, 50457, 50458, 50459, 325456})
# 海龙的冰眼/龙息按**技能号**放行，故意不校验来源模板是不是「当前 Boss」：
# 梦魇海龙里海龙飞在天上打不到，队伍实际在打地面的地狱犬，若加了
# 「必须是当前目标」的过滤，这两条会在整段地狱犬阶段消失。
MECHANICS = frozenset({325450, 325447, 50318, 50317, 50311, 50313})
CLASSIC_ACTIONS = frozenset({34715,34714,34738,34743,34734,34737,34771,34780,34781,34773,34798,34801,34850,34852,34853,34854,34856,34857,34858,34859,34879,34880,34860,34864,34849,34848,34876,34877})
# Additional starts only update visual shared cooldowns; they add no speech.
CLASSIC_ACTIONS = CLASSIC_ACTIONS | {34739}
CLASSIC_THRESHOLDS = {'503611':[78,52], '503621':[78,58,28], '503652':[49],
                      '503661':[74], '503681':[78,72,42,32,25],
                      '503682':[78,72,42,32,25], '503696':[78,72,42,32,25]}
HARD_ACTIONS = frozenset({733027,733036,734019,734020,733038,733039,734006,735006,734009,735009,735010,735011,734017,734008,735008,732001,732002,732003,732004,732005})
HARD_TEMPLATES = frozenset({4180001,4181001,4181005,4181006,4181007,4181008,4181009})
HARD_THRESHOLDS = {'4181001':[81], '4181009':[]}
SAND_ACTIONS = frozenset({729101,729102,729105,730021,730020,729085,730022,729058,729098,729100,729099,730001,730002,730003,730004,730025,730009,730010,729063,729064,729065,729066})
SAND_ACTIONS = SAND_ACTIONS | {729083}
SAND_TEMPLATES = frozenset({4150064,4150059,4150057,4150058,4150050,4160001})
SAND_THRESHOLDS = {'4150059':[97], '4150057':[92], '4150058':[97], '4150050':[86], '4160001':[26]}


class VoiceSignalFeed:
    def __init__(self):
        self._lock = Lock()
        self._rows = deque(maxlen=128)
        self._sequence = 0
        self._recent = {}
        self._health = {}
        # 每条血量包的「当前/上限/时间」。``_health`` 只为需要百分比机制的
        # Boss 维护（要有权威分母），这里对所有 Boss 都记一条，专门用于判断
        # 「血量贴地之后再也没刷新过」= 已离场。
        self._hp_seen: dict[str, tuple[int, int, int]] = {}

    def publish(self, row) -> dict | None:
        """发布一条证据行；返回**真正入库的那条**，被挡下则返回 ``None``。

        被 10 秒同键去重、或来源已判死的行会被挡下。实时链路不关心返回值；
        回放要按「正式链路到底放行了哪些行」记账，所以必须能区分，否则会把
        去重掉的行也算进播报清单，也会漏掉入库时才补上的 ``sequence``。
        """

        state = self._health.get(row['source'])
        if state and state[0] == 0 and row.get('kind') != 'boss_hp_state':
            return None
        # Keep the wire ID in the event, but deduplicate the shared laser mechanic.
        mechanic = 50408 if row['skill'] == 50409 else 50457 if row['skill'] in (50458,50459) else row['skill']
        key = (row["source"], mechanic, row.get("templateId"), row.get('kind'), row.get('threshold'), row.get('healthBand'),
               tuple(p["id"] for p in row.get("recipients", ())))
        stamp = row["timestamp"]
        with self._lock:
            previous = self._recent.get(key)
            if row.get('kind') != 'boss_hp_state' and previous is not None and 0 <= stamp - previous < 10000:
                return None
            self._recent[key] = stamp
            if len(self._recent) > 512:
                self._recent = {k: t for k, t in self._recent.items() if stamp - t < 60000}
            self._sequence += 1
            stored = dict(row, sequence=self._sequence, observedAt=time.time(),
                          publishedMonotonic=time.perf_counter())
            self._rows.append(stored)
            return stored

    def snapshot(self):
        with self._lock:
            return list(self._rows)

    def retired_sources(self, now_ms: int, exempt_templates=()) -> list[str]:
        """血量贴地后彻底不再刷新的 Boss —— 视为已离场。

        客户端在 Boss 死亡的那一刻就停止更新血条，最后一条血量常常停在
        0.0x%（梦魇海龙实测：审判者天启停在 156428/900000000 = 0.017%），
        于是「HP 归零」这条规则永远等不到。不补这一条，它放过的技能会一直
        以「已好」挂在悬浮窗里，直到下一次会话。

        两档，都是「血量已经贴地 + 不再刷新」：

        - ≤0.5% 且安静 20 秒：正常残血死亡（服务器没补 0 包）。
        - ≤5% 且安静 180 秒：阶段结束/换形态后这类 Boss 也会停更
          （P2 的空中海龙停在 0.9%，之后再没有任何血量包）。

        ``exempt_templates`` 里的模板永不判离场：场景 Boss（梦魇海龙 P2 的空中
        海龙 4237034）任何玩家都选不中，血量长期停在 0.9%，全靠技能行维持
        活跃；一旦两次机制间隔超过 180 秒，就会命中第二档被判「离场」再复活，
        悬浮窗上表现为倒计时整行闪一下。它本来就该常驻到战斗结束。
        """

        exempt = set()
        for value in exempt_templates or ():
            try:
                template_id = int(value)
            except (TypeError, ValueError):
                continue
            if template_id > 0:
                exempt.add(template_id)
        with self._lock:
            latest = max(
                (int(row.get("timestamp") or 0) for row in self._rows), default=0
            )
            clock = max(int(now_ms or 0), latest)
            # 还在放技能的 Boss 不能算离场：血量可以长时间不刷新（打不到/机制
            # 演出），但冰眼/龙息这类技能行会持续出现。
            last_row: dict[str, int] = {}
            for row in self._rows:
                source = str(row.get("source") or "")
                if source:
                    last_row[source] = max(
                        last_row.get(source, 0), int(row.get("timestamp") or 0)
                    )
            retired: list[str] = []
            for source, state in self._hp_seen.items():
                if exempt:
                    try:
                        template_id = int(str(source).rsplit("@", 1)[-1])
                    except (TypeError, ValueError):
                        template_id = 0
                    if template_id in exempt:
                        continue
                current_hp, total_hp, stamp = state
                if total_hp <= 0:
                    continue
                percent = current_hp * 100.0 / total_hp
                last_active = max(int(stamp or 0), last_row.get(str(source), 0))
                quiet = clock - last_active
                if (percent <= 0.5 and quiet >= 20_000) or (
                    percent <= 5.0 and quiet >= 180_000
                ):
                    retired.append(str(source))
        return sorted(retired)

    def health(self, event):
        if getattr(event, "part_id", 0):
            return  # Part destruction is not Boss death or a main-HP threshold.
        total = int(getattr(event, "target_max_hp", 0) or 0)
        current = int(getattr(event, "current_hp", 0) or 0)
        if total > 0 and 0 <= current <= total:
            seen = self._hp_seen.get(event.target_id)
            if seen is None or event.timestamp_ms >= seen[2]:
                self._hp_seen[event.target_id] = (current, total, event.timestamp_ms)
                if len(self._hp_seen) > 128:
                    floor = event.timestamp_ms - 600_000
                    self._hp_seen = {
                        key: value for key, value in self._hp_seen.items()
                        if value[2] >= floor
                    }
        template = str(event.target_id).rsplit('@', 1)[-1]
        # Death must reach the voice channel for EVERY boss, not just the two
        # templates with percentage-triggered mechanics. No HP denominator needed.
        previous_state = self._health.get(event.target_id)
        if previous_state and event.timestamp_ms < previous_state[2]:
            return
        if previous_state and previous_state[0] == 0:
            return  # Late HP/status packets cannot revive this encounter entity.
        if event.current_hp <= 0 and template.isdigit():
            if previous_state and previous_state[0] == 0:
                return
            self._health[event.target_id] = (0, set(), event.timestamp_ms)
            self.publish(dict(source=event.target_id, skill=0,
                timestamp=event.timestamp_ms, templateId=int(template),
                hpPercent=0, kind='boss_hp_state', healthBand='dead'))
            return
        # Share the audited entity denominator with statistics; never use a
        # stage damage budget or a separately hard-coded voice maximum.
        from .nest_hp_reference import profiles
        maximum = next((p['entities'].get(template, {}).get('full_hp')
                        for p in profiles() if p['label'] == '梦魇海龙'), None) if template in {'4235447','4237023','4237012'} else None
        if template in CLASSIC_THRESHOLDS:
            maximum = next((p['entities'].get(template, {}).get('full_hp')
                            for p in profiles() if p['label'] == '黑龙经典'), None)
        if template in HARD_THRESHOLDS:
            maximum = next((p['entities'].get(template, {}).get('full_hp')
                            for p in profiles() if p['label'] == '黑龙硬核'), None)
        if template in SAND_THRESHOLDS:
            maximum = next((p['entities'].get(template, {}).get('full_hp')
                            for p in profiles() if p['label'] == '沙龙硬核'), None)
        if maximum is None or not 0 <= event.current_hp <= maximum:
            return
        percent = event.current_hp * 100 / maximum
        key = event.target_id
        previous, seen, stamp = self._health.get(key, (None, set(), -1))
        if event.timestamp_ms < stamp:
            return
        band = (percent < 20 if template == '4235447' else percent <= 30)
        old_band = None if previous is None else (previous < 20 if template == '4235447' else previous <= 30)
        if template in CLASSIC_THRESHOLDS or template in HARD_THRESHOLDS or template in SAND_THRESHOLDS:
            # Bound live HP traffic to integer bands, with an explicit strict-49
            # boundary for the Punisher's safe-output announcements.
            band = (int(percent), percent < 49)
            old_band = None if previous is None else (int(previous), previous < 49)
        base = dict(source=key, skill=0, timestamp=event.timestamp_ms, templateId=int(template), hpPercent=percent)
        if previous is None or band != old_band or (percent == 0 and previous != 0):
            self.publish(dict(base, kind='boss_hp_state', healthBand=f'{band}:{percent == 0}'))
        thresholds = ([60,42] if template == '4235447' else [85,65,45,25] if template == '4237012'
                      else [66,62,61,56,52,46,42,36,32,26,22,16,6])
        thresholds = CLASSIC_THRESHOLDS.get(template, thresholds)
        thresholds = HARD_THRESHOLDS.get(template, thresholds)
        thresholds = SAND_THRESHOLDS.get(template, thresholds)
        # First observation establishes a baseline: never replay already passed mechanics.
        if previous is not None and percent > 0:
            for threshold in thresholds:
                if previous > threshold >= percent and threshold not in seen:
                    seen.add(threshold)
                    self.publish(dict(base, kind='boss_hp_threshold', threshold=threshold))
        else:
            seen.update(t for t in thresholds if t >= percent)
        self._health[key] = (percent, seen, event.timestamp_ms)
        if len(self._health) > 128:
            self._health = {k:v for k,v in self._health.items() if event.timestamp_ms-v[2] < 600000}


def publish_evidence(host, evidence):
    feed = getattr(host, "voice_signal_feed", None)
    if feed is None:
        return
    for item in evidence:
        skill = item.skill_index
        kind = item.evidence_kind
        recipients = []
        if skill in CLAW and kind == "status":
            target = item.affected_target_id
            try:
                actor = int(target, 16)
            except (ValueError, TypeError):
                continue
            context = host.actor_jobs.get(actor, ("", 0))
            job = context[0]
            if not job or job in {"未知", "未知职业"} or job.startswith("Boss"):
                continue
            recipients = [{"id": target, "job": job}]
        elif skill in SAND_ACTIONS and item.template_id in SAND_TEMPLATES and kind in {"protocol_action", "structured_action"}:
            pass
        elif skill in HARD_ACTIONS and item.template_id in HARD_TEMPLATES and kind in {"protocol_action", "structured_action"}:
            pass
        elif skill in CLASSIC_ACTIONS and item.template_id in {503611,503621,503652,503661,503681,503682,503696} and kind in {"protocol_action", "structured_action"}:
            pass
        elif skill in ACTION_ONLY and kind in {"protocol_action", "structured_action"}:
            pass
        elif skill in MECHANICS and kind in {"protocol_action", "structured_action"}:
            pass
        else:
            _probe_evidence(
                host,
                item,
                skill,
                kind,
                published=False,
                reason=_evidence_skip_reason(skill, kind, item.template_id),
            )
            continue
        _probe_evidence(host, item, skill, kind, published=True)
        feed.publish({"skill": skill, "source": item.source_id,
                      "timestamp": item.timestamp_ms, "templateId": item.template_id,
                      "evidenceKind": kind, "recipients": recipients})


def _evidence_skip_reason(skill: int, kind: str, template: int) -> str:
    """证据为什么没进 feed。观测日志只记「否」，排障时等于什么都没有。"""

    if skill in CLASSIC_ACTIONS:
        if int(template or 0) not in {503611, 503621, 503652, 503661, 503681, 503682, 503696}:
            return "classic:template_not_dragon_nest"
        return "classic:evidence_kind"
    if skill in HARD_ACTIONS:
        return "hard:template_or_kind"
    if skill in SAND_ACTIONS:
        return "sand:template_or_kind"
    if skill in CLAW:
        return "claw:evidence_kind"
    if skill in ACTION_ONLY or skill in MECHANICS:
        return "mechanic:evidence_kind"
    return "unknown_skill"


def _probe_evidence(
    host, item, skill, kind, *, published: bool, reason: str = ""
) -> None:
    """只写观测日志：记录每条证据是被哪个条件放行/拦下的。"""

    try:
        probe = getattr(host, "probe", None)
        if probe is None or not getattr(probe, "enabled", False):
            return
        probe.record(
            "evidence_publish" if published else "evidence_skip",
            skill=int(skill),
            # 不能叫 kind：ProbeRecorder.record(kind, **fields) 的第一个位置参数
            # 就是 kind，再传同名关键字会抛 TypeError，然后被下面的 except 吞掉
            # —— 这一整类观测从写下来那天起就没落过盘。
            evidence_kind=str(kind),
            template_id=int(getattr(item, "template_id", 0) or 0),
            source=str(getattr(item, "source_id", "")),
            classic_rule=bool(int(skill) in CLASSIC_ACTIONS),
            # 解码器的相对毫秒。抓包回放的行时间戳用的是同一个量纲，有它才能
            # 把「回放」和「实机」逐条对齐 —— 只靠日志里的 t（从开始录制算起）
            # 会固定差 0.5s 左右，对不上。
            ts_ms=int(getattr(item, "timestamp_ms", 0) or 0),
            reason=str(reason),
        )
    except Exception:
        pass
