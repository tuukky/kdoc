"""검토 실행 전체(추출→짝→비교→엑셀)와 화면 프로그램 기동. 원본 공고 파일(data/raw/P1)이 있을 때만."""

import json
import os
import csv

import pytest
from openpyxl import load_workbook

from kdoc import RAW, review, sections

P1 = RAW / "P1"
needs_p1 = pytest.mark.skipif(not (P1 / "manifest.json").exists(), reason="python -m kdoc fetch --project P1 필요")


def _p1_files():
    m = json.loads((P1 / "manifest.json").read_text(encoding="utf-8"))
    files = {"spec": [], "boq": [], "drawing": []}
    for f in m["files"]:
        if "file" in f:
            files[f["doc_type"]].append(P1 / f["file"])
    return files


def test_needs_spec_and_boq(tmp_path):
    with pytest.raises(ValueError):
        review.run_review({"spec": [], "boq": []}, tmp_path)


@needs_p1
def test_p1_review_without_ai_writes_linked_excel(tmp_path):
    res = review.run_review(_p1_files(), tmp_path, api_key=None, cache=tmp_path / "cache")
    wb = load_workbook(res["excel"])
    assert wb.sheetnames == ["요약", "공정 짝", "시방서→내역서", "내역서→시방서", "안내"]

    # 직접 찾았던 P1 방수공사 사례가 양방향으로 잡힌다
    s2b = {(r["종류"], r["자재"]) for r in res["spec_to_boq"]}
    b2s = {(r["종류"], r["자재"]) for r in res["boq_to_spec"]}
    assert ("내역서에 없음", "우레탄 방수") in s2b and ("시방서에 없음", "시멘트 액체방수") in b2s

    row = next(r for r in res["spec_to_boq"] if r["자재"] == "우레탄 방수")
    assert row["시방서 쪽"] == 24 and row["_spec_path"].endswith(".pdf")
    ws = wb["시방서→내역서"]
    n = next(i for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), 2) if r[1] == "우레탄 방수")
    cols = [c.value for c in ws[1]]
    assert ws.cell(n, cols.index("시방서 열기") + 1).hyperlink.target.endswith(".pdf#page=24")
    assert ws.cell(n, cols.index("내역서 열기") + 1).hyperlink.location.endswith("!A125")
    assert "키 없음" in res["summary"]["AI"]["error"]


@needs_p1
def test_registered_review_uses_manual_csv_on_next_run(tmp_path):
    files, doc_ids = review.project_inputs(RAW, "P1")
    csv_path = tmp_path / "P1.csv"
    baseline = review.run_review(files, tmp_path, doc_ids=doc_ids, pairs_file=csv_path)
    original = next(r for r in baseline["pairs"] if r["시방서 장"] == "6장 방수공사")
    assert "방수공사" in original["내역서 공종"]
    manual = {"spec_doc": "P1-02", "spec_chapter_no": "6", "spec_chapter": "방수공사",
              "boq_doc": "P1-06", "boq_section_code": "010104", "boq_section": "타일공사", "note": "확정"}
    with csv_path.open("w", encoding="cp949", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=sections.PAIR_FIELDS)
        writer.writeheader()
        writer.writerows([manual, {**manual, "boq_doc": "P1-99"}])
    result = review.run_review(files, tmp_path, doc_ids=doc_ids, pairs_file=csv_path)
    chosen = next(r for r in result["pairs"] if r["시방서 장"] == "6장 방수공사")
    assert chosen["내역서 공종"] == "010104 타일공사"
    assert chosen["근거"] == "사용자 지정 (CSV)"
    assert "P1-99" in result["summary"]["공정 짝 경고"][0]
    assert result["excel"] != baseline["excel"]


@needs_p1
def test_gui_starts_and_shows_results(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")  # Anaconda 기반 .venv 에선 건너뜀 (.venv-app 에서 실행)
    QApplication = QtWidgets.QApplication

    from kdoc import gui

    monkeypatch.setattr(gui.pairing, "load_key", lambda: None)
    app = QApplication.instance() or QApplication([])
    w = gui.Main()
    files = _p1_files()
    w.slots[0].list.add_paths([str(p) for p in files["spec"]] + [str(files["boq"][0])])  # 엑셀은 시방서 칸에 안 들어감
    assert w.slots[0].list.count() == len(files["spec"])
    res = review.run_review(files, tmp_path, api_key=None, cache=tmp_path / "cache")
    w.on_done(res)
    t, _ = w.tables["spec_to_boq"]
    assert t.rowCount() == len(res["spec_to_boq"]) and w.excel_btn.isEnabled()
    w.close()
    app.processEvents()
