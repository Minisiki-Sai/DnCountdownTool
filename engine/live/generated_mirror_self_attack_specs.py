"""Independent two-capture 33101/30706 proof; no whole-template/PvP alias.

Evidence: outputs/v26_mirror_self_attack_20260908/machine_map_extension.json.
Other ACT variants (293137, 292222) remain deliberately unvalidated.
"""
from types import MappingProxyType
from .summon_self_attack_evidence import AttackSpec

MIRROR_SELF_ATTACK_SPECS = MappingProxyType({
    (33101, 30706): AttackSpec(
        33101, 30706, 2410,
        "mirror33101-self30706-pve-b99f7953dff9642b92495a6d25f5cb480c8f674e34e5cb82cf31c77587327d46",
        ("Activate",), (), "pve"),
})
