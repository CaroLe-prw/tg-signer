import asyncio
import json
import sqlite3
import sys

import pytest

from tg_signer.webui.runtime import TaskManager, validate_task


@pytest.fixture
def runtime(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for account in ("alice", "bob"):
        with sqlite3.connect(session_dir / f"{account}.session") as connection:
            connection.execute("CREATE TABLE sessions (user_id INTEGER)")
            connection.execute("INSERT INTO sessions VALUES (123)")
    workdir = tmp_path / "work"
    for task in ("daily", "other"):
        path = workdir / "signs" / task / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "chats": [
                        {
                            "chat_id": -1001234,
                            "actions": [{"action": 1, "text": "hello"}],
                        }
                    ],
                    "sign_at": "*/5 * * * *",
                }
            )
        )
    manager = TaskManager(session_dir)
    return manager, workdir


def stub_command(manager, monkeypatch, script=None):
    script = script or (
        "import time\n"
        'print("ready", flush=True)\n'
        "while True:\n"
        ' print("tick", flush=True)\n'
        " time.sleep(.03)\n"
    )
    monkeypatch.setattr(
        manager, "command", lambda *_: [sys.executable, "-u", "-c", script]
    )


async def wait_for_log(manager, key, text):
    async def wait():
        while text not in manager.read_logs(*key):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 5)


@pytest.mark.asyncio
async def test_lifecycle_duplicate_account_and_log_clear(runtime, monkeypatch):
    manager, workdir = runtime
    key = (workdir, "alice", "daily")
    stub_command(manager, monkeypatch)
    try:
        run = await manager.start(*key)
        await wait_for_log(manager, key, "ready")
        assert run.active
        with pytest.raises(ValueError, match="已有运行"):
            await manager.start(*key)
        with pytest.raises(ValueError, match="已有运行"):
            await manager.start(workdir, "alice", "other")
        with pytest.raises(ValueError, match="已有运行"):
            await manager.start(workdir / "another", "alice", "daily")

        bob = await manager.start(workdir, "bob", "daily")
        await wait_for_log(manager, (workdir, "bob", "daily"), "ready")
        path = manager.log_path(*key)
        path.with_name(path.name + ".1").write_text("old log")
        record = workdir / "data.sqlite3"
        record.write_bytes(b"unchanged records")
        manager.clear_logs(*key)
        assert manager.read_logs(*key) == ""
        assert not path.with_name(path.name + ".1").exists()
        assert "ready" in manager.read_logs(workdir, "bob", "daily")
        assert record.read_bytes() == b"unchanged records"
        await wait_for_log(manager, key, "tick")
        assert "ready" not in manager.read_logs(*key)
        await manager.stop(*key)
        assert run.status == "已停止"
        assert run.process.returncode is not None
        assert run.ended_at
        assert bob.active
        manager.clear_logs(*key)
        assert manager.read_logs(*key) == ""
        restarted = await manager.start(*key)
        assert restarted is not run
    finally:
        await manager.close()
    assert all(not r.active for r in manager.runs.values())
    with pytest.raises(ValueError, match="关闭"):
        await manager.start(*key)


@pytest.mark.asyncio
async def test_concurrent_clicks_start_only_one_process(runtime, monkeypatch):
    manager, workdir = runtime
    stub_command(manager, monkeypatch)
    try:
        results = await asyncio.gather(
            manager.start(workdir, "alice", "daily"),
            manager.start(workdir, "alice", "daily"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert len(manager.runs) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_code", "status"), [(0, "已结束"), (7, "异常退出")])
async def test_exit_status_and_logs_survive_manager_restart(
    runtime, monkeypatch, exit_code, status
):
    manager, workdir = runtime
    stub_command(
        manager,
        monkeypatch,
        f'import sys; print("worker output"); sys.exit({exit_code})',
    )
    run = await manager.start(workdir, "alice", "daily")
    await asyncio.wait_for(run.watcher, 5)
    assert run.status == status
    assert run.returncode == exit_code
    assert not run.active
    fresh = TaskManager(manager.session_dir)
    assert "worker output" in fresh.read_logs(workdir, "alice", "daily")
    fresh.clear_logs(workdir, "alice", "daily")
    assert fresh.read_logs(workdir, "alice", "daily") == ""


@pytest.mark.asyncio
async def test_large_output_is_bounded_and_rotated(runtime, monkeypatch):
    manager, workdir = runtime
    stub_command(manager, monkeypatch, 'print("x" * 2200000); print("tail marker")')
    run = await manager.start(workdir, "alice", "daily")
    await asyncio.wait_for(run.watcher, 5)
    path = manager.log_path(workdir, "alice", "daily")
    assert path.stat().st_size < 1024 * 1024
    assert path.with_name(path.name + ".1").exists()
    assert "tail marker" in manager.read_logs(workdir, "alice", "daily")
    assert len(manager.read_logs(workdir, "alice", "daily")) <= 65536
    manager.clear_logs(workdir, "alice", "daily")
    assert not path.with_name(path.name + ".1").exists()


@pytest.mark.asyncio
async def test_invalid_session_config_and_path_never_spawn(runtime, monkeypatch):
    manager, workdir = runtime

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration started a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    for account, task in [
        ("missing", "daily"),
        ("alice", "missing"),
        ("../alice", "daily"),
        ("alice", "../daily"),
    ]:
        with pytest.raises(ValueError):
            await manager.start(workdir, account, task)
    with sqlite3.connect(manager.session_dir / "alice.session") as connection:
        connection.execute("UPDATE sessions SET user_id = NULL")
    with pytest.raises(ValueError, match="未完成登录"):
        await manager.start(workdir, "alice", "daily")


def test_schedule_validation_and_log_isolation(runtime):
    manager, workdir = runtime
    path = workdir / "signs" / "daily" / "config.json"
    config = json.loads(path.read_text())
    config["sign_at"] = "08:00:00"
    path.write_text(json.dumps(config))
    validate_task(workdir, "daily")
    config["sign_at"] = "invalid"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="签到时间"):
        validate_task(workdir, "daily")
    assert manager.log_path(workdir, "alice", "daily") != manager.log_path(
        workdir, "bob", "daily"
    )
    assert manager.log_path(workdir, "alice", "daily") != manager.log_path(
        workdir, "alice", "other"
    )
    assert manager.log_path(workdir, "alice", "daily") != TaskManager(
        "other-sessions"
    ).log_path(workdir, "alice", "daily")


@pytest.mark.asyncio
async def test_selected_account_cannot_be_overridden_by_env(runtime, monkeypatch):
    manager, workdir = runtime
    monkeypatch.setenv("TG_SESSION_STRING", "must-not-reach-worker")
    manager.proxy = {"scheme": "socks5", "hostname": "127.0.0.1", "port": 1080}
    stub_command(
        manager,
        monkeypatch,
        (
            "import os, json\n"
            'assert "TG_SESSION_STRING" not in os.environ\n'
            'assert json.loads(os.environ["TG_SIGNER_WEB_PROXY"])["port"] == 1080\n'
        ),
    )
    run = await manager.start(workdir, "alice", "daily")
    await asyncio.wait_for(run.watcher, 5)
    assert run.returncode == 0


@pytest.mark.asyncio
async def test_worker_reuses_signer_with_selected_account(runtime, monkeypatch):
    from argparse import Namespace

    from tg_signer.webui import worker

    manager, workdir = runtime
    calls = []

    class FakeSigner:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def run(self, count):
            calls.append(count)

    monkeypatch.setattr(worker, "UserSigner", FakeSigner)
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *_: None
    )
    monkeypatch.setenv("TG_SIGNER_WEB_PROXY", '{"hostname": "localhost"}')
    await worker.run(
        Namespace(
            session_dir=str(manager.session_dir),
            workdir=str(workdir),
            account="alice",
            task="daily",
        )
    )
    assert calls[0]["account"] == "alice"
    assert calls[0]["task_name"] == "daily"
    assert calls[0]["session_dir"] == str(manager.session_dir)
    assert calls[0]["proxy"] == {"hostname": "localhost"}
    assert calls[1] == 20
    with pytest.raises(RuntimeError, match="交互输入"):
        worker.no_input("phone number?")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_stop_kills_unresponsive_worker(runtime, monkeypatch):
    import tg_signer.webui.runtime as module

    manager, workdir = runtime
    monkeypatch.setattr(module, "STOP_TIMEOUT", 0.1)
    stub_command(
        manager,
        monkeypatch,
        (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            'print("ready", flush=True)\n'
            "time.sleep(60)\n"
        ),
    )
    key = (workdir, "alice", "daily")
    try:
        run = await manager.start(*key)
        await wait_for_log(manager, key, "ready")
        await manager.stop(*key)
        assert run.status == "已停止"
        assert run.returncode == -9
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_worker_cancels_task_when_parent_disappears(monkeypatch):
    from tg_signer.webui import worker

    monkeypatch.setattr(worker.os, "getppid", lambda: 1)
    task = asyncio.create_task(asyncio.sleep(60))
    await worker.watch_parent(task, 99999)
    with pytest.raises(asyncio.CancelledError):
        await task
