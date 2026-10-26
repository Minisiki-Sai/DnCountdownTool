# 数据文件说明

程序会优先读取本目录的 `manifest.json`，再按里面的路径加载各个小 JSON。
这里的 JSON 已经是紧凑结构，不再是原始 Excel 的一行一属性格式。

## 常用编辑位置

- 装备基础属性：`tables/equipment/base/equipment_base_职业.json`
- 装备强化：`tables/equipment/enhance/equipment_enhance_职业.json`
- 装备后缀：`tables/equipment/suffix/equipment_suffix_职业.json`
- 附魔：`tables/equipment_enchant.json`
- 饰品：`tables/jewelry/jewelry_部位.json`
- 套装属性：`tables/set_bonus.json`
- A 纹章：`tables/glyph_a_base.json`、`tables/glyph_a_extra.json`
- S 纹章：`tables/glyph_s_base.json`、`tables/glyph_s_extra.json`
- 普通石板：`tables/normal_rune.json`
- 源铸石板：`tables/core_rune.json`
- 卡片：`tables/card_data.json`、`tables/card_lookup.json`
- 收集等级：`tables/collection.json`
- 称号：`tables/title.json`
- 时装：`tables/fashion.json`
- 技能 buff：`tables/skill_buff.json`
- 职业基础属性：`tables/character_base.json`
- 职业映射：`tables/job_map.json`
- 属性转换：`tables/attribute_conversion.json`
- 等效面板权重：`tables/effective_panel.json`
- 下拉框列表：`lists/*.json`
- 默认配装：`defaults.json`
- 右侧属性显示顺序：`attribute_order.json`

## 例子

装备基础属性现在是：

```json
{
  "战士": {
    "70S": {
      "地狱犬头盔": {
        "部位": "头盔",
        "套装ID": 1000,
        "属性": {"防御": 123, "魔防": 456}
      }
    }
  }
}
```

源铸石板现在是：

```json
{
  "60": {
    "物攻→魔攻": {
      "扣除属性": "物攻",
      "获得属性": "魔攻",
      "转换比例": {"1": 5},
      "转换系数": {"1": 0.25},
      "最大扣除值": {"1": 20000}
    }
  }
}
```

## 更新流程

如果从 Excel 重新提取数据，运行：

```powershell
python tools\extract_workbook_data.py
```

这个命令会先刷新上一层的 `dragon_nest_data.json`，再自动刷新本目录的小文件。

`dragon_nest_data.json` 是重新提取时的临时中间文件，程序正常运行和发布均不需要保留它。

网页技能原始数据重新抓取并按职业拆分后，再运行：

```powershell
python tools\compact_full_skill_runtime.py --install
```

该命令只保留模拟器会读取的 PVE 技能树、等级、SP、CD、描述和元素字段，并删除原始总表及临时拆分数据。完整原始数据仍可从 v3.40 冻结版本恢复。
