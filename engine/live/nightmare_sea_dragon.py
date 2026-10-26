"""Reborn 2026-10-17 client-table and stage-trigger damage budgets.

Full entity HP and required stage depletion are deliberately separate.
Required depletion is user-confirmed: phase 1 11%, phase 2 none, phase 3 69%.
These percentages use each phase entity's own full HP.
No Nightmare Sea Dragon capture has been supplied for combat validation.
"""
NAME = "梦魇·海龙萨芬特拉"
HP_DIFFICULTY_MULTIPLIER = 100
PHASES = (
    {"phase":1,"template_ids":[4237003],"base_hp":71200000,"max_hp":7120000000,
     "start_ratio":1.,"end_ratio":.89,"required_damage":783200000,
     "evidence":"用户确认需输出本阶段满血11%；1Phase_hc.lua：89%进入Die"},
    {"phase":2,"template_ids":[4237034,4237035,4237011],"base_hp":71200000,"max_hp":7120000000,
     "start_ratio":.89,"end_ratio":.89,"required_damage":0,
     "evidence":"用户确认二阶段无需输出，需扣血预算为0；保留实体识别，不把脚本清除算作玩家扣血，起止比例仅表示零输出预算"},
    {"phase":3,"template_ids":[4237023],"base_hp":98200000,"max_hp":9820000000,
     "start_ratio":.69,"end_ratio":0.,"required_damage":6775800000,
     "evidence":"用户确认需输出本阶段满血69%；2 Bossdead节点：区域573出生后SetMonsterHpPer(0.69)"},
)
TEMPLATE_IDS = frozenset(n for phase in PHASES for n in phase["template_ids"])
TOTAL_FULL_HP = sum(phase["max_hp"] for phase in PHASES)
TOTAL_HP = sum(phase["required_damage"] for phase in PHASES)
PRESET = {"crit_resist":12000,"element_resist":20.0,"monster_defense":11000,"freeze_coefficient":0.3}
HP_NOTE = "按用户确认机制计算需扣血参考：一阶段需打自身满血11%，二阶段无需输出（不计血量预算），三阶段需打自身满血69%；基础HP已乘难度100。合计75.59亿；保留二阶段实体识别，不把脚本清除算作玩家伤害。尚待抓包核对血量基准、转场溢出和回血，非服务端当前血量实测。"

def matches(target_ids):
    return any(str(t).rsplit("@",1)[-1].isdigit() and int(str(t).rsplit("@",1)[-1]) in TEMPLATE_IDS for t in target_ids)
