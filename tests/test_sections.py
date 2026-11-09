"""공정 짝짓기 지원: 위치 계산, 이름 같은 짝 제안, 사용자 짝 지정 파일 보호."""

import csv
import json

import pytest
from openpyxl import load_workbook

from kdoc import sections


def _jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    raw, interim = tmp_path / "raw" / "P1", tmp_path / "interim" / "P1"
    raw.mkdir(parents=True), interim.mkdir(parents=True)
    (raw / "manifest.json").write_text(json.dumps({"files": [
        {"seq": 2, "file": "02_spec_건축시방서.pdf"}, {"seq": 6, "file": "06_boq_내역서.xlsx"},
        {"seq": 9, "error": "다운로드 실패"},
    ]}, ensure_ascii=False), encoding="utf-8")
    _jsonl(interim / "spec_clauses.jsonl", [
        {"doc_id": "P1-02", "chapter_no": None, "chapter": "", "page_start": 1},  # 머리말: 제외
        {"doc_id": "P1-02", "chapter_no": 6, "chapter": "방 수 공 사", "page_start": 26},
        {"doc_id": "P1-02", "chapter_no": 6, "chapter": "방 수 공 사", "page_start": 24},
        {"doc_id": "P1-02", "chapter_no": 9, "chapter": "도장공사", "page_start": 30},
    ])
    _jsonl(interim / "boq_items.jsonl", [
        {"doc_id": "P1-06", "section_code": "0107", "section": "방수공사", "section_path": ["방수공사"], "sheet": "공종별내역서", "row": 128},
        {"doc_id": "P1-06", "section_code": "0107", "section": "방수공사", "section_path": ["방수공사"], "sheet": "공종별내역서", "row": 125},
        {"doc_id": "P1-06", "section_code": "0110", "section": "칠공사", "section_path": ["칠공사"], "sheet": "공종별내역서", "row": 140},
    ])
    monkeypatch.setattr(sections, "RAW", tmp_path / "raw")
    monkeypatch.setattr(sections, "INTERIM", tmp_path / "interim")
    monkeypatch.setattr(sections, "REVIEW", tmp_path / "review")
    monkeypatch.setattr(sections, "PAIRS", tmp_path / "pairs")
    return tmp_path


def test_locations_use_first_page_and_first_row(proj):
    ch = sections.spec_chapters("P1")
    assert [(c["chapter"], c["page"], c["clauses"]) for c in ch] == [("방 수 공 사", 24, 2), ("도장공사", 30, 1)]
    sec = sections.boq_sections("P1")
    assert [(s["section"], s["row"], s["items"]) for s in sec] == [("방수공사", 125, 2), ("칠공사", 140, 1)]


def test_suggests_only_same_name_pairs_ignoring_spaces(proj):
    pairs = sections.suggest_pairs(sections.spec_chapters("P1"), sections.boq_sections("P1"))
    assert [(p["spec_chapter"], p["boq_section"], p["note"]) for p in pairs] == [("방 수 공 사", "방수공사", sections.AUTO_NOTE)]


def test_listing_marks_unpaired_and_links_to_location(proj):
    out = sections.write_listing("P1")
    assert out["pairs_file_created"] and out["pairs"] == 1
    assert out["spec_unpaired"] == 1 and out["boq_unpaired"] == 1  # 도장공사 ↔ 칠공사 는 사용자가 지정
    wb = load_workbook(out["listing"])
    spec = list(wb["시방서 장"].iter_rows(min_row=2, values_only=True))
    assert [r[0] for r in spec] == ["O", "미지정"]
    assert wb["시방서 장"]["G2"].hyperlink.target.endswith(".pdf#page=24")
    link = wb["내역서 공종"]["I2"].hyperlink
    assert link.target.endswith("06_boq_내역서.xlsx") and link.location == "'공종별내역서'!A125"


def test_user_pairs_file_is_never_overwritten(proj):
    sections.write_listing("P1")
    path = proj / "pairs" / "P1.csv"
    # 사용자가 엑셀에서 짝을 추가하고 '다른 이름으로 저장(CSV)' → cp949 로 저장된 경우
    rows = sections.read_pairs(path) + [{
        "spec_doc": "P1-02", "spec_chapter_no": "9", "spec_chapter": "도장공사",
        "boq_doc": "P1-06", "boq_section_code": "0110", "boq_section": "칠공사", "note": "직접 지정",
    }]
    with path.open("w", encoding="cp949", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sections.PAIR_FIELDS)
        w.writeheader(), w.writerows(rows)

    out = sections.write_listing("P1")
    assert not out["pairs_file_created"] and out["pairs"] == 2
    assert out["spec_unpaired"] == 0 and out["boq_unpaired"] == 0
    assert "직접 지정" in path.read_text(encoding="cp949")
