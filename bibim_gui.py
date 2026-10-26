"""Tk launcher facade: UI layout, job actions and process lifecycle are separate."""

import queue
import threading
import tkinter as tk
from bibim_config import PROJECT_ROOT as ROOT, MODEL_ROOT as DATASET, OUTPUT_ROOT, VERSION
from bibim_ui.layout import InterfaceLayout
from bibim_ui.workers import WorkerLifecycle, terminate_worker
from bibim_ui.actions import JobActions

__all__ = ["App", "ROOT", "DATASET", "terminate_worker"]


class App(InterfaceLayout, JobActions, WorkerLifecycle):
    def __init__(self, window):
        self.window = window
        window.title(f"BIBIM {VERSION} — IFC 다운로드 / 문서 기반 DB 변환")
        window.geometry("1040x760")
        window.minsize(860, 620)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.process = None
        self.worker_started = None
        self.last_output = None
        self.busy = False
        self.files = {}
        self.actions = []
        self.dataset = tk.StringVar(value=str(DATASET))
        self.output = tk.StringVar(value=str(OUTPUT_ROOT))
        self.documents = tk.BooleanVar(value=True)
        self.overwrite = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="준비 — 다운로드하거나 로컬 IFC를 추가하세요.")
        self.build_widgets(window)


if __name__ == "__main__":
    window = tk.Tk()
    App(window)
    window.mainloop()
