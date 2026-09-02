import os
import sys

# ----------------------------------------------------------------------------
# Self-activation shim: ensure this script always runs under this repo's own
# virtual environment (.venv), regardless of whatever venv (e.g. another
# project's) happens to be active in the caller's shell. This keeps OpenManus
# fully isolated from other environments on the machine.
#
# Must run BEFORE any third-party / app imports, since those fail when the
# wrong interpreter is active (e.g. ModuleNotFoundError: boto3).
# ----------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_VENV_PYTHON = os.path.join(_REPO_ROOT, ".venv", "bin", "python")
_REEXEC_FLAG = "OPENMANUS_VENV_STARTED"

if (
    os.path.isfile(_VENV_PYTHON)
    and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PYTHON)
    and os.environ.get(_REEXEC_FLAG) != "1"
):
    # Re-launch ourselves under the repo's venv interpreter, preserving args.
    os.environ[_REEXEC_FLAG] = "1"
    os.execv(
        _VENV_PYTHON,
        [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]],
    )

import argparse
import asyncio

from app.agent.manus import Manus
from app.logger import logger


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Run Manus agent with a prompt")
    parser.add_argument(
        "--prompt", type=str, required=False, help="Input prompt for the agent"
    )
    args = parser.parse_args()

    # Create and initialize Manus agent
    agent = await Manus.create()
    try:
        # Use command line prompt if provided, otherwise ask for input
        prompt = args.prompt if args.prompt else input("Enter your prompt: ")
        if not prompt.strip():
            logger.warning("Empty prompt provided.")
            return

        logger.warning("Processing your request...")
        await agent.run(prompt)
        logger.info("Request processing completed.")
    except KeyboardInterrupt:
        logger.warning("Operation interrupted.")
    finally:
        # Ensure agent resources are cleaned up before exiting
        await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
