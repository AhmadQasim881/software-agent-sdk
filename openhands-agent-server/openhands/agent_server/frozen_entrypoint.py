"""Dispatch fixed research subprocess modes before loading the server runtime."""

import runpy
import sys

from openhands.research.runtime import (
    HARNESS_MODE,
    HTTP_WORKER_MODE,
    HTTP_WORKER_SOURCE,
)


def main() -> None:
    if sys.argv[1:2] == [HARNESS_MODE]:
        del sys.argv[1]
        from openhands.research import main as research_main

        raise SystemExit(research_main())
    if sys.argv[1:2] == [HTTP_WORKER_MODE]:
        del sys.argv[1]
        sys.argv[0] = str(HTTP_WORKER_SOURCE)
        # Running the fixed bundled script avoids importing the SDK or its tools.
        runpy.run_path(str(HTTP_WORKER_SOURCE), run_name="__main__")
        return

    from openhands.agent_server.__main__ import main as server_main

    server_main()


if __name__ == "__main__":
    main()
