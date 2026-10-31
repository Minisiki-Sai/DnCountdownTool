"""梦魇龙巢专属：换边（置换）印记的即时播报。

770008「地脉诅咒」是服务端给**玩家自己**挂的状态（self-cast），包里没有
Boss actor，所以现有的两条路都看不到它：

* ``decode_v576_boss_skill_starts`` 要求 actor 是 Boss；
* ``_boss_status_action_events`` 要求 ``status.source_actor_id in boss_actor_ids``。

这里单开一条分支：

1. 先用 marker 预筛 + 实例 ID 验证，从包里解出 ``(玩家, 770008)``；
2. 再判定副本 —— 优先 ``detected_nest_key``，副本还没识别出来时（中途点开
   统计的常见情况）从当前已知 Boss 反推，天启 4235447 / 狮蝎 4235448 都能
   反推出 ``nightmare_sea_dragon``；
3. 命中后以**固定 source** 入播报。

``source`` 必须固定：前端 ``SeaReminderClock`` 用 ``source`` 做倒计时主键，
两个玩家各带自己的 ID 会让同一次换边播两遍、并起两个倒计时。
真实玩家 ID 放进 ``recipients``，只作事实记录。
"""
from .protocol import decode_v576_self_state_applies

SWAP_SKILL = 770008
SWAP_NEST = "nightmare_sea_dragon"
SWAP_SOURCE = "swap"
SWAP_KIND = "swap_state"


def _is_swap_nest(host) -> bool:
    """当前是不是梦魇龙巢。

    已识别成别的副本就直接否；还没识别时按已知 Boss 反推 —— 中途点开统计
    时 ``detected_nest_key`` 是空的，只能靠这条兜底。
    """
    key = str(getattr(host, "detected_nest_key", "") or "")
    if key == SWAP_NEST:
        return True

    from engine.native.nest_inference import known_nest_keys

    if key and key in known_nest_keys():
        return False

    entities = getattr(host, "monster_entities", None)
    if not entities:
        return False
    from .nest_recognizer import resolve_nest_template

    for actor_id in getattr(host, "boss_actor_ids", ()) or ():
        entity = entities.get(actor_id)
        if entity is None:
            continue
        resolution = resolve_nest_template(getattr(entity, "template_id", 0))
        if resolution is not None and resolution.profile.key == SWAP_NEST:
            return True
    return False


def publish_nest_swap_state(host, packet):
    """把一次换边印记发进播报 feed；没有就静默返回。"""
    feed = getattr(host, "voice_signal_feed", None)
    if feed is None:
        return
    if packet.transport.upper() != "UDP" or packet.inferred_direction() != "server_to_client":
        return
    rows = decode_v576_self_state_applies(
        packet.payload, "server_to_client", {SWAP_SKILL}
    )
    if not rows:
        return
    if not _is_swap_nest(host):
        return

    origin = getattr(host, "first_timestamp", 0) or packet.timestamp_seconds
    stamp = max(0, round((packet.timestamp_seconds - origin) * 1000))
    recipients = [{"id": "0x%08X" % owner} for owner, _skill in rows]
    feed.publish(
        {
            "skill": SWAP_SKILL,
            "source": SWAP_SOURCE,
            "timestamp": stamp,
            "templateId": 0,
            "evidenceKind": SWAP_KIND,
            "kind": SWAP_KIND,
            "recipients": recipients,
        }
    )
