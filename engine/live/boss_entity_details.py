"""Entity-local HP is never divided by the encounter damage budget."""
from .boss_health_wire import _maximum
from .nightmare_sea_dragon import PHASES


def entity_details(snapshot, encounter, templates, hp_reference=None):
    rows = []
    targets = {row.target_id: row for row in getattr(snapshot, 'damage_targets', ())}
    for target_id in sorted(encounter.target_ids):
        template_id = str(target_id).rsplit('@', 1)[-1]
        template = templates.get(template_id, {})
        damage_row = targets.get(target_id)
        tracks = [t for t in getattr(snapshot, 'boss_hp_tracks', ()) if t.target_id == target_id
                  and t.confidence == 'packet_hp']
        track = max(tracks, key=lambda t: t.last_seconds) if tracks else None
        full_hp = _maximum(target_id, encounter.name, int(template.get('hp', 0)))
        from .nest_hp_reference import entity_max, known_template, damage_budget
        if known_template(target_id):
            from .nest_hp_reference import select_reference
            reference = hp_reference or select_reference(snapshot, encounter.target_ids)
            full_hp = entity_max(reference, target_id)
        required = None
        if hp_reference:
            required = damage_budget(hp_reference, [target_id])
        phase_label = ''
        for phase in PHASES:
            if template_id in {str(value) for value in phase['template_ids']}:
                required = phase['required_damage']
                phase_label = f"阶段{phase['phase']}"
        if template_id in {'4181001', '4181009'}:
            required = full_hp / 2
            phase_label = '前10管' if template_id == '4181001' else '后10管'
        current = track.current_hp if track else None
        percent = (100 * current / full_hp if current is not None
                   and full_hp > 0 and 0 <= current <= full_hp else None)
        rows.append(dict(target_id=target_id, template_id=template_id,
                         name=template.get('name') or encounter.name,
                         phase=phase_label, full_hp=full_hp,
                         required_damage=required,
                         damage=damage_row.damage if damage_row else None,
                         current_hp=current, current_percent=percent,
                         first_ms=damage_row.first_ms if damage_row else encounter.first_ms,
                         last_ms=damage_row.last_ms if damage_row else encounter.last_ms))
    return sorted(rows, key=lambda row: (row['first_ms'], row['target_id']))
