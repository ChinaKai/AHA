"""Test package for AHA CLI."""

from __future__ import annotations

import os


AHA_RUNTIME_ENV_KEYS = (
    "AHA_HOME",
    "AHA_ROOT",
    "AHA_RUN_ID",
    "AHA_TASK_ID",
    "AHA_AGENT_ID",
    "AHA_BACKEND",
    "AHA_MODEL",
    "AHA_GENERATED_BY",
    "AHA_WSL_DISTRO",
    "AHA_WSL_AHA_HOME",
    "AHA_WSL_AHA_BIN",
    "AHA_RUNTIME_PYTHON",
)


# pytest applies the autouse fixture in conftest.py, but direct unittest runs do
# not load conftest. Scrub inherited production routing before either runner
# imports a test module so an in-process CLI call cannot target a live AHA home.
for _key in AHA_RUNTIME_ENV_KEYS:
    os.environ.pop(_key, None)
