"""Assemble a local SDK conversation around an existing research thread."""

from pathlib import Path
from uuid import uuid4

from openhands.sdk import LLM, Agent, AgentContext, LocalConversation, Tool
from openhands.sdk.context.condenser import (
    LLMSummarizingCondenser,
    RollingCondenser,
)
from openhands.sdk.plugin import load_available_plugins

from .condenser import CheckpointCondenser
from .definition import ResearchHarnessTool
from .hooks import make_hook_config
from .models import HarnessError
from .store import HarnessStore
from .workflow import load_workflow_skill


def create_research_conversation(
    *,
    llm: LLM,
    workspace: Path,
    state_dir: Path,
    thread_id: str,
    allow_github: bool = False,
    condenser: RollingCondenser | None = None,
) -> LocalConversation:
    """Create a conversation with native skill loading, hooks, and checkpoints.

    The caller supplies an already configured LLM. Construction validates the
    existing research thread and makes no model request. Each conversation has
    its own persisted SDK history while sharing the selected durable thread.

    This scoped helper requires no currently discoverable ambient plugins. Its
    preflight does not prevent concurrent configuration changes by other local
    processes. Custom SDK sessions can use ResearchHarnessTool with their own
    plugin configuration.
    """
    store = HarnessStore(state_dir, workspace)
    store.get_thread(thread_id)
    plugins = load_available_plugins(
        work_dir=store.workspace, include_user=True, include_project=True
    )
    if plugins:
        names = ", ".join(repr(name[:80]) for name in sorted(plugins)[:8])
        omitted = f" (and {len(plugins) - 8} more)" if len(plugins) > 8 else ""
        raise HarnessError(
            "Scoped research conversations require an environment without ambient "
            f"plugins. Found: {names}{omitted}. The harness CLI, MCP, and "
            "ResearchHarnessTool in custom SDK sessions remain usable."
        )
    conversation_id = uuid4()
    context = AgentContext(
        skills=[load_workflow_skill()],
        load_user_skills=False,
        load_public_skills=False,
        load_project_skills=False,
        load_memory=False,
    )
    delegate = condenser if condenser is not None else LLMSummarizingCondenser(llm=llm)
    agent = Agent(
        llm=llm,
        tools=[
            Tool(
                name=ResearchHarnessTool.name,
                params={
                    "state_dir": str(store.state_dir),
                    "allow_github": allow_github,
                },
            )
        ],
        agent_context=context,
        condenser=CheckpointCondenser(
            delegate=delegate,
            workspace=store.workspace,
            state_dir=store.state_dir,
            thread_id=thread_id,
            session_id=str(conversation_id),
        ),
    )
    return LocalConversation(
        agent=agent,
        workspace=store.workspace,
        persistence_dir=store.state_dir / "conversations",
        conversation_id=conversation_id,
        hook_config=make_hook_config(
            workspace=store.workspace,
            state_dir=store.state_dir,
            thread_id=thread_id,
        ),
        visualizer=None,
    )
