"""Console bootstrap that keeps SDK banners out of JSON and MCP output."""

import os


def main() -> int:
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from openhands.tools.research_harness.cli import main as cli_main

    return cli_main()
