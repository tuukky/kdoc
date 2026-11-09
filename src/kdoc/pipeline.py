"""manifest.json 기준으로 프로젝트 문서를 전부 추출해 data/interim/{P}/ 에 JSONL로 저장."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import INTERIM, RAW
from .extract import boq, drawing, pdf, spec


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def extract_file(path: Path, meta: dict) -> tuple[dict, dict[str, list[dict]]]:
    """문서 하나 추출. meta = {doc_id, doc_type, discipline}. 실패해도 예외 대신 info["error"].

    반환: (info, {"pages":[], "clauses":[], "spans":[], "items":[]}) — 각 행에 meta가 붙어 있다.
    """
    doc_type = meta["doc_type"]
    out: dict[str, list[dict]] = {"pages": [], "clauses": [], "spans": [], "items": []}
    info = {**meta, "file": path.name}
    t0 = time.perf_counter()
    try:
        if path.suffix.lower() == ".pdf":
            pg = pdf.read_pages(path)
            out["pages"] = [{**meta, **p} for p in pg]
            info["pages"] = len(pg)
            info["needs_ocr_pages"] = sum(p["needs_ocr"] for p in pg)
            if doc_type == "spec":
                cl = spec.split_clauses(pg)
                out["clauses"] = [{**meta, **c} for c in cl]
                info["clauses"] = len(cl)
            elif doc_type == "drawing":
                sp = drawing.read_spans(path)
                out["spans"] = [{**meta, **s} for s in sp]
                info["spans"] = len(sp)
                info["spans_non_boilerplate"] = sum(not s["boilerplate"] for s in sp)
        elif doc_type == "boq" and path.suffix.lower() in (".xlsx", ".xls"):
            it = boq.read_boq(path)
            out["items"] = [{**meta, **i} for i in it]
            info["boq_items"] = len(it)
            info["boq_sections"] = len({i["section_code"] for i in it})
        else:
            info["skipped"] = "지원하지 않는 형식"
    except Exception as e:  # 한 문서 실패가 전체를 멈추지 않게
        info["error"] = f"{type(e).__name__}: {e}"
    info["seconds"] = round(time.perf_counter() - t0, 2)
    return info, out


def write_interim(out_dir: Path, rows: dict[str, list[dict]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, name in [("pages", "pages"), ("clauses", "spec_clauses"), ("spans", "drawing_spans"), ("items", "boq_items")]:
        _write_jsonl(out_dir / f"{name}.jsonl", rows[key])


def extract_project(pid: str) -> dict:
    raw_dir = RAW / pid
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    out_dir = INTERIM / pid

    rows: dict[str, list[dict]] = {"pages": [], "clauses": [], "spans": [], "items": []}
    docs = []
    for f in manifest["files"]:
        if "error" in f:
            continue
        meta = {"doc_id": f"{pid}-{f['seq']:02d}", "doc_type": f["doc_type"], "discipline": f.get("discipline")}
        info, out = extract_file(raw_dir / f["file"], meta)
        for k in rows:
            rows[k] += out[k]
        docs.append(info)

    write_interim(out_dir, rows)
    summary = {"project": pid, "name": manifest["project"]["name"], "docs": docs}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def stats(pid: str) -> dict:
    d = INTERIM / pid
    clauses = _read_jsonl(d / "spec_clauses.jsonl")
    items = _read_jsonl(d / "boq_items.jsonl")
    chapters = sorted({(c["doc_id"], c["chapter_no"] or 0, c["chapter"]) for c in clauses})
    sections = sorted({(i["doc_id"], i["section_code"] or "", i["section"] or "") for i in items})
    return {
        "project": pid,
        "spec_chapters": [f"{a} 제{b}장 {c}" for a, b, c in chapters],
        "boq_sections": [f"{a} {b} {c} ({sum(1 for i in items if i['section_code'] == b and i['doc_id'] == a)}개)" for a, b, c in sections],
    }
