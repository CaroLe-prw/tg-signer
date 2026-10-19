import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("nicegui")
from nicegui import Client, ui

from tg_signer.webui.runtime import TaskManager
from tg_signer.webui.task_controls import TaskControlBlock


@pytest.mark.asyncio
async def test_control_selection_start_stop_and_error(monkeypatch, tmp_path):
    manager = TaskManager(tmp_path)
    (tmp_path / "second.session").touch()
    (tmp_path / "signs" / "daily").mkdir(parents=True)
    calls = []
    notices = []
    monkeypatch.setattr(ui, "notify", lambda message, **_: notices.append(message))

    async def start(*key):
        calls.append(("start", key))
        manager.runs[manager.key(*key)] = SimpleNamespace(
            account="second",
            task="daily",
            workdir=tmp_path,
            active=True,
            status="运行中",
            started_at="now",
            ended_at="",
            returncode=None,
        )

    async def stop(*key):
        calls.append(("stop", key))
        run = manager.get(*key)
        run.active = False
        run.status = "已停止"
        run.returncode = 0

    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "stop", stop)
    client = Client(ui.page("/test-controls"))
    try:
        with client:
            controls = TaskControlBlock(manager, lambda: tmp_path, "second")
            assert controls.account.value == "second"
            assert controls.task.value == "daily"
            assert controls.start_button.enabled
            assert not controls.stop_button.enabled
            await controls.start()
            assert controls.status.text == "状态：运行中"
            assert controls.stop_button.enabled
            assert not controls.start_button.enabled
            # A second browser page sees the same process and cannot start it twice.
            second = TaskControlBlock(manager, lambda: tmp_path, "second")
            assert second.status.text == "状态：运行中"
            assert not second.start_button.enabled
            await second.stop()
            controls.refresh_status()
            assert controls.status.text == "状态：已停止"
            assert controls.start_button.enabled
            assert [call[0] for call in calls] == ["start", "stop"]
            assert calls[0][1] == (tmp_path, "second", "daily")

            async def fail(*_):
                raise ValueError("login required")

            monkeypatch.setattr(manager, "start", fail)
            await controls.start()
            assert notices == ["login required"]
            assert not controls.busy
    finally:
        client.delete()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_empty_controls_disable_actions(tmp_path):
    client = Client(ui.page("/test-empty"))
    try:
        with client:
            controls = TaskControlBlock(TaskManager(tmp_path), lambda: tmp_path)
            assert not controls.start_button.enabled
            assert not controls.stop_button.enabled
            assert not controls.clear_button.enabled
            assert "未找到账号" in controls.hint.text
    finally:
        client.delete()
        await asyncio.sleep(0)
