"""Task controls shared by authenticated dashboard pages."""

from pathlib import Path
from typing import Callable

from nicegui import ui

from tg_signer.webui.data import list_task_names
from tg_signer.webui.runtime import TaskManager


class TaskControlBlock:
    def __init__(
        self,
        manager: TaskManager,
        workdir: Callable[[], Path],
        default_account: str = "my_account",
    ):
        self.manager = manager
        self.workdir = workdir
        self.default_account = default_account
        self.busy = False
        with ui.card().classes("w-full"):
            ui.label("签到任务控制").classes("text-lg font-semibold")
            ui.label(
                "先保存配置，再选择账号启动。启动后可能立即发送；修改配置后需停止并重新启动。"
            ).classes("text-gray-600")
            ui.label(
                "同一账号同时运行一个任务，一个任务可包含多个群。"
                "关闭浏览器不会停止任务；关闭网页服务会停止其管理的任务。"
            ).classes("text-gray-600")
            ui.label(f"账号目录：{manager.session_dir}").classes(
                "text-sm text-gray-500"
            )
            with ui.row().classes("items-end gap-3 w-full"):
                self.account = ui.select(
                    [], label="执行账号", on_change=self.refresh_status
                ).classes("min-w-[220px]")
                self.task = ui.select(
                    [], label="已保存的签到任务", on_change=self.refresh_status
                ).classes("min-w-[220px]")
                ui.button("刷新账号和任务", on_click=self.refresh)
            self.hint = ui.label().classes("text-sm text-gray-500")
            with ui.row().classes("gap-2"):
                self.start_button = ui.button(
                    "启动任务", icon="play_arrow", on_click=self.start
                )
                self.stop_button = ui.button(
                    "停止任务", icon="stop", color="negative", on_click=self.stop
                )
            self.status = ui.label("请选择账号和任务").classes("font-semibold")
            self.details = ui.label().classes("text-sm text-gray-500")
            ui.label("本服务管理的任务（包含其他工作目录）").classes(
                "font-semibold mt-2"
            )
            self.table = ui.table(
                columns=[
                    {"name": name, "label": label, "field": name}
                    for name, label in (
                        ("account", "账号"),
                        ("task", "任务"),
                        ("status", "状态"),
                        ("workdir", "工作目录"),
                    )
                ],
                rows=[],
                row_key="key",
            ).classes("w-full")
            ui.label("任务日志").classes("text-lg font-semibold mt-2")
            with ui.row().classes("items-center"):
                ui.button("刷新任务日志", on_click=self.refresh_status)
                self.clear_button = ui.button(
                    "清除任务日志", on_click=self.confirm_clear
                ).props("outline")
                ui.label("每 2 秒自动刷新，显示最新 300 行（最多 64 KiB）").classes(
                    "text-sm text-gray-500"
                )
            self.logs = ui.code("暂无日志", language="text").classes(
                "w-full max-h-96 overflow-auto whitespace-pre-wrap"
            )
        self.refresh()
        ui.timer(2, self.refresh_status)

    def selection(self):
        if not self.account.value or not self.task.value:
            raise ValueError("请先选择账号和任务")
        return (self.workdir().resolve(), self.account.value, self.task.value)

    def refresh(self):
        accounts = self.manager.accounts()
        tasks = list_task_names("signer", self.workdir())
        # Keep stopped/deleted entries selectable so their logs remain accessible.
        for run in self.manager.runs.values():
            if run.workdir == self.workdir().resolve():
                accounts = sorted(set(accounts) | {run.account})
                tasks = sorted(set(tasks) | {run.task})
        account = (
            self.account.value
            if self.account.value in accounts
            else (
                self.default_account
                if self.default_account in accounts
                else next(iter(accounts), None)
            )
        )
        task = self.task.value if self.task.value in tasks else next(iter(tasks), None)
        self.account.set_options(accounts, value=account)
        self.task.set_options(tasks, value=task)
        self.hint.set_text(
            "账号来自本机 session 文件；未完成或已失效的登录需先在终端重新登录。"
            if accounts
            else "未找到账号，请先通过 tg-signer --account 账号名 login 登录，再刷新。"
        )
        self.refresh_status()

    def refresh_status(self):
        if not hasattr(self, "status"):
            return
        self.table.rows = [
            {
                "key": str(key),
                "account": run.account,
                "task": run.task,
                "status": run.status,
                "workdir": str(run.workdir),
            }
            for key, run in self.manager.runs.items()
        ]
        self.table.update()
        selected = bool(self.account.value and self.task.value)
        run = self.manager.get(*self.selection()) if selected else None
        active = bool(run and run.active)
        occupied = any(
            r.active and r.account == self.account.value
            for r in self.manager.runs.values()
        )
        self.start_button.set_enabled(selected and not occupied and not self.busy)
        self.stop_button.set_enabled(active and not self.busy)
        self.clear_button.set_enabled(selected and not self.busy)
        self.status.set_text(
            f"状态：{run.status}"
            if run
            else "状态：未启动"
            if selected
            else "请选择账号和任务"
        )
        self.details.set_text(
            f"启动：{run.started_at}　结束：{run.ended_at or '—'}　退出码："
            f"{run.returncode if run.returncode is not None else '—'}"
            if run
            else ""
        )
        try:
            content = self.manager.read_logs(*self.selection()) if selected else ""
            content = content or "暂无日志"
        except OSError as exc:
            content = f"读取日志失败：{exc}"
        if self.logs.content != content:
            self.logs.set_content(content)

    async def start(self):
        await self._action(self.manager.start)

    async def stop(self):
        await self._action(self.manager.stop)

    async def _action(self, action):
        self.busy = True
        try:
            key = self.selection()
            self.refresh_status()
            await action(*key)
        except Exception as exc:
            ui.notify(str(exc), type="negative")
        finally:
            self.busy = False
            self.refresh_status()

    async def confirm_clear(self):
        key = self.selection()
        with ui.dialog() as dialog, ui.card():
            ui.label(f"清除账号 {key[1]} / 任务 {key[2]} 的全部任务日志？")
            ui.label("不会删除签到记录或停止任务；运行中的任务仍会继续写入新日志。")
            with ui.row():
                ui.button("取消", on_click=lambda: dialog.submit(False))
                ui.button(
                    "确认清除", color="negative", on_click=lambda: dialog.submit(True)
                )
        if await dialog:
            try:
                self.manager.clear_logs(*key)
                ui.notify("任务日志已清除", type="positive")
            except OSError as exc:
                ui.notify(str(exc), type="negative")
            self.refresh_status()
        dialog.delete()
