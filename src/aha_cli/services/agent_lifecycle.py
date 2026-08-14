"""Atomic terminal transitions for a task agent's lifecycle.

The interrupt and stale-recovery paths each stopped a backend, advanced the
chat offset, marked the agent interrupted, and recorded a recovery context --
but they duplicated that bookkeeping across two web modules. A failure in the
middle (e.g. ``stop_backend`` crashing before advancing the offset) left the
cursor stale, so the next backend start replayed already-delivered messages
and skipped the user's follow-up.

This module owns the terminal transition as one primitive so every path that
stops an agent for good produces the same consistent state:

- the chat cursor advanced past every message already in the inbox
- the agent marked ``interrupted``
- a recovery context recorded so its next turn knows it was interrupted
"""
from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.chat_offsets import advance_chat_offset_to_inbox_end
from aha_cli.store.agents import update_agent_runtime
from aha_cli.store.filesystem import set_agent_status


def record_agent_interrupt(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    reason: str,
    recovery_context: str,
) -> dict:
    """Terminalize an agent as interrupted with one consistent transition.

    Advances the chat cursor to the inbox end, marks the agent ``interrupted``,
    and records a recovery context so its next turn knows the previous one was
    interrupted. Returns the updated agent dict.

    Both the normal ``/aha interrupt`` path and the stale-recovery path call
    this, so a stopped backend always leaves the same state -- never a killed
    process with an unadvanced offset.
    """
    advance_chat_offset_to_inbox_end(root, run_id, agent_id, task_id)
    set_agent_status(root, run_id, task_id, agent_id, "interrupted")
    return update_agent_runtime(
        root,
        run_id,
        task_id,
        agent_id,
        recovery_context=recovery_context,
        recovery_context_reason=reason,
        recovery_context_at=utc_now(),
        recovery_context_consumed_at="",
    )


__all__ = ["record_agent_interrupt"]
