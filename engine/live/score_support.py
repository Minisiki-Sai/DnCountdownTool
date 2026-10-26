from __future__ import annotations

from .score_support_catalog import ScoreSupportCatalogMixin, bind_host_globals as _bind_ScoreSupportCatalogMixin
from .score_support_allocation import ScoreSupportAllocationMixin, bind_host_globals as _bind_ScoreSupportAllocationMixin
from .score_support_counterfactual import ScoreSupportCounterfactualMixin, bind_host_globals as _bind_ScoreSupportCounterfactualMixin


class ScoreSupportMixin(
    ScoreSupportCatalogMixin,
    ScoreSupportAllocationMixin,
    ScoreSupportCounterfactualMixin,
):
    pass


def bind_host_globals(host_globals: dict[str, object]) -> None:
    _bind_ScoreSupportCatalogMixin(host_globals)
    _bind_ScoreSupportAllocationMixin(host_globals)
    _bind_ScoreSupportCounterfactualMixin(host_globals)


# Extracted from scoring.py; algorithm bodies are unchanged.
