"""Non-interactive entry point for a WebUI-owned signer process."""

import argparse
import asyncio
import builtins
import json
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

from tg_signer.core import UserSigner
from tg_signer.logger import formatter
from tg_signer.webui.runtime import validate_session, validate_task


def no_input(*_args, **_kwargs):
    raise RuntimeError("任务需要交互输入，请先在终端完成账号登录和配置，再从网页启动")


async def watch_parent(task, parent_pid):
    # On POSIX, also stop if the UI server crashes without running shutdown hooks.
    while os.getppid() == parent_pid:
        await asyncio.sleep(1)
    task.cancel()


async def run(args):
    # Revalidate after spawning to avoid falling into CLI setup prompts.
    validate_session(Path(args.session_dir), args.account)
    validate_task(Path(args.workdir), args.task)
    signer = UserSigner(
        task_name=args.task,
        account=args.account,
        session_dir=args.session_dir,
        workdir=args.workdir,
        proxy=json.loads(os.environ.get("TG_SIGNER_WEB_PROXY", "null")),
        loop=asyncio.get_running_loop(),
    )
    task = asyncio.create_task(signer.run(20))
    loop = asyncio.get_running_loop()
    parent_watcher = None
    if os.name != "nt":
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        parent_pid = int(os.environ.get("TG_SIGNER_WEB_PARENT_PID", os.getppid()))
        parent_watcher = asyncio.create_task(watch_parent(task, parent_pid))
    try:
        await task
    except asyncio.CancelledError:
        logging.getLogger("tg-signer").info("任务已停止")
    finally:
        if parent_watcher:
            parent_watcher.cancel()
            with suppress(asyncio.CancelledError):
                await parent_watcher
        if os.name != "nt":
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)


def main():
    parser = argparse.ArgumentParser()
    for option in ("workdir", "session-dir", "account", "task"):
        parser.add_argument(f"--{option}", required=True)
    args = parser.parse_args()
    # The parent captures stdout/stderr into one bounded, per-task log.
    logger = logging.getLogger("tg-signer")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    builtins.input = no_input
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
