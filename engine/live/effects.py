from __future__ import annotations

from dataclasses import dataclass
import math
import re


@dataclass(frozen=True, slots=True)
class BossEffectRule:
    key: str
    value: float | None
    duration_seconds: float
    stackable: bool = False
    stack_group: str = ""
    proc_chance: float = 1.0


def _rule(
    key: str,
    value: float | None,
    duration: float,
    *,
    stackable: bool = False,
    group: str = "",
    proc_chance: float = 1.0,
) -> BossEffectRule:
    return BossEffectRule(
        key,
        value,
        duration,
        stackable,
        group,
        max(0.0, min(1.0, float(proc_chance))),
    )


def _skill_key(value: str) -> str:
    return "".join(str(value or "").strip().upper().split())


# This catalog only translates a server status record's owning skill into a
# readable effect category. It never creates or times a status: activation,
# refresh and duration all come from the server packet itself.
_RULES_BY_SKILL: dict[str, tuple[BossEffectRule, ...]] = {
    _skill_key("霜冻"): (_rule("减水抗%", 30, 12),),
    _skill_key("漫长熔浆"): (
        _rule("减火抗%", 30, 15, group="client:36"),
    ),
    _skill_key("天堂审判"): (_rule("减光抗%", 30, 15),),
    _skill_key("神圣大地"): (
        _rule("减光抗%", 10, 11, stackable=True, group="神圣大地"),
    ),
    _skill_key("冰雪连天"): (_rule("减水抗%", 30, 8),),
    _skill_key("爱情病毒"): (_rule("减暗抗%", 30, 10),),
    _skill_key("毒池"): (_rule("减暗抗%", 30, 11),),
    _skill_key("C2H5OH"): (
        _rule("减全抗%", 12, 15, stackable=True, group="C2H5OH"),
    ),
    _skill_key("混沌阵"): (_rule("减暗抗%", 30, 12),),
    _skill_key("上升重力"): (_rule("减暗抗%", 15, 15),),
    _skill_key("灵魂之门EX"): (_rule("减暗抗%", 30, 14),),
    _skill_key("冰刃"): (_rule("减水抗%", 15, 4),),
    _skill_key("冰雾"): (_rule("减水抗%", 15, 8),),
    _skill_key("冰刺"): (_rule("减水抗%", 15, 6),),
    _skill_key("弗罗斯特风"): (_rule("减水抗%", 15, 5),),
    _skill_key("地狱火海EX"): (
        _rule("减火抗%", 15, 11, group="client:36"),
    ),
    _skill_key("岩浆墙"): (
        _rule("减火抗%", 15, 10, group="client:36"),
    ),
    _skill_key("毒气聚能"): (_rule("减暗抗%", 15, 11),),
    _skill_key("疾病"): (_rule("减暗抗%", 15, 15),),
    _skill_key("混合感染"): (_rule("减暗抗%", 15, 11),),
    _skill_key("狂暴之爪"): (_rule("减暗抗%", 15, 10),),
    _skill_key("复仇之手EX"): (_rule("减暗抗%", 15, 5),),
    _skill_key("连锁闪电"): (_rule("减光抗%", 15, 10),),
    _skill_key("雷电冲击"): (_rule("减光抗%", 15, 5),),
    _skill_key("重击"): (_rule("减光抗%", 15, 10),),
    _skill_key("电光箭"): (_rule("减光抗%", 15, 10),),
    _skill_key("精神爆破EX"): (_rule("减光抗%", 30, 7),),
    _skill_key("闪电风暴"): (_rule("减光抗%", 15, 12),),
    _skill_key("雷电之手"): (_rule("减光抗%", 15, 6),),
    _skill_key("雷电之手EX"): (_rule("减光抗%", 15, 6),),
    _skill_key("天降雨点"): (_rule("减暗抗%", 15, 15),),
    _skill_key("灵巧猎手"): (
        _rule("减火抗%", 15, 20, group="client:36"),
    ),
    _skill_key("黑暗线条"): (_rule("减暗抗%", 15, 10),),
    _skill_key("灵魂之刃"): (
        _rule("减火抗%", 15, 30, group="client:36"),
    ),
    # Silver Hunter debuffs are separate server states. Hurricane Rapid Fire
    # is packet-timed, while Holy Spiral Hurricane applies a confirmed 10 s
    # amplification state (wire skill 1628, effect 134700, parameter 1.2).
    _skill_key("飓风狂射"): (_rule("减光抗%", 30, 0, group="飓风狂射"),),
    _skill_key("螺旋飓风"): (_rule("增伤%", 20, 10, group="螺旋飓风"),),
    _skill_key("神圣螺旋飓风"): (_rule("增伤%", 20, 10, group="螺旋飓风"),),
    _skill_key("银色箭雨"): (
        # Each hit has a 50% trigger chance, but one Silver Arrow Rain cast
        # contains enough independent hits that the combat/simulation contract
        # treats the application as stable per cast.  Keep the raw 50% wording
        # in skill_buff.json; this field is the cast-level application chance.
        _rule("减光抗%", 15, 6, group="银色箭雨", proc_chance=1.0),
    ),
    _skill_key("重锤"): (_rule("增伤%", 40, 15, group="重锤"),),
    _skill_key("瞄准"): (_rule("增伤%", 20, 60, group="瞄准"),),
    _skill_key("能量反射"): (_rule("魔法增伤%", 30, 24, group="能量反射"),),
    _skill_key("破甲"): (_rule("增伤%", 20, 15, group="破甲"),),
    _skill_key("召唤傀儡"): (_rule("增伤%", 20, 20, group="召唤傀儡"),),
    _skill_key("冲击之手"): (_rule("增伤%", 20, 20, group="冲击之手"),),
    _skill_key("掌握之手EX"): (_rule("增伤%", 10, 15, group="掌握之手EX"),),
    _skill_key("捆绑射击EX"): (
        _rule("物理增伤%", 30, 9, group="游侠物理增伤"),
    ),
    _skill_key("爆裂踢EX"): (
        _rule("物理增伤%", 30, 9, group="游侠物理增伤"),
    ),
    _skill_key("冰刃EX"): (_rule("增伤%", 20, 7, group="冰刃EX"),),
    _skill_key("黄昏之光"): (_rule("增伤%", 25, 10, group="黄昏之光"),),
    _skill_key("重磅鸭"): (_rule("增伤%", 12, 20, group="重磅鸭"),),
    _skill_key("飞行冲击EX"): (_rule("增伤%", 20, 15, group="飞行冲击EX"),),
    _skill_key("疾病EX"): (_rule("增伤%", 20, 10, group="疾病EX"),),
    _skill_key("日光焰"): (_rule("增伤%", 20, 10, group="日光焰"),),
    _skill_key("火焰冲刺"): (_rule("增伤%", 20, 15, group="火焰冲刺"),),
    _skill_key("冰尖柱"): (
        _rule("冰冻层", 1, 4, stackable=True, group="冰尖柱"),
    ),
    _skill_key("冰河"): (
        # Default Lv7 is six seconds; selected-level theory resolves the exact
        # 3-8 second value from the skill description.
        _rule("冰冻层", 1, 6, stackable=True, group="冰河"),
    ),
    _skill_key("暴风雪"): (
        _rule("冰冻层", 1, 11, stackable=True, group="暴风雪"),
    ),
    _skill_key("冰柱喷射"): (
        _rule("冰冻层", 1, 11, stackable=True, group="冰柱喷射"),
    ),
    _skill_key("霜冻EX"): (
        _rule("冰冻层", 1, 4, stackable=True, group="霜冻EX"),
    ),
    _skill_key("冰光束"): (
        # 冰光束的冻结时间随技能等级增长；默认加点为 Lv7（6 秒）。
        # 实战统计优先采用状态包携带的实际持续时间。
        _rule("冰冻层", 1, 6, stackable=True, group="炼金冰冻"),
    ),
    _skill_key("寒冰戳刺EX"): (
        _rule("冰冻层", 1, 3, stackable=True, group="炼金冰冻"),
    ),
    _skill_key("切骨之刺EX"): (
        _rule("减致命抵抗%", 20, 10, group="client:162"),
    ),
    _skill_key("旋风斩EX"): (
        _rule("减致命抵抗%", 20, 8, group="client:162"),
    ),
    _skill_key("火墙EX"): (
        _rule("减致命抵抗%", 30, 15, group="火墙EX"),
    ),
    _skill_key("重磅轰击"): (
        _rule("减致命抵抗%", 20, 12, group="重磅轰击"),
    ),
    _skill_key("灵魂尖叫"): (
        _rule("减致命抵抗%", 20, 15, group="灵魂尖叫"),
    ),
    _skill_key("致命狂镰"): (
        _rule("减防御%", 10, 8, group="client:skill-duplicate:6202"),
    ),
    _skill_key("惩罚"): (
        _rule("减防御%", 10, 12, group="client:skill-duplicate:6202"),
    ),
    _skill_key("致命狂镰EX"): (
        _rule("减防御%", 20, 8, group="client:skill-duplicate:6202"),
    ),
    _skill_key("惩罚EX"): (
        _rule("减防御%", 20, 12, group="client:skill-duplicate:6202"),
    ),
    _skill_key("铁壁后缀"): (
        _rule("减物理防御%", 30, 10, group="client:state-effect:20013"),
    ),
    _skill_key("帷幕后缀"): (
        _rule("减魔法防御%", 30, 10, group="client:state-effect:20014"),
    ),
    _skill_key("疾风后缀"): (
        # 后缀等级决定实际数值；实时包会携带 -0.1325~-0.144，
        # 因此保留动态值；所有武器和玩家共用目标的 StateEffect 20003。
        _rule(
            "减致命抵抗%",
            None,
            10,
            group="client:state-effect:20003",
        ),
    ),
    _skill_key("毁灭呐喊"): (
        _rule("减攻击%", None, 0, group="减攻击"),
    ),
    _skill_key("反击怒吼"): (
        _rule("减攻击%", None, 0, group="减攻击"),
    ),
    _skill_key("正义撞击"): (
        _rule("减攻击%", None, 0, group="减攻击"),
    ),
    _skill_key("神圣断言"): (
        _rule("减攻击%", 15, 7, group="减攻击"),
    ),
}


_EX_ALIASES: dict[str, tuple[str, frozenset[str]]] = {
    _skill_key("灵魂之门"): (_skill_key("灵魂之门EX"), frozenset({"噬魂者"})),
    _skill_key("地狱火海"): (_skill_key("地狱火海EX"), frozenset({"火舞"})),
    _skill_key("复仇之手"): (_skill_key("复仇之手EX"), frozenset({"黑暗萨满"})),
    _skill_key("精神爆破"): (_skill_key("精神爆破EX"), frozenset({"雷神"})),
    _skill_key("霜冻"): (_skill_key("霜冻EX"), frozenset({"冰灵"})),
    _skill_key("捆绑射击"): (_skill_key("捆绑射击EX"), frozenset({"影舞者"})),
    _skill_key("爆裂踢"): (_skill_key("爆裂踢EX"), frozenset({"风行者"})),
    _skill_key("切骨之刺"): (_skill_key("切骨之刺EX"), frozenset({"剑皇"})),
    _skill_key("旋风斩"): (_skill_key("旋风斩EX"), frozenset({"月之领主"})),
    _skill_key("火墙"): (_skill_key("火墙EX"), frozenset({"火舞"})),
    _skill_key("飞行冲击"): (_skill_key("飞行冲击EX"), frozenset({"毁灭者"})),
    _skill_key("寒冰戳刺"): (
        _skill_key("寒冰戳刺EX"),
        frozenset({"炼金圣士"}),
    ),
}


# A skill may apply more than one server status. These entries identify the
# secondary state which must not inherit the skill's main resistance/amp rule.
_STATUS_RULES_BY_SKILL_AND_CODE: dict[
    tuple[str, str], tuple[BossEffectRule, str]
] = {
    (
        _skill_key("闪电风暴"),
        "135b00",
    ): (
        _rule("触电", None, 2.5, group="闪电风暴触电"),
        "每1秒触发，最多3次",
    ),
    (_skill_key("科兹污点"), "134400"): (
        _rule("烙印状态", None, 20, group="烙印"),
        "科兹污点施加的烙印",
    ),
    (_skill_key("灵魂之纸"), "135900"): (
        _rule("诅咒状态", None, 12, group="诅咒"),
        "诅咒攻击触发状态",
    ),
    (_skill_key("灵魂之纸"), "135000"): (
        _rule("诅咒触发", None, 12, group="诅咒"),
        "诅咒伤害的内部触发状态",
    ),
    (_skill_key("野兽之灵"), "135900"): (
        _rule("诅咒状态", None, 12, group="诅咒"),
        "诅咒攻击触发状态",
    ),
    (_skill_key("灵魂之门"), "135600"): (
        _rule("诅咒状态", None, 10, group="诅咒"),
        "灵魂之门幽灵施加的诅咒",
    ),
    (_skill_key("苦痛之灵"), "136000"): (
        _rule("幽灵诅咒", None, 15, group="幽灵诅咒"),
        "重叠3次后会爆炸",
    ),
    (_skill_key("怨恨之像"), "134400"): (
        _rule("超级盔甲破坏力降低%", 100, 1.5, group="怨恨之像"),
        "结界范围内持续刷新",
    ),
    (_skill_key("捆绑射击"), "135900"): (
        _rule("束缚状态", None, 7, group="捆绑射击束缚"),
        "拖拽/束缚的内部状态",
    ),
    (_skill_key("惩戒波"), "134900"): (
        _rule("挑衅", None, 0, group="挑衅"),
        "与挑衅相同的仇恨状态",
    ),
    (_skill_key("惩戒波EX"), "134900"): (
        _rule("挑衅", None, 0, group="挑衅"),
        "与挑衅相同的仇恨状态",
    ),
    (_skill_key("飞行冲击"), "135800"): (
        _rule("增伤%", 20, 15, group="飞行冲击EX"),
        "飞行冲击EX增伤",
    ),
    (_skill_key("飞行冲击EX"), "135800"): (
        _rule("增伤%", 20, 15, group="飞行冲击EX"),
        "飞行冲击EX增伤",
    ),
    (_skill_key("旋风斩"), "134800"): (
        _rule("减致命抵抗%", 20, 7, group="client:162"),
        "旋风斩EX减致命抵抗",
    ),
    (_skill_key("旋风斩EX"), "134800"): (
        _rule("减致命抵抗%", 20, 7, group="client:162"),
        "旋风斩EX减致命抵抗",
    ),
    (_skill_key("爆裂踢"), "135700"): (
        # The observed v576 carrier packet is seven seconds. The current
        # add-point definition is nine seconds; packet timing remains factual
        # for replay/live statistics while the generic rule stays current.
        _rule("物理增伤%", 30, 7, group="游侠物理增伤"),
        "爆裂踢EX物理增伤",
    ),
    (_skill_key("爆裂踢EX"), "135700"): (
        _rule("物理增伤%", 30, 9, group="游侠物理增伤"),
        "爆裂踢EX物理增伤",
    ),
    (_skill_key("灵魂之门"), "134800"): (
        _rule("减暗抗%", 30, 14, group="灵魂之门EX"),
        "灵魂之门EX减暗抗",
    ),
    (_skill_key("灵魂之门EX"), "134800"): (
        _rule("减暗抗%", 30, 14, group="灵魂之门EX"),
        "灵魂之门EX减暗抗",
    ),
    (_skill_key("地狱火海"), "134b00"): (
        _rule("减火抗%", 15, 11, group="client:36"),
        "地狱火海EX减火抗",
    ),
    (_skill_key("地狱火海EX"), "134b00"): (
        _rule("减火抗%", 15, 11, group="client:36"),
        "地狱火海EX减火抗",
    ),
}


def resistance_application_mode(skill_name: str, effect_key: str) -> str:
    """Whether a resistance state is applied by the cast or by later hits."""
    if (
        _skill_key(skill_name).removesuffix("EX") == _skill_key("灵魂之门")
        and effect_key in {"", "减暗抗%"}
    ):
        # The EX door emits 134800 at release. Its later ghost hits carry
        # curse/internal statuses, not another 30% resistance application.
        return "cast"
    return "hit"


# These packet states intentionally carry a control state and a gameplay
# modifier together. Their packet duration may differ from the current
# add-point fallback after a balance patch, so the secondary effect must not
# disappear merely because duration-based compatibility no longer matches.
_MULTIPLEXED_STATUS_COEFFECTS: dict[tuple[str, str], frozenset[str]] = {
    (_skill_key("捆绑射击"), "束缚状态"): frozenset({"物理增伤%"}),
}


def boss_effect_rules(skill_name: str, job: str = "") -> tuple[BossEffectRule, ...]:
    key = _skill_key(skill_name)
    job_name = str(job or "").strip()
    rules = list(_RULES_BY_SKILL.get(key, ()))
    if key.endswith("EX"):
        # EX skills retain the base skill's debuff unless a packet-specific
        # rule proves otherwise. Status catalogs often report the EX name
        # directly while this table stores the shared base effect.
        direct_rule_keys = {rule.key for rule in rules}
        inherited_rules = [
            rule
            for rule in _RULES_BY_SKILL.get(key[:-2], ())
            if rule.key not in direct_rule_keys
        ]
        rules = inherited_rules + rules
    if not rules and key.endswith("状态"):
        rules = list(_RULES_BY_SKILL.get(key[:-2], ()))
    # 刺客页和贤者页都有“重击”，但只有贤者系版本附带减光抗。
    # 职业已知时必须在这里隔离同名技能，避免实时状态和评分同时串效。
    if (
        key in {_skill_key("重击"), _skill_key("重击EX")}
        and job_name
        and job_name not in {"圣骑士", "十字军"}
    ):
        rules = []
    # 寒冰戳刺的叠冰来自炼金圣士的EX，药剂师的同名基础技能
    # 没有冰冻层。职业已知时也隔离误归名为EX的实时状态记录。
    if (
        key in {_skill_key("寒冰戳刺"), _skill_key("寒冰戳刺EX")}
        and job_name
        and job_name != "炼金圣士"
    ):
        rules = []
    if key == _skill_key("致命狂镰") and job_name == "影":
        rules = list(_RULES_BY_SKILL[_skill_key("致命狂镰EX")])
    if key == _skill_key("惩罚") and job_name == "影":
        rules = list(_RULES_BY_SKILL[_skill_key("惩罚EX")])
    alias = _EX_ALIASES.get(key)
    if alias is not None and job_name in alias[1]:
        rules.extend(_RULES_BY_SKILL.get(alias[0], ()))
    if key == _skill_key("冰刃") and job_name == "冰灵":
        rules.extend(_RULES_BY_SKILL[_skill_key("冰刃EX")])
    if key == _skill_key("疾病") and job_name == "药剂师":
        rules.extend(_RULES_BY_SKILL[_skill_key("疾病EX")])
    if key == _skill_key("掌握之手") and job_name == "圣徒":
        rules.extend(_RULES_BY_SKILL[_skill_key("掌握之手EX")])
    return tuple(rules)


def boss_effect_rules_for_status_packet(
    skill_name: str,
    job: str,
    *,
    packet_value: float | None,
    duration_seconds: float,
    observed_effect_key: str = "",
    catalog_job: str = "",
    effect_code: str = "",
) -> tuple[BossEffectRule, ...]:
    """Return semantic effects compatible with one observed status packet.

    A single server status can describe both a control/internal state and a
    gameplay debuff. Compatibility is determined from the packet rather than
    a skill-name exception, so future multi-effect skills use the same path.
    """
    rules = boss_effect_rules(skill_name, job)
    if not rules and catalog_job and catalog_job != job:
        # Early packets can know only the actor's first job. The skill ID's
        # catalog job is more specific, so use it to retain the first
        # application of job-gated effects such as Heavy Smash.
        rules = boss_effect_rules(skill_name, catalog_job)
    # Once a skill/effect has a packet-confirmed status code, its other
    # internal states must not inherit the same gameplay modifier merely from
    # the skill name or a coincidentally matching duration/value.
    skill_key = _skill_key(skill_name)
    normalized_code = str(effect_code or "").strip().lower()
    if normalized_code:
        locked_keys = {
            rule.key
            for (locked_skill, locked_code), (rule, _label)
            in _STATUS_RULES_BY_SKILL_AND_CODE.items()
            if locked_skill == skill_key and locked_code != normalized_code
        }
        rules = tuple(rule for rule in rules if rule.key not in locked_keys)
    if not observed_effect_key:
        return tuple(
            rule
            for rule in rules
            if rule.key != "减攻击%" or packet_value is not None
        )

    # This is a packet-visible Ice Spike implementation state, not another
    # gameplay resistance modifier. It shares the wire status code and a
    # similar duration with the real EffectClass 144 Debuff, so duration-based
    # compatibility must not re-attach the 15% rule.
    if observed_effect_key == "冰刺内部状态":
        return ()

    compatible: list[BossEffectRule] = []
    multiplexed_keys = _MULTIPLEXED_STATUS_COEFFECTS.get(
        (_skill_key(skill_name), observed_effect_key), frozenset()
    )
    for rule in rules:
        if rule.key == "减攻击%" and packet_value is None:
            continue
        if rule.key == observed_effect_key:
            continue
        value_matches = (
            packet_value is not None
            and rule.value is not None
            and abs(rule.value - packet_value) <= 1e-6
        )
        duration_matches = (
            duration_seconds > 0
            and abs(rule.duration_seconds - duration_seconds) <= 0.25
        )
        if value_matches or duration_matches or rule.key in multiplexed_keys:
            compatible.append(rule)
    return tuple(compatible)


def packet_effect_value_for_rule(
    rule: BossEffectRule,
    parameters: str,
    packet_value: float | None,
) -> float | None:
    """Resolve a trustworthy gameplay percentage from one status packet.

    Wire parameters use several encodings: ``-0.15`` is a direct 15% loss,
    ``0.70`` can mean a remaining 70% multiplier (therefore a 30% loss), and
    ``1.20`` can mean a 20% amplification. A status can also carry unrelated
    coefficients such as Lightning Relic's ``0.96``. Prefer the packet only
    when its encoding is compatible with the semantic rule; otherwise return
    ``None`` so the reviewed add-point value remains the safe fallback.
    """

    key = str(rule.key or "").strip()
    if not key.endswith("%") or key == "冰冻层":
        return None

    values: list[float] = []
    for token in re.split(r"[;,]", str(parameters or "")):
        try:
            value = float(token.strip())
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    if not values:
        return None

    # A signed fractional loss is unambiguous even when the packet also
    # carries damage coefficients or trigger parameters.
    negative = [abs(value) * 100.0 for value in values if -1.0 <= value < 0.0]
    if negative:
        resolved = round(negative[-1], 6)
        return resolved if 0.0 < resolved <= 100.0 else None

    # Multiple positive terms are not safe to interpret generically. They
    # commonly describe formulas rather than the displayed Buff/Debuff value.
    if len(values) != 1:
        return None
    raw = values[0]
    candidates: list[float] = []
    reduction = key.startswith("减") or "降低" in key
    amplification = "增伤" in key or "增加" in key or "提升" in key
    if reduction and 0.0 < raw < 1.0:
        # Both encodings exist. The reviewed value disambiguates the lane, but
        # the returned number is still the packet-derived value.
        candidates.extend((raw * 100.0, (1.0 - raw) * 100.0))
    elif amplification and 1.0 < raw <= 2.0:
        candidates.append((raw - 1.0) * 100.0)
    elif amplification and 0.0 < raw < 1.0:
        candidates.append(raw * 100.0)
    elif packet_value is not None:
        candidates.append(float(packet_value))
    candidates = [round(value, 6) for value in candidates if 0.0 < value <= 100.0]
    if not candidates:
        return None

    if rule.value is None:
        return candidates[0]
    expected = float(rule.value)
    resolved = min(candidates, key=lambda value: abs(value - expected))
    # Reject an unrelated coefficient. Moderate balance changes still pass;
    # grossly different values remain on the reviewed fallback until the
    # status encoding has been confirmed.
    tolerance = max(2.0, min(10.0, abs(expected) * 0.5))
    return resolved if abs(resolved - expected) <= tolerance + 1e-9 else None


def boss_effect_duration_ms(
    rule: BossEffectRule,
    packet_duration_ms: int,
) -> int:
    """Resolve one semantic effect's duration from a multiplexed status.

    Freeze packets can carry another state from the same skill, so their raw
    duration is not always the lifetime of the freeze layer itself.
    """
    if rule.key == "冰冻层":
        # 冻结持续时间会随技能等级变化。服务器状态包已经给出本次
        # 实际值时，以包为事实来源；规则时长只作为理论轴/缺包回退。
        if packet_duration_ms > 0:
            return int(packet_duration_ms)
        return max(1, round(rule.duration_seconds * 1000))
    if packet_duration_ms > 0:
        return int(packet_duration_ms)
    return max(1, round(rule.duration_seconds * 1000))


def boss_status_override(
    skill_name: str,
    effect_code: str,
    *,
    parameters: str = "",
    duration_seconds: float = 0.0,
    effect_class_id: int = 0,
) -> tuple[BossEffectRule, str] | None:
    skill_key = _skill_key(skill_name)
    normalized_code = str(effect_code or "").strip().lower()
    if (
        skill_key in {_skill_key("冰刺"), _skill_key("冰刺EX")}
        and normalized_code == "135500"
    ):
        if int(effect_class_id or 0) == 144:
            return (
                _rule("减水抗%", 15, duration_seconds or 6, group="冰刺"),
                "冰刺15%减水抗",
            )
        if int(effect_class_id or 0) == 652:
            return (
                _rule(
                    "冰刺内部状态",
                    None,
                    duration_seconds or 6,
                    group="冰刺内部状态",
                ),
                "冰刺内部触发状态（不参与减抗）",
            )
    exact = _STATUS_RULES_BY_SKILL_AND_CODE.get(
        (skill_key, normalized_code)
    )
    if exact is not None:
        return exact

    # Lightning Storm's internal shock status uses more than one wire code
    # (135b/135c/135f have all appeared in real captures).  Its stable
    # signature is a roughly 2.5-second state with
    # ``interval_ms;max_triggers;1`` parameters.  Recognise the signature
    # instead of allowing those packets to inherit the skill's 15% light
    # resistance reduction.
    if skill_key != _skill_key("闪电风暴"):
        return None
    duration = float(duration_seconds or 0.0)
    if not 2.0 <= duration <= 3.0:
        return None
    fields = [part.strip() for part in str(parameters or "").split(";")]
    if len(fields) != 3:
        return None
    try:
        interval_ms, max_triggers, state_flag = (float(part) for part in fields)
    except (TypeError, ValueError):
        return None
    if (
        not 900.0 <= interval_ms <= 1100.0
        or max_triggers <= 0
        or abs(max_triggers - round(max_triggers)) > 1e-6
        or abs(state_flag - 1.0) > 1e-6
    ):
        return None
    interval_seconds = interval_ms / 1000.0
    interval_text = f"{interval_seconds:g}"
    trigger_count = int(round(max_triggers))
    return (
        _rule(
            "触电",
            None,
            duration,
            group="闪电风暴触电",
        ),
        f"每{interval_text}秒触发，最多{trigger_count}次",
    )


def boss_effect_label(key: str) -> str:
    extracted_labels = {
        "减硬直/眩晕抵抗%": "减硬直/眩晕抵抗",
        "减致命抵抗%": "减致命抗",
        "减全抗%": "减全抗",
        "减移动速度%": "减移动速度",
        "减物理防御%": "减物理防御",
        "减魔法防御%": "减魔法防御",
        "减攻击%": "减少攻击",
        "禁足": "禁足",
        "破坏超级盔甲": "破坏超级盔甲",
        "烙印状态": "烙印状态",
        "诅咒状态": "诅咒状态",
        "诅咒触发": "诅咒触发",
        "幽灵诅咒": "幽灵诅咒",
        "超级盔甲破坏力降低%": "超级盔甲破坏力降低",
        "束缚状态": "束缚状态",
        "挑衅": "挑衅",
    }
    if key in extracted_labels:
        return extracted_labels[key]
    return {
        "减火抗%": "减火抗",
        "减水抗%": "减水抗",
        "减光抗%": "减光抗",
        "减暗抗%": "减暗抗",
        "减全抗%": "减全抗",
        "增伤%": "通用增伤",
        "物理增伤%": "物理增伤",
        "魔法增伤%": "魔法增伤",
        "减防御%": "减防御",
        "减物理防御%": "减物理防御",
        "减魔法防御%": "减魔法防御",
        "减攻击%": "减少攻击",
        "减致命抵抗%": "减致命抗",
        "冰冻层": "叠冰状态",
        "触电": "触电",
    }.get(key, key.rstrip("%"))
