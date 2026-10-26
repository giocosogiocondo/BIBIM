"""Widget construction and file-selection presentation."""

from __future__ import annotations
import os
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from bibim_config import PROJECT_ROOT as ROOT, VERSION


class InterfaceLayout:
    def build_widgets(self, window):
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"BIBIM ver {VERSION}", font=("Segoe UI", 17, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text="IFC2X3 / IFC4 / IFC4X3 ADD2 → 해석된 Attribute·표준 Pset·Qto View + 객체별 CustomPset View",
        ).pack(anchor="w", pady=(3, 10))
        download = ttk.LabelFrame(outer, text="1. IFC-Bench 다운로드", padding=10)
        download.pack(fill="x")
        ttk.Label(
            download,
            text="Hugging Face: sylvainHellin/ifc-bench · V2 전체 파일 (최신 목록에서 IFC 자동 발견)",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        entry = ttk.Entry(download, textvariable=self.dataset)
        entry.grid(row=1, column=0, sticky="ew", pady=6)
        self.actions.append(entry)
        self.button(download, "폴더 선택", self.pick_dataset).grid(row=1, column=1, padx=5)
        self.button(download, "벤치마크 다운로드", self.start_download).grid(row=1, column=2)
        check = ttk.Checkbutton(
            download,
            text="벤치마크 부속 PDF / Excel 포함 (IFC 변환에는 사용하지 않음)",
            variable=self.documents,
        )
        check.grid(row=2, column=0, columnspan=3, sticky="w")
        self.actions.append(check)
        download.columnconfigure(0, weight=1)
        convert = ttk.LabelFrame(outer, text="2. 변환할 IFC 선택", padding=10)
        convert.pack(fill="both", expand=True, pady=10)
        toolbar = ttk.Frame(convert)
        toolbar.pack(fill="x", pady=(0, 7))
        for label, command in [
            ("다운로드 폴더 스캔", self.scan),
            ("IFC 파일 추가", self.add_files),
            ("IFC 폴더 추가", self.add_folder),
            ("전체 선택", self.select_all),
            ("선택 항목 제거", self.remove),
        ]:
            self.button(toolbar, label, command).pack(side="left", padx=(0, 5))
        frame = ttk.Frame(convert)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            frame,
            columns=("file", "state", "output"),
            show="headings",
            selectmode="extended",
            height=8,
        )
        for key, label, width in [
            ("file", "IFC 파일 / 프로젝트", 350),
            ("state", "상태", 100),
            ("output", "출력 DB", 380),
        ]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        bottom = ttk.Frame(convert)
        bottom.pack(fill="x", pady=(9, 0))
        ttk.Label(bottom, text="출력 폴더").pack(side="left")
        entry = ttk.Entry(bottom, textvariable=self.output)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        self.actions.append(entry)
        self.button(bottom, "폴더 선택", self.pick_output).pack(side="left")
        check = ttk.Checkbutton(bottom, text="기존 DB 덮어쓰기", variable=self.overwrite)
        check.pack(side="left", padx=8)
        self.actions.append(check)
        self.button(bottom, "선택 IFC 변환", self.start_conversion).pack(side="left")
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        self.stop_button = ttk.Button(
            footer, text="작업 중지", command=self.stop, state="disabled"
        )
        self.stop_button.pack(side="right")
        self.button(footer, "결과 폴더 열기", self.open_output).pack(side="right", padx=5)
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=6)
        self.logbox = ScrolledText(outer, height=10, state="disabled", font=("Consolas", 9))
        self.logbox.pack(fill="both")
        ttk.Label(
            outer,
            text="다운로드 후 자동 변환하지 않습니다. 전체 선택 후 변환 버튼을 누르세요. Ctrl/Shift로 다중 선택 가능합니다.",
        ).pack(anchor="w", pady=(5, 0))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.after(100, self.poll)

    def button(self, parent, text, command):
        widget = ttk.Button(parent, text=text, command=command)
        self.actions.append(widget)
        return widget

    def pick_dataset(self):
        path = filedialog.askdirectory(initialdir=ROOT)
        if path:
            self.dataset.set(path)

    def pick_output(self):
        path = filedialog.askdirectory(initialdir=ROOT)
        if path:
            self.output.set(path)

    def add_paths(self, paths):
        existing = set(self.files.values())
        for path in sorted(set(Path(p).resolve() for p in paths)):
            if path in existing:
                continue
            iid = self.tree.insert("", "end", values=(str(path), "대기", ""))
            self.files[iid] = path
            existing.add(path)
        self.select_all()
        self.status.set(f"IFC {len(self.files)}개 — 변환할 항목을 선택하세요.")

    def scan(self):
        folder = Path(self.dataset.get()).expanduser()
        if not folder.is_dir():
            messagebox.showinfo("입력 폴더", "먼저 다운로드하거나 IFC 파일을 추가하세요.")
            return
        self.add_paths(
            p
            for p in folder.rglob("*")
            if p.is_file()
            and p.suffix.lower() == ".ifc"
            and "_variants" not in p.relative_to(folder).parts
        )

    def add_files(self):
        self.add_paths(
            filedialog.askopenfilenames(filetypes=[("IFC files", "*.ifc"), ("All files", "*.*")])
        )

    def add_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.add_paths(
                p for p in Path(path).rglob("*") if p.is_file() and p.suffix.lower() == ".ifc"
            )

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def remove(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.files.pop(iid, None)

    def open_output(self):
        path = Path(self.output.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)
