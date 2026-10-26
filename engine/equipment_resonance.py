"""One resonance calculation shared by build preview and all damage engines."""
from collections import defaultdict
from functools import lru_cache
import json
import re
from .data_loader import MODULAR_DIR

DATA = MODULAR_DIR / 'tables'
SLOTS = ('头盔', '上装', '下装', '手套', '鞋子', '主手', '副手')

@lru_cache(maxsize=1)
def resonance_tiers():
    return json.loads((DATA/'equipment_resonance.json').read_text(encoding='utf-8'))['tiers']

@lru_cache(maxsize=1)
def neutral_passives():
    return json.loads((DATA/'skills/neutral_passives.json').read_text(encoding='utf-8'))['jobs']

def resonance_preview(store, build):
    rows = {str(item.get('slot','')): item for item in build.get('equipment',[]) if isinstance(item,dict)}
    # Slot labels come from the same data model; do not infer quality from names.
    index = getattr(store, '_resonance_equipment_tiers', None)
    if index is None:
        index = {}
        for row in store.tables.get('base',[]):
            match = re.fullmatch(r'(\d+)([ASL])', str(row.get('等级','')))
            if match:
                index[(str(row.get('职业','')),str(row.get('装备名称','')))] = (int(match[1]), {'A':3,'S':4,'L':5}[match[2]])
        store._resonance_equipment_tiers = index
    first, second = build.get('first_job',''), build.get('second_job','')
    jobs = [second, first, store.job_family(second,first)]
    selected = []
    for slot in SLOTS:
        item = rows.get(slot,{})
        tier = next((index[(job,item.get('name',''))] for job in jobs if (job,item.get('name','')) in index), None)
        if tier is None:
            return dict(active=False, label='未激活', reason='需装备全部七件武器与防具（A级及以上）', attrs={}, tiers=[])
        selected.append((*tier,int(float(item.get('enhance',0) or 0))))
    level, quality, enhance = (min(row[i] for row in selected) for i in range(3))
    candidates = [r for r in resonance_tiers() if r['level']<=level and r['quality']<=quality]
    if not candidates or enhance<10:
        return dict(active=False,label='未激活',reason='最低强化未达到 +10 或无对应共鸣档位',attrs={},tiers=[])
    tier_key = max((r['level'],r['quality']) for r in candidates)
    active = sorted((r for r in candidates if (r['level'],r['quality'])==tier_key and r['enhance']<=enhance), key=lambda r:r['enhance'])
    attrs = defaultdict(float)
    for row in active:
        for key,value in row['attrs'].items():
            attrs[key] += value
    label = f"{tier_key[0]}{'ASL'[tier_key[1]-3]} +{min(13,enhance)}"
    return dict(active=bool(active),label=label,reason='由七件装备最低等级、品质与强化决定；+10 至当前档位逐档叠加',
                attrs={key:round(value,8) for key,value in attrs.items()}, tiers=active)
