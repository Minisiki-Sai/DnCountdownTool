"""Reviewed geometry-sensitive candidates; ordinary missed hits never lower theory."""
# The same explicit user-approved scope governs theory and realization.
THEORY_BODY_SKILLS = frozenset({
    '冰刺', '冰尖柱', '黑暗神殿', '箭雨风暴', '致命劈砍', '追风箭',
    '黑暗信念', '连锁闪电', '圣光十字', '黑暗冲浪', '集中射击', '迅捷箭',
    '激光破碎', '风车公主', '新月之刃', '审判锤', '瞒天过海', '雷暴', '野兽之灵', '冰柱喷射',
})

def theory_body_sensitive(job, skill):
    from .scoring import canonical_skill
    return canonical_skill(skill) in THEORY_BODY_SKILLS

V1_SUMMONS = {'雷电之手','偷袭点','炮塔','链锯之塔','寒冰喷射塔'}
def body_sensitive(job,skill):return theory_body_sensitive(job,skill)
def v1_summon(job,skill):
 return skill in {'雷电之手','偷袭点','灵魂之门','暗影召唤术','瞒天过海'} or (job in {'重炮手','机械大师'} and ('塔' in skill or skill in V1_SUMMONS))
