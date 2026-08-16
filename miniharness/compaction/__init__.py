"""miniharness.compaction — minimal context compaction family.

Mirrors packages/compaction/ with a teaching‑focused subset:
  • tokenMeter (L1) – incremental fold + usage anchoring
  • BasicCompactionEngine (L3) – pressure trigger + overflow recovery
  • Compaction checkpoints (log‑only, non‑surface events)
  • install_compaction(ctx, config) – idempotent assembly‑site helper
"""
from __future__ import annotations

from . import config as _config
from . import engine as _engine
from . import region as _region
from . import summarizer as _summarizer

__all__ = [
    "CONTEXT_WINDOW_EXCEEDED",
    "TokenMeter",
    "TargetPressureConfigError",
    "CompactionEngine",
    "compact_surface_region",
    "inspect_compaction_entry_state",
    "select_compactable_range",
    "resolve_config",
    "resolve_spec",
    "install_compaction",
]

# ----------------------------------------------------------------------
# Public API re‑exports (kept lightweight – deep implementation lives in
# the sub‑modules above).
# ----------------------------------------------------------------------
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"

TokenMeter = _engine.TokenMeter  # noqa: F401  (re‑export for convenience)
TargetPressureConfigError = _config.TargetPressureConfigError  # noqa: F401

CompactionEngine = _engine.CompactionEngine  # noqa: F401

compact_surface_region = _region.compact_surface_region  # noqa: F401
inspect_compaction_entry_state = _region.inspect_compaction_entry_state  # noqa: F401
select_compactable_range = _region.select_compactable_range  # noqa: F401

resolve_config = _config.resolve_config  # noqa: F401
resolve_spec = _config.resolve_spec  # noqa: F401


def install_compaction(ctx, config: dict | None = None):
    """Idempotent assembly‑site helper: creates a CompactionEngine and registers
    the automatic pre‑step / request‑error listeners.

    Idempotent – calling it more than once is a no‑op (mirrors
    apply_retry_planner’s pattern in llm/retry.py).
    """
    if getattr(ctx, "_miniharness_compaction_installed", False):
        return
    ctx._miniharness_compaction_installed = True
    _engine.CompactionEngine(ctx, config)