"""Server-owned signer processes; no Telegram calls run in the UI event loop."""

import asyncio
import codecs
import hashlib
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from croniter import croniter

from tg_signer.ai_tools import OpenAIConfigManager
from tg_signer.config import SignConfigV3

STOP_TIMEOUT = 8


def validate_name(name: str) -> str:
    if not name or name in {".", ".."} or any(c in name for c in "/\\\x00"):
        raise ValueError("请选择有效的账号和任务名称")
    return name


def session_path(session_dir: Path, account: str) -> Path:
    path = session_dir / f"{validate_name(account)}.session"
    if path.is_symlink():
        raise ValueError("不支持符号链接账号文件")
    return path


def validate_session(session_dir: Path, account: str) -> None:
    path = session_path(session_dir, account)
    if not path.is_file():
        raise ValueError("账号尚未登录，请先使用 login 命令登录此账号")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("无法读取账号登录状态，请检查 session 文件") from exc
    if not row or not row[0]:
        raise ValueError("账号尚未完成登录，请先使用 login 命令登录此账号")


def validate_task(workdir: Path, task: str) -> None:
    root = (workdir / "signs").resolve()
    path = root / validate_name(task) / "config.json"
    if not path.resolve().is_relative_to(root):
        raise ValueError("任务配置必须位于工作目录中")
    if not path.is_file():
        raise ValueError("请先保存签到配置，再启动任务")
    loaded = SignConfigV3.load(json.loads(path.read_text(encoding="utf-8")))
    if not loaded:
        raise ValueError("签到配置无效")
    config, _ = loaded
    expression = config.sign_at.replace("：", ":").strip()
    try:
        time.fromisoformat(expression)
        valid_schedule = True
    except ValueError:
        valid_schedule = croniter.is_valid(expression)
    if not config.chats or not valid_schedule:
        raise ValueError("请配置至少一个聊天和有效的签到时间")
    if config.requires_ai and not OpenAIConfigManager(workdir).load_config():
        raise ValueError("此任务需要 AI，请先通过 llm-config 或环境变量配置")


@dataclass
class TaskRun:
    workdir: Path
    account: str
    task: str
    process: asyncio.subprocess.Process
    handler: RotatingFileHandler
    started_at: str
    status: str = "运行中"
    ended_at: str = ""
    returncode: int | None = None
    watcher: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.status in {"运行中", "停止中"}


class TaskManager:
    def __init__(self, session_dir=".", proxy=None):
        self.session_dir = Path(session_dir).expanduser().resolve()
        self.proxy = proxy
        self.runs: dict[tuple[Path, str, str], TaskRun] = {}
        self.lock = asyncio.Lock()
        self.closing = False

    def accounts(self) -> list[str]:
        return sorted(
            p.stem
            for p in self.session_dir.glob("*.session")
            if p.is_file() and not p.is_symlink()
        )

    def key(self, workdir, account, task):
        return (
            Path(workdir).expanduser().resolve(),
            validate_name(account),
            validate_name(task),
        )

    def log_path(self, workdir, account, task) -> Path:
        workdir, account, task = self.key(workdir, account, task)
        identity = json.dumps([str(self.session_dir), account, task])
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return workdir / "webui_logs" / f"{digest}.log"

    def get(self, workdir, account, task) -> TaskRun | None:
        return self.runs.get(self.key(workdir, account, task))

    def command(self, workdir, account, task) -> list[str]:
        return [
            sys.executable,
            "-u",
            "-m",
            "tg_signer.webui.worker",
            "--workdir",
            str(workdir),
            "--session-dir",
            str(self.session_dir),
            "--account",
            account,
            "--task",
            task,
        ]

    async def start(self, workdir, account, task) -> TaskRun:
        async with self.lock:
            if self.closing:
                raise ValueError("网页服务正在关闭")
            key = self.key(workdir, account, task)
            workdir, account, task = key
            if any(r.active and r.account == account for r in self.runs.values()):
                raise ValueError(
                    "此账号已有运行中的任务，请先停止；一个任务可配置多个群"
                )
            validate_session(self.session_dir, account)
            validate_task(workdir, task)
            path = self.log_path(*key)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
            )
            env = os.environ.copy()
            # An inherited session string must never override the selected account.
            env.pop("TG_SESSION_STRING", None)
            env["TG_SIGNER_WEB_PROXY"] = json.dumps(self.proxy)
            env["TG_SIGNER_WEB_PARENT_PID"] = str(os.getpid())
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.command(*key),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
            except BaseException:
                handler.close()
                raise
            run = TaskRun(
                *key, process, handler, datetime.now().isoformat(timespec="seconds")
            )
            self.runs[key] = run
            self._write(run, f"任务启动：{run.started_at}，账号 {account}，任务 {task}")
            run.watcher = asyncio.create_task(self._watch(run))
            return run

    @staticmethod
    def _write(run, text):
        run.handler.emit(logging.LogRecord("task", logging.INFO, "", 0, text, (), None))

    async def _watch(self, run):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            while chunk := await run.process.stdout.read(4096):
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    self._write(run, line)
                if len(pending) > 16384:
                    self._write(run, pending)
                    pending = ""
            pending += decoder.decode(b"", final=True)
            if pending:
                self._write(run, pending)
            run.returncode = await run.process.wait()
            run.status = (
                "已停止"
                if run.status == "停止中"
                else "已结束"
                if run.returncode == 0
                else "异常退出"
            )
        finally:
            run.ended_at = datetime.now().isoformat(timespec="seconds")
            self._write(run, f"{run.status}：{run.ended_at}，退出码 {run.returncode}")
            run.handler.close()

    async def stop(self, workdir, account, task) -> None:
        # Serialize start/stop so a click cannot start a replacement before cleanup.
        async with self.lock:
            run = self.get(workdir, account, task)
            if not run or not run.active:
                return
            run.status = "停止中"
            if run.process.returncode is None:
                try:
                    run.process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(run.watcher), timeout=STOP_TIMEOUT
                )
            except asyncio.TimeoutError:
                if run.process.returncode is None:
                    try:
                        run.process.kill()
                    except ProcessLookupError:
                        pass
                await run.watcher

    async def close(self):
        self.closing = True
        await asyncio.gather(*(self.stop(*key) for key in list(self.runs)))

    def read_logs(self, workdir, account, task) -> str:
        path = self.log_path(workdir, account, task)
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 65536)
            stream.seek(offset)
            content = stream.read().decode("utf-8", errors="replace")
        if offset:
            content = content.partition("\n")[2]
        return "\n".join(content.splitlines()[-300:])

    def clear_logs(self, workdir, account, task) -> None:
        path = self.log_path(workdir, account, task)
        run = self.get(workdir, account, task)
        # All writes and truncation run on the same server event loop.
        if run and run.active and run.handler.stream:
            run.handler.flush()
            run.handler.stream.seek(0)
            run.handler.stream.truncate()
        elif path.exists():
            path.write_text("", encoding="utf-8")
        for backup in path.parent.glob(path.name + ".*"):
            if backup.suffix[1:].isdigit():
                backup.unlink()
