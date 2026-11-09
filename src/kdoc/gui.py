"""kdoc 화면 프로그램 (PySide6).

시방서·내역서·도면 칸에 파일을 끌어다 놓거나 찾아서 넣고 [검토 시작] → 화면 표 + 결과 엑셀.
표의 줄을 더블클릭하면 원본을 그 위치로 연다 (PDF: 브라우저로 해당 쪽, 엑셀: 파일을 열고 시트·행 안내).
OpenAI 키는 [키 설정]에서 넣으면 윈도우 자격 증명 관리자에 저장된다 (EXE·엑셀에는 저장하지 않음).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import compare, pairing, review

SLOTS = [  # (doc_type, 제목, 받는 확장자, 안내)
    ("spec", "시방서", (".pdf",), "PDF"),
    ("boq", "내역서", (".xlsx", ".xls"), "엑셀 (xlsx·xls)"),
    ("drawing", "도면", (".pdf",), "PDF · 이번 버전은 참고용(비교 안 함)"),
]
ROW_COLORS = {pairing.UNSURE: "#FFF2CC", pairing.NONE: "#F8CBAD", compare.KIND_VALUE_DIFF: "#F8CBAD"}


class DropList(QListWidget):
    """파일을 끌어다 놓을 수 있는 목록. 확장자가 맞지 않는 파일은 받지 않는다."""

    rejected = Signal(list)

    def __init__(self, exts: tuple[str, ...]):
        super().__init__()
        self.exts = exts
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumHeight(80)
        self.setMaximumHeight(120)

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls()])
        e.acceptProposedAction()

    def add_paths(self, paths: list[str]) -> None:
        have = set(self.paths())
        bad = []
        for p in paths:
            path = Path(p)
            if path.suffix.lower() not in self.exts or not path.is_file():
                bad.append(path.name)
            elif str(path) not in have:
                item = QListWidgetItem(path.name)
                item.setData(Qt.UserRole, str(path))
                item.setToolTip(str(path))
                self.addItem(item)
                have.add(str(path))
        if bad:
            self.rejected.emit(bad)

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))


class Slot(QGroupBox):
    def __init__(self, doc_type: str, title: str, exts: tuple[str, ...], hint: str):
        super().__init__(title)
        self.doc_type, self.title_text = doc_type, title
        self.list = DropList(exts)
        lay = QVBoxLayout(self)
        tip = QLabel(f"여기에 끌어다 놓기 · {hint}")
        tip.setStyleSheet("color:#666;")
        lay.addWidget(tip)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        add, rm = QPushButton("파일 찾기…"), QPushButton("빼기")
        add.clicked.connect(self.browse)
        rm.clicked.connect(self.list.remove_selected)
        row.addWidget(add)
        row.addWidget(rm)
        lay.addLayout(row)
        self.exts = exts

    def browse(self) -> None:
        pattern = " ".join(f"*{e}" for e in self.exts)
        files, _ = QFileDialog.getOpenFileNames(self, f"{self.title_text} 파일 선택", "", f"{self.title_text} ({pattern})")
        self.list.add_paths(files)


class Worker(QObject):
    progress = Signal(str, float)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, files: dict, out_dir: Path, api_key: str | None):
        super().__init__()
        self.files, self.out_dir, self.api_key = files, out_dir, api_key

    def run(self) -> None:
        try:
            res = review.run_review(self.files, self.out_dir, api_key=self.api_key,
                                    progress=lambda m, f: self.progress.emit(m, f))
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")


def _browser() -> str | None:
    """#page=N 을 지키는 브라우저 (Edge → Chrome)."""
    pf = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    candidates = [Path(b) / rel for b in pf if b for rel in (
        "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe")]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("msedge") or shutil.which("chrome")


def open_pdf(path: str, page: int | None) -> None:
    url = Path(path).as_uri() + (f"#page={page}" if page else "")
    exe = _browser()
    if exe:
        subprocess.Popen([exe, url])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def open_file(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — 사용자가 고른 파일을 기본 프로그램으로
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("kdoc — 시방서·내역서 검토 보조")
        self.resize(1280, 860)
        self.result: dict | None = None
        self.thread: QThread | None = None

        root = QWidget()
        lay = QVBoxLayout(root)

        slots_row = QHBoxLayout()
        self.slots = [Slot(*s) for s in SLOTS]
        for s in self.slots:
            s.list.rejected.connect(self.on_rejected)
            slots_row.addWidget(s)
        lay.addLayout(slots_row)

        opt = QHBoxLayout()
        opt.addWidget(QLabel("결과 폴더"))
        self.out_edit = QLineEdit(str(Path.home() / "Documents" / "kdoc 검토결과"))
        opt.addWidget(self.out_edit, 1)
        pick = QPushButton("변경…")
        pick.clicked.connect(self.pick_out)
        opt.addWidget(pick)
        opt.addSpacing(20)
        self.key_label = QLabel()
        opt.addWidget(self.key_label)
        key_btn = QPushButton("OpenAI 키 설정…")
        key_btn.clicked.connect(self.set_key)
        opt.addWidget(key_btn)
        lay.addLayout(opt)

        run_row = QHBoxLayout()
        self.run_btn = QPushButton("검토 시작")
        self.run_btn.setMinimumHeight(40)
        self.run_btn.setStyleSheet("font-size:15px; font-weight:bold;")
        self.run_btn.clicked.connect(self.start)
        run_row.addWidget(self.run_btn, 1)
        self.excel_btn = QPushButton("결과 엑셀 열기")
        self.excel_btn.setEnabled(False)
        self.excel_btn.clicked.connect(lambda: self.result and open_file(self.result["excel"]))
        run_row.addWidget(self.excel_btn)
        self.folder_btn = QPushButton("결과 폴더 열기")
        self.folder_btn.setEnabled(False)
        self.folder_btn.clicked.connect(lambda: self.result and open_file(self.result["dir"]))
        run_row.addWidget(self.folder_btn)
        lay.addLayout(run_row)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.status = QLabel("시방서와 내역서를 넣고 [검토 시작]을 누르세요.")
        lay.addWidget(self.bar)
        lay.addWidget(self.status)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("padding:6px; background:#F3F6FA; border-radius:4px;")
        self.tabs = QTabWidget()
        self.tables = {}
        for key, title, cols in [("pairs", "공정 짝", review.PAIR_COLS),
                                 ("spec_to_boq", "시방서 → 내역서", review.FINDING_COLS),
                                 ("boq_to_spec", "내역서 → 시방서", review.FINDING_COLS)]:
            t = QTableWidget(0, len(cols))
            t.setHorizontalHeaderLabels(cols)
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.setSortingEnabled(True)
            t.setWordWrap(False)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            t.cellDoubleClicked.connect(lambda r, c, k=key: self.open_row(k, r, c))
            self.tables[key] = (t, cols)
            self.tabs.addTab(t, title)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.summary)
        hint = QLabel("줄을 더블클릭하면 원본을 엽니다 — 시방서 열(또는 왼쪽)을 누르면 시방서 해당 쪽, 내역서 열을 누르면 내역서 파일. "
                      + review.DISCLAIMER)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        bl.addWidget(hint)
        bl.addWidget(self.tabs, 1)
        lay.addWidget(bottom, 1)

        self.setCentralWidget(root)
        self.setAcceptDrops(False)
        self.refresh_key()

    # ── 설정 ──
    def refresh_key(self) -> None:
        has = bool(pairing.load_key())
        self.key_label.setText("AI 공정 짝: 사용" if has else "AI 공정 짝: 키 없음 (이름 같은 공정만)")
        self.key_label.setStyleSheet("color:#2E7D32;" if has else "color:#B26A00;")

    def set_key(self) -> None:
        key, ok = QInputDialog.getText(
            self, "OpenAI 키 설정",
            "OpenAI API 키를 붙여넣으세요.\n윈도우 자격 증명 관리자에 저장되며 프로그램·결과 파일에는 남지 않습니다.\n"
            "비워두고 확인을 누르면 저장된 키를 지웁니다.\nAI에는 시방서 장·내역서 공종 '이름'만 보냅니다.",
            QLineEdit.Password)
        if ok:
            try:
                pairing.save_key(key.strip() or None)
            except Exception as e:
                QMessageBox.warning(self, "키 저장 실패", str(e))
            self.refresh_key()

    def pick_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "결과 폴더", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def on_rejected(self, names: list) -> None:
        self.status.setText("이 칸에 맞지 않는 파일이라 넣지 않았습니다: " + ", ".join(names[:5]))

    # ── 실행 ──
    def start(self) -> None:
        files = {s.doc_type: [Path(p) for p in s.list.paths()] for s in self.slots}
        if not files["spec"] or not files["boq"]:
            QMessageBox.information(self, "파일 필요", "시방서와 내역서를 한 개 이상씩 넣어주세요.")
            return
        self.run_btn.setEnabled(False)
        self.bar.setValue(0)
        self.thread = QThread()
        self.worker = Worker(files, Path(self.out_edit.text()), pairing.load_key())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        for sig in (self.worker.done, self.worker.failed):
            sig.connect(self.thread.quit)
        self.thread.start()

    def on_progress(self, msg: str, frac: float) -> None:
        self.status.setText(msg)
        self.bar.setValue(int(frac * 100))

    def on_failed(self, msg: str) -> None:
        self.run_btn.setEnabled(True)
        self.status.setText("실패 — 자세한 내용은 창을 보세요.")
        QMessageBox.critical(self, "검토 실패", msg)

    def on_done(self, res: dict) -> None:
        self.result = res
        self.run_btn.setEnabled(True)
        self.excel_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
        s = res["summary"]
        ai = s["AI"]
        ai_txt = (f"AI {ai['model']} 사용" if ai["used"] else f"AI 미사용 ({ai['error'] or '판단할 공정 없음'})")
        errs = [d for d in s["문서"] if d.get("error") or d.get("skipped")]
        self.summary.setText(
            f"<b>공정</b> 시방서 장 {s['시방서 장']} · 내역서 공종 {s['내역서 공종']} → 짝 {s['짝']} · "
            f"<span style='color:#B26A00'>판단 불가 {s['판단 불가']}</span> · 없음 {s['없음']} ({ai_txt})<br>"
            f"<b>시방서→내역서</b> 내역서에 없음 {s['내역서에 없음']} · 수치 다름 {s['수치 다름']} · "
            f"수치 판단 불가 {s['수치 판단 불가']} &nbsp; "
            f"<b>내역서→시방서</b> 시방서에 없음 {s['시방서에 없음']}<br>"
            f"결과 엑셀: {res['excel']}"
            + (f"<br><span style='color:#C00'>읽지 못한 파일 {len(errs)}개: "
               + ", ".join(Path(d['path']).name for d in errs) + "</span>" if errs else ""))
        for key, (t, cols) in self.tables.items():
            self.fill(t, cols, res[key])
        self.status.setText(f"완료 — {s['소요(초)']}초")

    def fill(self, t: QTableWidget, cols: list[str], rows: list[dict]) -> None:
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            color = ROW_COLORS.get(row.get("판정") or row.get("종류"))
            for c, col in enumerate(cols):
                v = row.get(col, "")
                item = QTableWidgetItem()
                item.setData(Qt.DisplayRole, v if isinstance(v, int) else str(v or ""))
                item.setToolTip(str(v or ""))
                if c == 0:
                    item.setData(Qt.UserRole, row)  # 정렬해도 원래 줄 정보가 따라가게
                if color:
                    item.setBackground(QColor(color))
                t.setItem(r, c, item)
        t.setSortingEnabled(True)
        t.resizeColumnsToContents()
        for c in range(t.columnCount()):
            t.setColumnWidth(c, min(t.columnWidth(c), 380))

    def open_row(self, key: str, r: int, c: int) -> None:
        t, cols = self.tables[key]
        row = t.item(r, 0).data(Qt.UserRole)
        want_boq = cols[c].startswith("내역서")
        if (want_boq and row["_boq_path"]) or not row["_spec_path"]:
            if row["_boq_path"]:
                open_file(row["_boq_path"])
                self.status.setText(f"내역서를 열었습니다 → 시트 '{row['_boq_sheet']}' {row['_boq_row']}행을 보세요.")
        else:
            open_pdf(row["_spec_path"], row["_spec_page"])
            self.status.setText(f"시방서 {row['_spec_page']}쪽을 브라우저로 열었습니다.")


def selftest(out: Path) -> int:
    """EXE 빌드 확인용: 화면 없이 구성 요소가 다 들어갔는지 out(json)에 적는다. 키 값은 적지 않는다."""
    import json

    report: dict = {"frozen": bool(getattr(sys, "frozen", False))}
    try:
        import keyring
        report["keyring_backend"] = type(keyring.get_keyring()).__name__
        report["key_saved"] = bool(pairing.load_key())
    except Exception as e:
        report["keyring_error"] = repr(e)
    try:
        import openai
        report["openai"] = openai.__version__
    except Exception as e:
        report["openai_error"] = repr(e)
    try:
        mp = compare.materials_path()
        report["materials"] = str(mp)
        report["materials_count"] = len({n for _, n in compare.load_materials()})
    except Exception as e:
        report["materials_error"] = repr(e)
    report["cache_dir"] = str(pairing.cache_dir())
    app = QApplication.instance() or QApplication([])
    w = Main()
    report["window"] = w.windowTitle()
    w.close()
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        return selftest(Path(sys.argv[2]))
    app = QApplication(sys.argv)
    app.setApplicationName("kdoc")
    app.setStyle("Fusion")
    w = Main()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
