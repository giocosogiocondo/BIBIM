"""Background process lifecycle, cancellation and UI event delivery."""

from __future__ import annotations
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from tkinter import messagebox
from bibim_config import PROJECT_ROOT as ROOT


def terminate_worker(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # venv python.exe can be a launcher with the real interpreter as a child.
        # Stop only this worker's tree, never every Python process on the machine.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
    else:
        process.terminate()


class WorkerLifecycle:
    def start(self, work):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        for button in self.actions:
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.start(12)

        def wrapper():
            try:
                work()
            except Exception as exc:
                self.events.put(("log", f"오류: {exc}"))
                self.events.put(("status", "작업 실패 — 로그를 확인하세요."))
            finally:
                self.events.put(("done", None))

        threading.Thread(target=wrapper, daemon=True).start()

    def run_worker(self, args):
        if self.cancel.is_set():
            return -1
        command = [sys.executable, "-u", str(ROOT / "benchmark_tools.py"), *map(str, args)]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.process = process
        self.worker_started = self.last_output = time.monotonic()
        if self.cancel.is_set():
            terminate_worker(process)
        try:
            log_folder = ROOT / "logs"
            log_folder.mkdir(exist_ok=True)
            log_path = log_folder / (
                "worker_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log"
            )
            self.events.put(("log", f"상세 로그 저장: {log_path}"))
            with log_path.open("w", encoding="utf-8", buffering=1) as logfile:
                for line in process.stdout:
                    self.last_output = time.monotonic()
                    logfile.write(line)
                    self.events.put(("log", line.rstrip()))
            return process.wait()
        finally:
            process.stdout.close()
            self.process = None
            self.worker_started = None

    def stop(self):
        self.cancel.set()
        if self.process and self.process.poll() is None:
            try:
                terminate_worker(self.process)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.status.set("중지 요청 — 현재 프로세스를 종료합니다.")

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", data + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "status":
                    self.status.set(data)
                elif kind == "row":
                    iid, state, output = data
                    self.tree.set(iid, "state", state)
                    self.tree.set(iid, "output", output)
                elif kind == "scan":
                    self.scan()
                elif kind == "done":
                    self.busy = False
                    self.progress.stop()
                    self.stop_button.configure(state="disabled")
                    for button in self.actions:
                        button.configure(state="normal")
        except queue.Empty:
            pass
        if self.busy and self.worker_started is not None:
            elapsed = int(time.monotonic() - self.worker_started)
            quiet = int(time.monotonic() - self.last_output)
            base = self.status.get().split(" | 경과 ")[0]
            self.status.set(
                f"{base} | 경과 {elapsed//60}분 {elapsed%60}초 · 마지막 로그 {quiet}초 전"
            )
        self.window.after(100, self.poll)

    def close(self):
        if self.busy and not messagebox.askyesno(
            "작업 종료", "진행 중인 작업을 중지하고 닫을까요?"
        ):
            return
        self.stop()
        self.window.destroy()
