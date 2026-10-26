"""User-triggered download and conversion job orchestration."""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from tkinter import messagebox


class JobActions:
    def start_download(self):
        folder, documents = self.dataset.get().strip(), self.documents.get()
        if not folder:
            messagebox.showerror("폴더 필요", "다운로드 폴더를 지정하세요.")
            return
        self.status.set("다운로드 중...")

        def work():
            code = self.run_worker(["download", folder] + (["--documents"] if documents else []))
            if code == 0:
                self.events.put(("scan", None))
            else:
                self.events.put(
                    (
                        "status",
                        (
                            "다운로드 중지됨"
                            if self.cancel.is_set()
                            else "다운로드 실패 — 로그를 확인하세요."
                        ),
                    )
                )

        self.start(work)

    def start_conversion(self):
        selected = list(self.tree.selection())
        if not selected:
            messagebox.showinfo("IFC 선택", "변환할 IFC를 먼저 선택하세요.")
            return
        if not self.output.get().strip():
            messagebox.showerror("폴더 필요", "출력 폴더를 지정하세요.")
            return
        output = Path(self.output.get()).expanduser().resolve()
        dataset = Path(self.dataset.get()).expanduser().resolve()
        if (dataset / "projects").is_dir():
            dataset = dataset / "projects"
        overwrite = self.overwrite.get()
        jobs, destinations = [], set()
        for iid in selected:
            source = self.files[iid]
            relative = (
                source.relative_to(dataset)
                if source.is_relative_to(dataset)
                else Path(source.parent.name) / source.name
            )
            destination = output / relative.with_suffix(".sqlite")
            if destination in destinations:
                messagebox.showerror(
                    "출력 이름 중복",
                    f"출력 이름이 겹칩니다: {destination}\n파일을 나누어 변환하거나 출력 폴더를 변경하세요.",
                )
                return
            destinations.add(destination)
            jobs.append((iid, source, destination))

        def work():
            results = []
            for i, (iid, source, destination) in enumerate(jobs, 1):
                if self.cancel.is_set():
                    break
                self.events.put(
                    ("status", f"변환 {i}/{len(jobs)}: {source.parent.name}/{source.name}")
                )
                self.events.put(("row", (iid, "변환 중", str(destination))))
                if destination.exists() and not overwrite:
                    state = "건너뜀"
                    self.events.put(("log", f"기존 DB 건너뜀: {destination}"))
                else:
                    code = self.run_worker(
                        ["convert", source, destination] + (["--overwrite"] if overwrite else [])
                    )
                    state = "중지됨" if self.cancel.is_set() else ("완료" if code == 0 else "실패")
                self.events.put(("row", (iid, state, str(destination))))
                results.append({"input": str(source), "output": str(destination), "status": state})
            output.mkdir(parents=True, exist_ok=True)
            report = output / ("batch_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
            report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            counts = {
                s: sum(x["status"] == s for x in results)
                for s in ("완료", "실패", "건너뜀", "중지됨")
            }
            self.events.put(("log", f"일괄 보고서: {report}"))
            self.events.put(
                (
                    "status",
                    f'작업 {"중지" if self.cancel.is_set() else "종료"}: 완료 {counts["완료"]}, 실패 {counts["실패"]}, 건너뜀 {counts["건너뜀"]}',
                )
            )

        self.start(work)
