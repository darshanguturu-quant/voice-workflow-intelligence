"""Cohort evaluation, total-variation distance calculation, and config diffing.

Matches the simulator specification exactly.
"""
from __future__ import annotations

import copy
from typing import Any

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
ALL_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")


def match_cohort(features: dict[str, Any], cohorts: list[dict[str, Any]]) -> str:
    """Matches a lead's features to the first matching cohort id, or 'default'."""
    for c in cohorts:
        cid = c.get("id")
        where = c.get("where", {})
        matched = True
        for feat_name, allowed_vals in where.items():
            val = str(features.get(feat_name, ""))
            # allowed_vals is a list of strings
            if val not in [str(x) for x in allowed_vals]:
                matched = False
                break
        if matched:
            return cid
    return "default"


def get_dimension_distribution(config: dict[str, Any], cohort_id: str, dim: str) -> dict[str, float]:
    """Extracts the normalized distribution for a dimension within a cohort, falling back to default."""
    allocs = config.get("allocations", {})
    cohort_alloc = allocs.get(cohort_id, {})
    dim_alloc = cohort_alloc.get(dim)
    if dim_alloc is None and cohort_id != "default":
        dim_alloc = allocs.get("default", {}).get(dim, {})
    if not dim_alloc:
        return {}

    total = sum(dim_alloc.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in dim_alloc.items()}


def max_shift(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> float:
    """Computes largest total-variation distance of any agent-dim allocation across cohorts."""
    old_cohorts = {c["id"] for c in old_cfg.get("cohorts", [])}
    new_cohorts = {c["id"] for c in new_cfg.get("cohorts", [])}
    keys = {"default"} | old_cohorts | new_cohorts

    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a = get_dimension_distribution(old_cfg, k, dim)
            b = get_dimension_distribution(new_cfg, k, dim)
            all_options = set(a) | set(b)
            if not all_options:
                continue
            tv = 0.5 * sum(abs(a.get(o, 0.0) - b.get(o, 0.0)) for o in all_options)
            if tv > worst:
                worst = tv
    return worst


def changed_dims(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> list[str]:
    """Returns sorted list of changed configuration dimensions."""
    changed = []
    if old_cfg.get("cohorts") != new_cfg.get("cohorts"):
        changed.append("cohorts")

    for dim in AGENT_DIMS:
        # Check if allocation for this dim changed across any cohort
        old_allocs = {k: v.get(dim) for k, v in old_cfg.get("allocations", {}).items() if dim in v}
        new_allocs = {k: v.get(dim) for k, v in new_cfg.get("allocations", {}).items() if dim in v}
        if old_allocs != new_allocs:
            changed.append(dim)

    if old_cfg.get("prompt_version") != new_cfg.get("prompt_version"):
        changed.append("prompt_version")

    if old_cfg.get("schedule") != new_cfg.get("schedule"):
        changed.append("schedule")

    return sorted(changed)


def compute_diff(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> dict[str, Any]:
    """Generates a clean structural diff between two configs."""
    diff = {}
    dims = changed_dims(old_cfg, new_cfg)
    for dim in dims:
        if dim == "cohorts":
            diff["cohorts"] = {"old": old_cfg.get("cohorts"), "new": new_cfg.get("cohorts")}
        elif dim in AGENT_DIMS:
            diff[f"allocations.{dim}"] = {
                "old": {k: v.get(dim) for k, v in old_cfg.get("allocations", {}).items() if dim in v},
                "new": {k: v.get(dim) for k, v in new_cfg.get("allocations", {}).items() if dim in v},
            }
        elif dim == "prompt_version":
            diff["prompt_version"] = {"old": old_cfg.get("prompt_version"), "new": new_cfg.get("prompt_version")}
        elif dim == "schedule":
            diff["schedule"] = {"old": old_cfg.get("schedule"), "new": new_cfg.get("schedule")}
    return diff
