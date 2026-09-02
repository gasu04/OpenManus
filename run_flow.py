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

import asyncio
import time

from app.agent.data_analysis import DataAnalysis
from app.agent.manus import Manus
from app.config import config
from app.flow.flow_factory import FlowFactory, FlowType
from app.logger import logger


async def run_flow():
    agents = {
        "manus": Manus(),
    }
    if config.run_flow_config.use_data_analysis_agent:
        agents["data_analysis"] = DataAnalysis()
    try:
        prompt = input("Enter your prompt: ")

        if prompt.strip().isspace() or not prompt:
            logger.warning("Empty prompt provided.")
            return

        flow = FlowFactory.create_flow(
            flow_type=FlowType.PLANNING,
            agents=agents,
        )
        logger.warning("Processing your request...")

        try:
            start_time = time.time()
            result = await asyncio.wait_for(
                flow.execute(prompt),
                timeout=3600,  # 60 minute timeout for the entire execution
            )
            elapsed_time = time.time() - start_time
            logger.info(f"Request processed in {elapsed_time:.2f} seconds")
            logger.info(result)
        except asyncio.TimeoutError:
            logger.error("Request processing timed out after 1 hour")
            logger.info(
                "Operation terminated due to timeout. Please try a simpler request."
            )

    except KeyboardInterrupt:
        logger.info("Operation cancelled by user.")
    except Exception as e:
        logger.error(f"Error: {str(e)}")


if __name__ == "__main__":
    asyncio.run(run_flow())
