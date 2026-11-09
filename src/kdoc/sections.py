"""공정 짝짓기 지원: 시방서 장·내역서 공종이 '어떤 표현으로 어디에' 있는지 보여주고,
사용자가 짝을 지정하는 파일을 만든다.

- data/review/{P}/공정목록_{P}.xlsx : 매번 새로 만든다 (위치·하이퍼링크·짝 지정 여부)
- config/pairs/{P}.csv              : 사용자 소유. 없을 때 한 번만 만들고 절대 덮어쓰지 않는다.
                                      처음엔 이름이 완전히 같은 짝만 '자동(이름 같음)'으로 채운다.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.hyperlink import Hyperlink

from . import CONFIG, DATA, INTERIM, RAW

REVIEW = DATA / "review"
PAIRS = CONFIG / "pairs"
PAIR_FIELDS = ["spec_doc", "spec_chapter_no", "spec_chapter", "boq_doc", "boq_section_code", "boq_section", "note"]
AUTO_NOTE = "자동(이름 같음)"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name or "")


def doc_files(pid: str) -> dict[str, Path]:
    """doc_id(P1-02) → 원본 파일의 절대 경로."""
    manifest = json.loads((RAW / pid / "manifest.json").read_text(encoding="utf-8"))
    return {f"{pid}-{f['seq']:02d}": (RAW / pid / f["file"]).resolve() for f in manifest["files"] if "file" in f}


def spec_chapters(pid: str) -> list[dict]:
    return group_chapters(_read_jsonl(INTERIM / pid / "spec_clauses.jsonl"))


def boq_sections(pid: str) -> list[dict]:
    return group_sections(_read_jsonl(INTERIM / pid / "boq_items.jsonl"))


def group_chapters(clauses: list[dict]) -> list[dict]:
    """시방서 장 목록. 장 구분 전 머리말(장 이름 없음)은 제외한다."""
    chapters: dict[tuple, dict] = {}
    for c in clauses:
        if not c["chapter"]:
            continue
        key = (c["doc_id"], c["chapter_no"], c["chapter"])
        ch = chapters.setdefault(key, {"doc_id": c["doc_id"], "chapter_no": c["chapter_no"],
                                       "chapter": c["chapter"], "page": c["page_start"], "clauses": 0})
        ch["page"] = min(ch["page"], c["page_start"])
        ch["clauses"] += 1
    return sorted(chapters.values(), key=lambda x: (x["doc_id"], x["page"]))


def group_sections(items: list[dict]) -> list[dict]:
    """내역서 공종 목록. 위치는 그 공종의 첫 항목 행."""
    sections: dict[tuple, dict] = {}
    for i in items:
        key = (i["doc_id"], i["section_code"])
        s = sections.setdefault(key, {"doc_id": i["doc_id"], "section_code": i["section_code"],
                                      "section": i["section"] or "", "path": " > ".join(i["section_path"] or []),
                                      "sheet": i["sheet"], "row": i["row"], "items": 0})
        if i["row"] < s["row"]:
            s["sheet"], s["row"] = i["sheet"], i["row"]
        s["items"] += 1
    return sorted(sections.values(), key=lambda x: (x["doc_id"], x["sheet"], x["row"]))


def suggest_pairs(chapters: list[dict], sections: list[dict]) -> list[dict]:
    """이름(공백 무시)이 완전히 같은 장·공종만 짝으로 제안한다. 나머지는 사용자가 지정."""
    by_name: dict[str, list[dict]] = {}
    for s in sections:
        by_name.setdefault(_norm(s["section"]), []).append(s)
    return [
        {"spec_doc": c["doc_id"], "spec_chapter_no": c["chapter_no"] or "", "spec_chapter": c["chapter"],
         "boq_doc": s["doc_id"], "boq_section_code": s["section_code"], "boq_section": s["section"], "note": AUTO_NOTE}
        for c in chapters for s in by_name.get(_norm(c["chapter"]), [])
    ]


def read_pairs(path: Path) -> list[dict]:
    """엑셀에서 '다른 이름으로 저장 → CSV'를 하면 cp949로 저장될 수 있어 둘 다 받는다."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path}: UTF-8/CP949 어느 쪽으로도 읽을 수 없음")
    reader = csv.DictReader(text.splitlines(), strict=True)
    if not set(PAIR_FIELDS[:-1]).issubset(reader.fieldnames or []):
        raise ValueError("공정 짝 CSV 필수 열이 없습니다")
    rows = []
    for row in reader:
        if None in row:
            raise ValueError(f"공정 짝 CSV {reader.line_num}행의 열이 너무 많습니다")
        fields = {k: (row.get(k) or "").strip() for k in PAIR_FIELDS}
        if any(fields.values()):
            rows.append(fields)
    return rows


def ensure_pairs_file(pid: str, chapters: list[dict], sections: list[dict]) -> tuple[Path, bool]:
    """짝 지정 파일이 없을 때만 만든다. (경로, 새로 만들었는지)"""
    path = PAIRS / f"{pid}.csv"
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # BOM: 엑셀에서 한글이 안 깨지게
        w = csv.DictWriter(f, fieldnames=PAIR_FIELDS)
        w.writeheader()
        w.writerows(suggest_pairs(chapters, sections))
    return path, True


def _link(cell, target: Path, location: str | None = None, page: int | None = None) -> None:
    """PDF는 file:///...#page=N (브라우저로 열면 그 쪽으로), 엑셀은 시트·셀 위치까지."""
    if page:
        cell.hyperlink = f"{target.as_uri()}#page={page}"
    else:
        cell.hyperlink = Hyperlink(ref=cell.coordinate, target=str(target), location=location)
    cell.font = Font(color="0563C1", underline="single")


def _autowidth(ws) -> None:
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(8, width * 1.6), 80)


def write_listing(pid: str) -> dict:
    files = doc_files(pid)
    chapters, sections = spec_chapters(pid), boq_sections(pid)
    pairs_path, created = ensure_pairs_file(pid, chapters, sections)
    pairs = read_pairs(pairs_path)

    paired_ch = {(p["spec_doc"], p["spec_chapter"]) for p in pairs}
    paired_sec = {(p["boq_doc"], p["boq_section_code"]) for p in pairs}

    wb = Workbook()
    ws = wb.active
    ws.title = "시방서 장"
    ws.append(["짝 지정", "문서", "장 번호", "장 이름", "시작 쪽", "조항 수", "열기", "파일 경로"])
    for c in chapters:
        path = files[c["doc_id"]]
        ws.append(["O" if (c["doc_id"], c["chapter"]) in paired_ch else "미지정", c["doc_id"],
                   c["chapter_no"], c["chapter"], c["page"], c["clauses"], f"{c['page']}쪽 열기", str(path)])
        _link(ws.cell(ws.max_row, 7), path, page=c["page"])

    ws = wb.create_sheet("내역서 공종")
    ws.append(["짝 지정", "문서", "공종 코드", "공종 이름", "공종 경로", "시트", "첫 행", "항목 수", "열기", "파일 경로"])
    for s in sections:
        path = files[s["doc_id"]]
        ws.append(["O" if (s["doc_id"], s["section_code"]) in paired_sec else "미지정", s["doc_id"],
                   s["section_code"], s["section"], s["path"], s["sheet"], s["row"], s["items"],
                   f"{s['sheet']} {s['row']}행 열기", str(path)])
        _link(ws.cell(ws.max_row, 9), path, location=f"'{s['sheet']}'!A{s['row']}")

    ws = wb.create_sheet("안내")
    for line in [
        f"짝 지정 파일: {pairs_path.resolve()}",
        "이 파일을 엑셀로 열어 시방서 장 ↔ 내역서 공종 짝을 한 줄에 하나씩 적는다 (2:1, 1:2 짝은 여러 줄).",
        f"처음 만들 때 이름이 완전히 같은 짝만 note='{AUTO_NOTE}'로 채워 두었다. 틀리면 지우면 된다.",
        "짝 지정 파일은 다시 실행해도 덮어쓰지 않는다. 이 목록 파일(공정목록)은 매번 새로 만든다.",
        "'열기' 링크: PDF는 브라우저(Edge·Chrome)로 열면 해당 쪽으로 이동한다. 다른 뷰어는 첫 쪽을 열 수 있으니 '시작 쪽' 숫자를 함께 본다.",
    ]:
        ws.append([line])

    for sheet in wb.worksheets[:2]:
        sheet.freeze_panes = "A2"
        _autowidth(sheet)
    wb["안내"].column_dimensions["A"].width = 120

    out_dir = REVIEW / pid
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"공정목록_{pid}.xlsx"
    wb.save(out)
    return {
        "project": pid, "listing": str(out.resolve()), "pairs_file": str(pairs_path.resolve()),
        "pairs_file_created": created, "pairs": len(pairs),
        "spec_chapters": len(chapters), "spec_unpaired": sum((c["doc_id"], c["chapter"]) not in paired_ch for c in chapters),
        "boq_sections": len(sections), "boq_unpaired": sum((s["doc_id"], s["section_code"]) not in paired_sec for s in sections),
    }
