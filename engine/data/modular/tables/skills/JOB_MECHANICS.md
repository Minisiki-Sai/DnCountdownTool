# 职业特殊机制数据

`job_mechanics.json` 是职业特殊机制的运行数据源。程序启动时由
`engine/job_mechanics.py` 校验并编译，旧模块仍可通过原有常量名称读取。

## 常用字段

- `auto_selection.skip`：一键模拟不自动选择的技能。
- `auto_selection.force_include`：一键模拟必须纳入的技能。
- `auto_selection.forced_last`：强制放在自动技能列表最后的技能。
- `auto_selection.duplicate_limit`：允许重复选择的技能组及总数量。
- `opening`：实战轴起手优先安排的状态或技能。
- `combos.followups`：技能之后可以衔接的技能。
- `combos.priority`：存在多个衔接技能时的优先顺序。
- `combos.required_predecessors`：释放技能必须满足的前置技能。
- `combos.required_followups`：释放后必须紧接的后续技能。
- `combos.quick_predecessors`：只有指定前置紧邻时才使用快速施法时间；不满足时仍可按完整动作时间正常施放。若技能本身禁止独立施放，必须另写入 `combos.required_predecessors`。
- `resources`：弹药、怒气、护卫者幻影、烙印等资源规则。
- `summons`：按职业覆盖的召唤数量和持续时间。
- `stance_skills`：动作速度影响攻击次数的姿态技能。
- `global.action_speed.fixed_duration_output_skills`：施法时间固定，动作速度按同比例增加输出的持续动作技能。
- `passive_cooldown_sources`：由被动触发的冷却规则。
- `special_cooldown_sources`：由其他技能循环决定的等效冷却。
- `team_effects`：该职业可以带入配队时间轴的团队效果技能。
- `runtime_handlers`：该职业需要调用的复杂执行器类型，只用于描述和校验。

## 修改原则

1. 修改数值、技能名称、前后置关系和数量限制时，只改 JSON。
2. 数组顺序有意义，例如 `opening` 和 `combos.priority` 会按顺序读取。
3. `set` 类型在 JSON 中写成数组，加载器会自动转换并去重。
4. 新增 `runtime_handlers` 时，必须先在 `global.handler_catalog` 声明。
5. 修改后运行 `python -m unittest tests.test_job_mechanics -v` 检查结构和兼容接口。

## 示例

下面的规则表示直线冲击必须紧接在重锤之后：

```json
"剑皇": {
  "runtime_handlers": ["strict_combo"],
  "combos": {
    "followups": {"重锤": ["直线冲击"]},
    "required_predecessors": {"直线冲击": ["重锤"]}
  }
}
```
