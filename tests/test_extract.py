"""추출기 단위 테스트 (실제 공고 3건에서 본 형식을 작은 예시로 재현) + 실데이터 회귀 테스트."""

import json

import pandas as pd
import pytest

from kdoc import INTERIM, RAW
from kdoc.extract import boq, spec
from kdoc.fetch import _filename


# ---------- 시방서 ----------

def _pages(*texts):
    return [{"page": i + 1, "text": t} for i, t in enumerate(texts)]


def test_spec_explicit_chapter_with_spaced_title():
    cl = spec.split_clauses(_pages("제 6장. 방 수 공 사\n1.1 적용범위\n우레탄 도막방수 3mm\n"))
    assert cl[-1]["chapter"] == "방수공사"
    assert cl[-1]["num"] == "1.1" and "우레탄" in cl[-1]["text"]


def test_spec_jeol_counts_as_chapter():
    cl = spec.split_clauses(_pages("제 1 절  일반공통사항 총칙\n1.  일반사항\n본문\n"))
    assert cl[-1]["chapter_no"] == 1 and cl[-1]["chapter"] == "일반공통사항총칙"


def test_spec_numbered_chapters_kcs_style():
    text = "1. 총 칙\n1. 일반사항\n본문\n2. 콘크리트공사\n1. 일반사항\n2. 재료\n레미콘 25-24-150\n3. 방수공사\n3. 시공\n"
    cl = spec.split_clauses(_pages(text))
    chapters = [c["chapter"] for c in cl]
    assert "총칙" in chapters and "콘크리트공사" in chapters and "방수공사" in chapters
    # 하위 '1. 일반사항', '2. 재료'는 장이 아니라 조항
    assert not any(c == "재료" for c in chapters)
    mat = next(c for c in cl if c["heading"] == "재료")
    assert mat["chapter"] == "콘크리트공사" and "레미콘" in mat["text"]


def test_spec_lone_number_line_merged():
    cl = spec.split_clauses(_pages("1.\n일반사항\n2.\n가설공사\n가. 적용범위\n"))
    assert [c["chapter"] for c in cl][-1] == "가설공사"


def test_spec_toc_page_skipped():
    cl = spec.split_clauses(_pages("목\n차\n1. 일반사항\n2. 가설공사\n", "1. 일반사항\n본문\n"))
    assert all(c["page_start"] == 2 for c in cl)


def test_spec_out_of_sequence_number_not_chapter():
    cl = spec.split_clauses(_pages("1. 가설공사\n2. 철근콘크리트공사\n1. m당 10mm 이하 필요에\n본문\n"))
    assert {c["chapter"] for c in cl} == {"철근콘크리트공사"}  # 본문 없는 '가설공사' 장은 조항이 없음
    assert cl[-1]["num"] == "1" and cl[-1]["heading"].startswith("m당")


# ---------- 내역서 ----------

def _df(rows):
    width = max(len(r) for r in rows)
    return pd.DataFrame([list(r) + [None] * (width - len(r)) for r in rows])


def test_boq_standard_layout():
    df = _df([
        ["[ 공사명 ]", None, None, None, None],
        ["품      명", "규      격", "단위", "수량", "비  고"],
        [None, None, None, None, None],
        ["010106  방  수  공  사", None, None, None, None],
        ["우레탄 도막방수", "바닥, 3mm", "M2", 120, "호표 5"],
        ["[ 합          계 ]", None, None, None, None],
        ["010107  금  속  공  사", None, None, None, None],
        ["스텐 코너비드", "STS304", "M", "1,250", None],
    ])
    items = boq.parse_sheet(df)
    assert len(items) == 2
    assert items[0]["section"] == "방수공사" and items[0]["spec"] == "바닥, 3mm" and items[0]["qty"] == 120
    assert items[1]["section_code"] == "010107" and items[1]["qty"] == 1250


def test_boq_mechanical_layout_name_in_third_column():
    df = _df([
        [None, None, "[공사명] 기계"],
        ["공종", "코드", "품          명", "규       격", "단위", "수량"],
        [None, None, None, None, None, None],
        [None, None, "0101  위생기구설치공사", None, None, None],
        ["0101", "45100017088", "원형세면기(SL,절수형)", "VL-1050", "SET", 43],
    ])
    items = boq.parse_sheet(df)
    assert items == [{**items[0], "name": "원형세면기(SL,절수형)", "section": "위생기구설치공사", "qty": 43.0}]


def test_boq_section_hierarchy_by_code_prefix():
    df = _df([
        ["품명", "규격", "단위", "수량"],
        ["0101  화장실", None, None, None],
        ["010101  방수공사", None, None, None],
        ["액체방수", None, "M2", 10],
        ["0102  외부", None, None, None],
        ["010201  도장공사", None, None, None],
        ["외벽 수성페인트", None, "M2", 50],
    ])
    items = boq.parse_sheet(df)
    assert items[0]["section_path"] == ["화장실", "방수공사"]
    assert items[1]["section_path"] == ["외부", "도장공사"]


def test_boq_detail_sheet_selection():
    names = ["원가계산서", "공종별집계표", "공종별내역서", "일위대가목록", "내역서", "단가대비표"]
    assert boq.detail_sheets(names) == ["공종별내역서", "내역서"]


# ---------- 수집 ----------

def test_filename_from_content_disposition():
    class R:
        headers = {"content-disposition": "attachment;filename=%EA%B3%B5%EB%82%B4%EC%97%AD%EC%84%9C.xlsx;"}
    assert _filename(R(), "x") == "공내역서.xlsx"


# ---------- 실데이터 회귀 (data/interim 이 있을 때만) ----------

def _jsonl(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


@pytest.mark.skipif(not (INTERIM / "P1" / "summary.json").exists(), reason="python -m kdoc extract 먼저 실행")
@pytest.mark.parametrize("pid,doc_id,chapter", [
    ("P1", "P1-02", "방수공사"), ("P2", "P2-05", "방수공사"),
    ("P3", "P3-03", "단열공사"), ("P3", "P3-04", "타일공사"),
])
def test_real_spec_chapters(pid, doc_id, chapter):
    cl = [c for c in _jsonl(INTERIM / pid / "spec_clauses.jsonl") if c["doc_id"] == doc_id]
    assert chapter in {c["chapter"] for c in cl}


@pytest.mark.skipif(not (INTERIM / "P1" / "summary.json").exists(), reason="python -m kdoc extract 먼저 실행")
def test_real_boq_and_ocr_flags():
    for pid in ("P1", "P2", "P3"):
        docs = json.loads((INTERIM / pid / "summary.json").read_text(encoding="utf-8"))["docs"]
        assert not any("error" in d for d in docs), pid
        assert sum(d.get("boq_items", 0) for d in docs) > 40, pid
    p2 = json.loads((INTERIM / "P2" / "summary.json").read_text(encoding="utf-8"))["docs"]
    drawing = next(d for d in p2 if d["doc_type"] == "drawing")
    assert drawing["needs_ocr_pages"] == drawing["pages"]  # P2 도면은 전부 스캔 이미지
