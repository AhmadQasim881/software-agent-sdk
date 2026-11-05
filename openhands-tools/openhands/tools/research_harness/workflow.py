"""Load the packaged workflow through the SDK's native AgentSkills mechanism."""

from pathlib import Path

from openhands.sdk.skills import Skill


def workflow_path() -> Path:
    return Path(__file__).parent / "skills" / "research-review" / "SKILL.md"


def load_workflow_skill() -> Skill:
    return Skill.load(workflow_path(), strict=True, skip_mcp=True)
