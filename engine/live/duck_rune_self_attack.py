"""Post-admission-only rune duck attacks, proved from installed AI/ACT/DNT.

No nearest cast, m114 creator fallback, whole-duck identity or new hit admission.
261101 lacks its explicit loop phase in the DNT and is NOT added by analogy.
Reproducible certificate: tools/build_duck_rune_self_attack.py.
"""
from hashlib import sha256
from types import MappingProxyType
from .summon_self_attack_evidence import AttackSpec

CERTIFICATE='6d7f5d1610fa566896f84d3025f4b5e3215e7de725bfcd3d713efae8762ae51d'
RUNE_DUCK_SELF_ATTACK_SPECS=MappingProxyType({
    (32005,sid):AttackSpec(32005,sid,4217,sha256(f'{CERTIFICATE}:{sid}'.encode('ascii')).hexdigest(),
        ('Attack_Shock_Start_Mutant','Attack_Shock_Mutant_Loop','Attack_Shock_Mutant_End'))
    for sid in range(261102,261118)
})
