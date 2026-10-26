"""Reviewed common skill-plate result keys from the installed client tables."""
from types import MappingProxyType

# SkillTable RowId -> UIStr NameID: 80005 -> 1000016890;
# 80020 -> 1000032862. These are damaging skill plates, outside job trees.
# All admitted IDs have NeedJob=0, a localized client NameID, and a
# SkillLevel processor whose player ACT contains a typed damage signal (4)
# or a typed Projectile spawn (15). Recovery/revival/shield/control-only
# common skills are excluded. Evidence: common_plate_graph.json from the
# unpacked 0811 resource; joins use exact processor/action names.
SKILL_PLATE_DAMAGE_NAMES = MappingProxyType({
    80001: '地狱犬纹章-暗黑闪电',
    80002: '地狱犬纹章 - 冰球',
    80003: '地狱犬纹章 - 火球',
    80004: '狮蝎纹章 - 黑洞',
    80005: '狮蝎纹章 - 重力球',
    80006: '狮蝎纹章 - 漩涡',
    80007: '牛头怪纹章 - 母牛袭击',
    80008: '黑暗神殿纹章 - 猎犬袭击',
    80010: '天启纹章 - 消失',
    80013: '杰兰特纹章-瓦解',
    80014: '杰兰特纹章-龙之变奏曲',
    80015: '海龙纹章-龙的气息',
    80017: '草原石人纹章-毒性孢子',
    80018: '牛头怪纹章 - 母牛袭击',
    80020: '胡知诺斯之纹章-黑洞激光',
    80021: '胡知诺斯之纹章-岩石之手',
    80023: '卡拉翰之纹章-魔法导弹',
    80027: '绿龙纹章-毒性气息',
    80028: '绿龙纹章-灼热飓风',
    80029: '绿龙纹章-冰墙',
    80042: '地狱犬纹章 - 暗黑天雷',
    80043: '狮蝎纹章 - 荆棘',
    80044: '海龙纹章 - 霜冻之星',
    80046: '巨蜥纹章-大地震颤',
})
# Full player ACT reverse join gives one owning skill for these concrete
# configurations. 415 is reached only by typed spawn edges from 403.
SKILL_PLATE_PROJECTILE_SKILLS = MappingProxyType({
    381: 80002,
    382: 80003,
    401: 80006,
    402: 80004,
    403: 80005,
    410: 80008,
    411: 80008,
    412: 80008,
    413: 80008,
    415: 80005,
    416: 80017,
    417: 80020,
    418: 80023,
    419: 80027,
    420: 80028,
    421: 80029,
    422: 80044,
    423: 80043,
    424: 80046,
})
# A shared concrete configuration alone cannot distinguish these two
# client skill IDs. Exact native result keys can still identify either ID.
SKILL_PLATE_AMBIGUOUS_PROJECTILES = MappingProxyType({
    391: frozenset({80007, 80018}),
})

# cleric_subskill.act action 318 / signal 24 (type 15, config 403)
# is PlateSkill_Man_GravityBall_Start. This tuple independently proves the
# plate branch; Force User gravity balls use Projectile rows 3005/3018.
CLERIC_GRAVITY_PLATE_ROOT_KEYS = frozenset(
    (job, 318, 24, 403)
    for job in ("牧师", "祭司", "贤者", "圣徒", "雷神", "圣骑士", "十字军")
)
