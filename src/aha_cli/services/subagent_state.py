from __future__ import annotations

TERMINAL_AGENT_STATUSES = {"completed", "failed", "blocked", "interrupted", "stopped"}


def sub_agents(task: dict) -> list[dict]:
    return [agent for agent in task.get("agents", []) if agent.get("role") == "sub"]


def active_sub_agent_count(task: dict) -> int:
    return sum(1 for agent in sub_agents(task) if agent.get("status") not in TERMINAL_AGENT_STATUSES)


def _agent_activity_at(agent: dict) -> str:
    timestamps = [
        str(value).strip()
        for value in (
            agent.get("last_active_at"),
            agent.get("status_started_at"),
            agent.get("started_at"),
            agent.get("finished_at"),
            agent.get("reused_at"),
        )
        if value
    ]
    return max(timestamps) if timestamps else ""


def current_round_sub_agents(task: dict) -> list[dict]:
    agents = sub_agents(task)
    coordination = task.get("coordination") or {}
    followup_started_at = str(coordination.get("followup_started_at") or "").strip()
    if not followup_started_at:
        return agents
    return [agent for agent in agents if _agent_activity_at(agent) >= followup_started_at]


def pending_sub_agents(task: dict) -> list[dict]:
    return [agent for agent in sub_agents(task) if agent.get("status") not in TERMINAL_AGENT_STATUSES]


def pending_current_round_sub_agents(task: dict) -> list[dict]:
    return [agent for agent in current_round_sub_agents(task) if agent.get("status") not in TERMINAL_AGENT_STATUSES]


def task_has_incomplete_sub_agents(task: dict) -> bool:
    return bool(pending_current_round_sub_agents(task))


def waiting_for_subagents_message(task: dict) -> str:
    agents = current_round_sub_agents(task)
    pending = pending_current_round_sub_agents(task)
    if not pending:
        return "所有子 agent 已完成，等待 task-main 做本轮汇总。"
    names = ", ".join(agent.get("id", "-") for agent in pending)
    done = len(agents) - len(pending)
    return f"等待子 agent 完成：{names}。当前进度 {done}/{len(agents)}。"


def continuing_with_subagents_message(task: dict) -> str:
    agents = current_round_sub_agents(task)
    pending = pending_current_round_sub_agents(task)
    if not pending:
        return "子 agent 已完成，task-main 将继续处理本轮汇总。"
    names = ", ".join(agent.get("id", "-") for agent in pending)
    done = len(agents) - len(pending)
    return f"已分配子 agent 并让 task-main 继续主线工作；后台等待：{names}。当前进度 {done}/{len(agents)}。"
