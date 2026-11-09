"""양방향 자재·수치 비교 규칙 (결과를 보기 전에 정한 기준을 고정)."""

import pytest

from kdoc import compare as C


@pytest.fixture
def terms(tmp_path):
    p = tmp_path / "m.txt"
    p.write_text("# 설명\n우레탄 방수: 우레탄\n시멘트 액체방수: 액체방수\n방수석고보드\n석고보드\n시멘트 모르타르: 몰탈\n",
                 encoding="utf-8")
    return C.load_materials(p)


def test_values_need_a_unit_and_thk_t_are_mm():
    assert C.find_values("바닥THK3MM, 수직:THK 2mm, 9.5T, 2회, 1.5cm, 3겹, 24MPa") == [
        ("mm", 3.0), ("mm", 2.0), ("mm", 15.0), ("mm", 9.5), ("MPa", 24.0), ("회", 2.0), ("겹", 3.0)]
    assert C.find_values("KS F 4909, pH7∼9, 함수율 6%") == []


def test_materials_ignore_spaces_and_prefer_longer_names(terms):
    assert C.find_materials("방수 석고보드 위 우레탄 노출형", terms) == ["방수석고보드", "우레탄 방수"]
    assert C.find_materials("석고보드 9.5T", terms) == ["석고보드"]


def _cl(chapter, text, page=24):
    return {"doc_id": "S01", "chapter_no": 6, "chapter": chapter, "num": "1", "heading": "",
            "text": text, "page_start": page, "page_end": page}


def _it(section, name, spec, row):
    return {"doc_id": "B01", "section_code": "0106", "section": section, "name": name, "spec": spec,
            "sheet": "내역", "row": row}


def test_both_directions_and_value_diff(terms):
    clauses = [_cl("방수공사", "우레탄 노출형 방수\n바닥 THK3MM\n시멘트 몰탈 2회 바름")]
    items = [_it("방수공사", "시멘트 액체방수 바름", "바닥", 127), _it("방수공사", "몰탈 바름", "3회", 128)]
    pairs = [{"spec_key": ("S01", 6, "방수공사"), "boq_key": ("B01", "0106")}]
    got = {(f["kind"], f["material"]): f for f in C.compare(pairs, clauses, items, terms)}

    assert set(got) == {(C.KIND_NOT_IN_BOQ, "우레탄 방수"), (C.KIND_NOT_IN_SPEC, "시멘트 액체방수"),
                        (C.KIND_VALUE_DIFF, "시멘트 모르타르")}
    assert got[(C.KIND_NOT_IN_BOQ, "우레탄 방수")]["boq_first"]["row"] == 127   # 짝 공종 첫 행으로 안내
    assert got[(C.KIND_NOT_IN_SPEC, "시멘트 액체방수")]["boq"]["item"]["row"] == 127
    diff = got[(C.KIND_VALUE_DIFF, "시멘트 모르타르")]
    assert (diff["spec_value"], diff["boq_value"]) == ("2회", "3회")


def test_number_on_next_line_attaches_to_material_above(terms):
    facts = C.spec_facts([_cl("방수공사", "우레탄 노출형 방수\n바닥 THK3MM\n가\n나\n다 5mm")], terms)
    assert [(f["material"], f["values"]) for f in facts] == [("우레탄 방수", []), ("우레탄 방수", [("mm", 3.0)])]


def test_same_values_and_unpaired_sections_give_nothing(terms):
    clauses = [_cl("방수공사", "시멘트 몰탈 2회")]
    items = [_it("방수공사", "몰탈", "2회", 5), {**_it("기타", "석고보드", "", 9), "section_code": "9999"}]
    pairs = [{"spec_key": ("S01", 6, "방수공사"), "boq_key": ("B01", "0106")}]
    assert C.compare(pairs, clauses, items, terms) == []


@pytest.mark.parametrize(("spec", "items", "expected", "agreed"), [
    ("석고보드 벽체 30mm\n석고보드 천장 50mm", [("벽체 30mm", 10), ("천장 40mm", 11)],
     [(C.KIND_VALUE_DIFF, "50mm", "40mm", 11)], 1),
    ("석고보드 30mm\n석고보드 50mm", [("30mm", 10)],
     [(C.KIND_UNSURE, "30mm, 50mm", "30mm", 10)], 0),
    ("석고보드 벽체 50mm", [("천장 30mm", 10)],
     [(C.KIND_UNSURE, "50mm", "30mm", 10)], 0),
    ("석고보드 30mm 이상", [("20mm", 10)],
     [(C.KIND_UNSURE, "30mm", "20mm", 10)], 0),
    ("석고보드 30mm~50mm", [("40mm", 10)],
     [(C.KIND_UNSURE, "30mm, 50mm", "40mm", 10)], 0),
    ("석고보드\n벽체 30mm", [("벽체 3cm", 10)], [], 1),
])
def test_numeric_evidence_fixed_cases(terms, spec, items, expected, agreed):
    pairs = [{"spec_key": ("S01", 6, "수장공사"), "boq_key": ("B01", "0106")}]
    coverage = {}
    actual = C.compare(pairs, [_cl("수장공사", spec)],
                       [_it("수장공사", "석고보드", value, row) for value, row in items], terms, coverage=coverage)
    assert [(f["kind"], f["spec_value"], f["boq_value"], f["boq"]["item"]["row"]) for f in actual] == expected
    assert coverage["수치 일치"] == agreed
