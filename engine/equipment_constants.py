"""Equipment catalogs shared by headless scoring, without importing Qt UI."""
AUTO_EQUIP_TARGETS = {
    "50": (("霸龙L装备", "equipment", "50L"), ("霸龙L首饰", "jewelry", "50L")),
    "60": (("赞龙L装备", "equipment", "60L"), ("赞龙L首饰", "jewelry", "60L"), ("赞龙S首饰", "jewelry", "60S")),
    "70": (
        ("黑龙L装备+首饰", "all", "70L"),
        ("黑龙L装备", "equipment", "70L"),
        ("黑龙L首饰", "jewelry", "70L"),
        ("黑龙S装备", "equipment", "70S"),
        ("黑龙S首饰", "jewelry", "70S"),
    ),
}
ENGRAVING_SLOTS = (
    "头盔", "上装", "下装", "手套", "鞋子", "主手", "副手",
    "项链", "耳环", "戒指1", "戒指2", "翅膀", "尾巴", "印花",
)
CARD_PREVIEW_ATTRS = ("HP", "MP", "体质", "力量", "敏捷", "智力", "最终", "物攻", "致命", "致命伤害", "防御", "魔攻", "魔防")
