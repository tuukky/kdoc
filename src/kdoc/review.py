"""검토 실행: 사용자가 고른 파일 → 추출 → 공정 짝(규칙+AI) → 양방향 자재·수치 비교 → 결과 엑셀.

화면 프로그램(gui.py)과 CLI(`python -m kdoc review`)가 같이 쓴다. manifest·sources.yaml 이 필요 없다.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from . import compare, pairing, pipeline
from .sections import _autowidth, _link, group_chapters, group_sections, read_pairs

DOC_TYPES = [("spec", "시방서", "S"), ("boq", "내역서", "B"), ("drawing", "도면", "D")]
DISCLAIMER = ("이 결과는 문서 사이의 '의심 위치'를 보여주는 검토 보조 자료입니다. 오류를 확정하지 않으며, "
              "수정 여부는 설계자·협력업체 확인 후 판단하십시오.")

Progress = Callable[[str, float], None]


def _noop(msg: str, frac: float) -> None:
    pass


class ProjectNotReady(ValueError):
    """등록된 프로젝트의 입력을 아직 준비하지 못했다."""


class ReviewProcessingError(ValueError):
    """입력이 있어도 비교할 수 있는 문서 데이터를 추출하지 못했다."""


def project_inputs(raw: Path, project: str) -> tuple[dict, dict]:
    folder = (raw / project).resolve()
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectNotReady("먼저 fetch로 프로젝트 자료를 준비하십시오.") from exc
    files: dict[str, list[Path]] = {t: [] for t, _, _ in DOC_TYPES}
    doc_ids: dict[Path, str] = {}
    for entry in manifest["files"]:
        doc_type = entry["doc_type"]
        if doc_type not in files:
            continue
        if not entry.get("file") or entry.get("error"):
            raise ProjectNotReady("다운로드되지 않은 프로젝트 문서가 있습니다.")
        path = (folder / entry["file"]).resolve()
        doc_id = f"{project}-{entry['seq']:02d}"
        if not path.is_relative_to(folder) or path in doc_ids or doc_id in doc_ids.values():
            raise ValueError("프로젝트 자료 목록에 잘못된 경로 또는 중복 문서가 있습니다.")
        if not path.is_file():
            raise ProjectNotReady("프로젝트 원본 파일이 없습니다. fetch 결과를 확인하십시오.")
        files[doc_type].append(path)
        doc_ids[path] = doc_id
    if not files["spec"] or not files["boq"]:
        raise ProjectNotReady("시방서와 내역서를 한 개 이상씩 준비하십시오.")
    return files, doc_ids


def extract_inputs(files: dict[str, list[Path]], progress: Progress = _noop, *, doc_ids=None) -> dict:
    """files = {"spec": [...], "boq": [...], "drawing": [...]} → 추출 결과 + doc_id → 경로."""
    rows: dict[str, list[dict]] = {"pages": [], "clauses": [], "spans": [], "items": []}
    docs, paths = [], {}
    todo = [(t, label, prefix, Path(p)) for t, label, prefix in DOC_TYPES for p in files.get(t, [])]
    counters = {t: 0 for t, _, _ in DOC_TYPES}
    for n, (t, label, prefix, path) in enumerate(todo):
        counters[t] += 1
        doc_id = (doc_ids or {}).get(path.resolve(), f"{prefix}{counters[t]:02d}")
        progress(f"추출 중 ({n + 1}/{len(todo)}): {label} {path.name}", n / max(len(todo), 1))
        info, out = pipeline.extract_file(path, {"doc_id": doc_id, "doc_type": t, "discipline": None})
        info["path"] = str(path.resolve())
        if not (info.get("error") or info.get("skipped")):
            if t == "spec" and not any(c.get("chapter") for c in out["clauses"]):
                info["skipped"] = "비교할 시방서 장을 추출하지 못함"
            elif t == "boq" and not out["items"]:
                info["skipped"] = "비교할 내역서 행을 추출하지 못함"
        docs.append(info)
        paths[doc_id] = path.resolve()
        for k in rows:
            rows[k] += out[k]
    return {"rows": rows, "docs": docs, "paths": paths}


def run_review(files: dict[str, list[Path]], out_dir: Path, api_key: str | None = None,
               progress: Progress = _noop, client=None, cache: Path | None = None,
               materials: Path | None = None, *, doc_ids=None, pairs_file: Path | None = None,
               strict_inputs: bool = False) -> dict:
    t0 = time.perf_counter()
    if not files.get("spec") or not files.get("boq"):
        raise ValueError("시방서와 내역서를 한 개 이상씩 넣어야 비교할 수 있습니다.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(out_dir)
    run_dir = out_dir / f"검토_{stamp}_{uuid4().hex[:12]}"

    ex = extract_inputs(files, lambda m, f: progress(m, f * 0.6), doc_ids=doc_ids)
    rows, paths = ex["rows"], ex["paths"]
    chapters, sections = group_chapters(rows["clauses"]), group_sections(rows["items"])
    if strict_inputs and (not chapters or not sections or any(
        d["doc_type"] in ("spec", "boq") and (d.get("error") or d.get("skipped")) for d in ex["docs"]
    )):
        raise ReviewProcessingError("시방서 또는 내역서에서 비교할 데이터를 추출하지 못했습니다.")
    pipeline.write_interim(run_dir / "추출데이터", rows)

    progress("공정 짝짓기 중 (사용자 지정, 규칙, 선택적 AI)", 0.65)
    manual, warnings = [], []
    if pairs_file:
        try:
            manual = read_pairs(pairs_file)
        except FileNotFoundError:
            pass
        except (ValueError, csv.Error, OSError) as exc:
            warnings.append(f"공정 짝 CSV를 읽지 못해 자동 짝짓기를 적용: {exc}")
    pr = pairing.pair(chapters, sections, api_key=api_key, client=client, cache=cache, manual=manual)

    progress("자재·수치 비교 중", 0.8)
    terms = compare.load_materials(materials)
    coverage = {}
    findings = compare.compare(pr["pairs"], rows["clauses"], rows["items"], terms, coverage=coverage)

    progress("결과 엑셀 저장 중", 0.9)
    page_text = {(p["doc_id"], p["page"]): compact_page(p["text"]) for p in rows["pages"]}
    table = {
        "pairs": pair_rows(pr, paths),
        "spec_to_boq": [finding_row(f, paths, page_text) for f in findings if f["kind"] != compare.KIND_NOT_IN_SPEC],
        "boq_to_spec": [finding_row(f, paths, page_text) for f in findings if f["kind"] == compare.KIND_NOT_IN_SPEC],
    }
    summary = {
        "시작": stamp, "소요(초)": round(time.perf_counter() - t0, 1),
        "문서": ex["docs"], "시방서 장": len(chapters), "내역서 공종": len(sections),
        "짝": sum(r["판정"] == pairing.PAIRED for r in table["pairs"]),
        "판단 불가": sum(r["판정"] == pairing.UNSURE for r in table["pairs"]),
        "없음": sum(r["판정"] == pairing.NONE for r in table["pairs"]),
        "내역서에 없음": sum(f["kind"] == compare.KIND_NOT_IN_BOQ for f in findings),
        "시방서에 없음": sum(f["kind"] == compare.KIND_NOT_IN_SPEC for f in findings),
        "수치 다름": sum(f["kind"] == compare.KIND_VALUE_DIFF for f in findings),
        "수치 판단 불가": sum(f["kind"] == compare.KIND_UNSURE for f in findings),
        "처리 불가 문서": sum(bool(d.get("error") or d.get("skipped")) for d in ex["docs"]),
        "비교 범위": coverage,
        "공정 짝 경고": [*warnings, *pr["warnings"]],
        "AI": pr["ai"], "자재 사전": str((materials or compare.materials_path()).resolve()), "자재 수": len({n for _, n in terms}),
    }
    excel = write_excel(run_dir / f"검토결과_{stamp}.xlsx", summary, table)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    progress("완료", 1.0)
    return {"excel": str(excel.resolve()), "dir": str(run_dir.resolve()), "summary": summary, **table}


def compact_page(text: str) -> str:
    return "".join((text or "").split())


def _find_page(doc_id: str, line: str, start: int, end: int, page_text: dict) -> int:
    """조항이 여러 쪽에 걸치면 그 줄이 실제로 있는 쪽을 찾는다. 못 찾으면 조항 첫 쪽."""
    key = compact_page(line)[:30]
    for pg in range(start, end + 1):
        if key and key in page_text.get((doc_id, pg), ""):
            return pg
    return start


# ── 표 한 줄 = 화면 표 한 줄 = 엑셀 한 줄. '_' 로 시작하는 키는 링크용(화면·엑셀에 안 보임) ──

def _spec_loc(paths, doc_id, page) -> dict:
    return {"시방서 파일": paths[doc_id].name, "시방서 쪽": page, "_spec_path": str(paths[doc_id]), "_spec_page": page}


def _boq_loc(paths, doc_id, sheet, row) -> dict:
    return {"내역서 파일": paths[doc_id].name, "내역서 위치": f"{sheet} {row}행",
            "_boq_path": str(paths[doc_id]), "_boq_sheet": sheet, "_boq_row": row}


def _empty_spec() -> dict:
    return {"시방서 파일": "", "시방서 쪽": "", "_spec_path": None, "_spec_page": None}


def _empty_boq() -> dict:
    return {"내역서 파일": "", "내역서 위치": "", "_boq_path": None, "_boq_sheet": None, "_boq_row": None}


def pair_rows(pr: dict, paths: dict) -> list[dict]:
    out = []
    for r in pr["spec"]:
        c = r["chapter"]
        names = ", ".join(f"{s['section_code']} {s['section']}" for s in r["boq"])
        first = min(r["boq"], key=lambda s: (s["doc_id"], s["sheet"], s["row"])) if r["boq"] else None
        out.append({"기준": "시방서 장", "시방서 장": f"{c['chapter_no'] or ''}장 {c['chapter']}".strip(),
                    "내역서 공종": names, "판정": r["status"], "근거": r["reason"],
                    **_spec_loc(paths, c["doc_id"], c["page"]),
                    **(_boq_loc(paths, first["doc_id"], first["sheet"], first["row"]) if first else _empty_boq())})
    for r in pr["boq"]:
        if r["status"] == pairing.PAIRED:
            continue  # 시방서 쪽 줄에 이미 나옴
        s = r["section"]
        out.append({"기준": "내역서 공종", "시방서 장": "", "내역서 공종": f"{s['section_code']} {s['section']}",
                    "판정": r["status"], "근거": r["reason"], **_empty_spec(),
                    **_boq_loc(paths, s["doc_id"], s["sheet"], s["row"])})
    return out


def finding_row(f: dict, paths: dict, page_text: dict) -> dict:
    row = {"종류": f["kind"], "자재": f["material"], "시방서 내용": "", "내역서 내용": "",
           "시방서 수치": f.get("spec_value", ""), "내역서 수치": f.get("boq_value", ""),
           "판정 근거": f.get("reason", "짝 공정에서 자재 사전과 일치하는 표현을 찾지 못함")}
    sf = f.get("spec")
    if sf:
        c = sf["clause"]
        pg = _find_page(c["doc_id"], sf["line"], c["page_start"], c["page_end"], page_text)
        row["시방서 내용"] = f"[{c['chapter']} {c.get('num') or ''} {c.get('heading') or ''}] {sf['line']}"
        row.update(_spec_loc(paths, c["doc_id"], pg))
    elif f.get("spec_first"):
        c = f["spec_first"]
        row["시방서 내용"] = f"(짝 장 [{c['chapter']}] 에 이 자재 언급 없음)"
        row.update(_spec_loc(paths, c["doc_id"], c["page_start"]))
    else:
        row.update(_empty_spec())
    bf = f.get("boq")
    if bf:
        it = bf["item"]
        row["내역서 내용"] = f"[{it['section']}] {bf['line']}"
        row.update(_boq_loc(paths, it["doc_id"], it["sheet"], it["row"]))
    elif f.get("boq_first"):
        it = f["boq_first"]
        row["내역서 내용"] = f"(짝 공종 [{it['section']}] 에 이 자재 없음)"
        row.update(_boq_loc(paths, it["doc_id"], it["sheet"], it["row"]))
    else:
        row.update(_empty_boq())
    return row


PAIR_COLS = ["기준", "시방서 장", "내역서 공종", "판정", "근거", "시방서 파일", "시방서 쪽", "내역서 파일", "내역서 위치"]
FINDING_COLS = ["종류", "자재", "시방서 수치", "내역서 수치", "시방서 내용", "내역서 내용",
                "시방서 파일", "시방서 쪽", "내역서 파일", "내역서 위치", "판정 근거"]
_FILL = {pairing.UNSURE: "FFF2CC", pairing.NONE: "F8CBAD", compare.KIND_VALUE_DIFF: "F8CBAD"}


def _sheet(wb, title: str, cols: list[str], rows: list[dict], color_key: str) -> None:
    ws = wb.create_sheet(title)
    ws.append(cols + ["시방서 열기", "내역서 열기", "시방서 전체 경로", "내역서 전체 경로"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r.get(c, "") for c in cols] + ["", "", r["_spec_path"] or "", r["_boq_path"] or ""])
        n = ws.max_row
        if r["_spec_path"]:
            ws.cell(n, len(cols) + 1, f"{r['_spec_page']}쪽 열기")
            _link(ws.cell(n, len(cols) + 1), Path(r["_spec_path"]), page=r["_spec_page"])
        if r["_boq_path"]:
            ws.cell(n, len(cols) + 2, f"{r['_boq_row']}행 열기")
            sheet = r["_boq_sheet"].replace("'", "''")
            _link(ws.cell(n, len(cols) + 2), Path(r["_boq_path"]), location=f"'{sheet}'!A{r['_boq_row']}")
        color = _FILL.get(r.get(color_key))
        if color:
            for c in ws[n][: len(cols)]:
                c.fill = PatternFill("solid", fgColor=color)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _autowidth(ws)
    for col in ("E", "F"):  # 긴 문장 열
        ws.column_dimensions[col].width = min(ws.column_dimensions[col].width, 60)


def write_excel(path: Path, summary: dict, table: dict) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    ai = summary["AI"]
    ai_text = (f"{ai['model']} 사용 (호출 {ai['calls']}회{', 캐시 사용' if ai['cached'] else ''})" if ai["used"]
               else f"사용 안 함 — {ai['error'] or '판단할 공정 없음'}")
    lines = [
        ("kdoc 설계도서 검토 결과", ""), ("", ""),
        ("실행 시각", summary["시작"]), ("소요 시간(초)", summary["소요(초)"]),
        ("공정 짝 AI", ai_text), ("", ""),
        ("시방서 장", summary["시방서 장"]), ("내역서 공종", summary["내역서 공종"]),
        ("  짝", summary["짝"]), ("  판단 불가", summary["판단 불가"]), ("  없음", summary["없음"]), ("", ""),
        ("시방서→내역서: 내역서에 없음", summary["내역서에 없음"]),
        ("시방서→내역서: 수치 다름", summary["수치 다름"]),
        ("내역서→시방서: 시방서에 없음", summary["시방서에 없음"]), ("", ""),
        ("수치 판단 불가", summary["수치 판단 불가"]), ("처리 불가 문서", summary["처리 불가 문서"]),
        *summary["비교 범위"].items(),
        *[("공정 짝 경고", warning) for warning in summary["공정 짝 경고"]], ("", ""),
        ("넣은 파일", ""),
    ]
    for k, v in lines:
        ws.append([k, v])
    for d in summary["문서"]:
        state = d.get("error") or d.get("skipped") or "추출 완료"
        ws.append([f"  {d['doc_id']} ({dict((t, l) for t, l, _ in DOC_TYPES)[d['doc_type']]})", d["path"], state])
    ws["A1"].font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 90

    _sheet(wb, "공정 짝", PAIR_COLS, table["pairs"], "판정")
    _sheet(wb, "시방서→내역서", FINDING_COLS, table["spec_to_boq"], "종류")
    _sheet(wb, "내역서→시방서", FINDING_COLS, table["boq_to_spec"], "종류")

    ws = wb.create_sheet("안내")
    for line in [
        DISCLAIMER, "",
        "[공정 짝] 사용자 지정 CSV를 우선 적용합니다. 미지정 장은 이름 일치 규칙과 선택적 AI를 사용합니다.",
        "  AI에는 장·공종 '이름'만 보냅니다. 문서 본문은 외부로 보내지 않습니다.",
        "[시방서→내역서] 시방서 기준. 짝 장에 나온 자재가 짝 공종에 없거나(내역서에 없음), 같은 자재의 두께·횟수가 다른 곳(수치 다름).",
        "[내역서→시방서] 내역서 기준. 짝 공종의 자재가 짝 장에 나오지 않는 곳(시방서에 없음).",
        "  짝이 '판단 불가'·'없음'인 공정은 자재 비교를 하지 않습니다 → '공정 짝' 시트에서 확인.",
        f"[자재 사전] {summary['자재 사전']} 에 있는 자재({summary['자재 수']}종)만 찾습니다. 없는 자재는 놓칠 수 있으니 사전을 보완하십시오.",
        "[수치] 숫자와 단위(mm·회·겹·MPa, THK·T 표기 포함)가 함께 있을 때만 비교합니다.",
        "  부위·조건이 다르거나 단일 수치 쌍을 정할 수 없으면 '판단 불가'입니다. 이때 링크는 확인할 후보 위치입니다.",
        "  수치 비교 건수는 단일 수치로 비교한 조합 수입니다. 판단 불가 건수와 미짝 공정은 별도로 봅니다.",
        "[열기 링크] PDF는 브라우저(Edge·Chrome)로 열면 해당 쪽으로 이동합니다. 엑셀은 해당 시트·행으로 이동합니다.",
        "[도면] 이번 버전은 도면 문자만 추출하고 비교에는 쓰지 않습니다.",
    ]:
        ws.append([line])
    ws.column_dimensions["A"].width = 140
    for c in ws["A"]:
        c.alignment = Alignment(wrap_text=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
