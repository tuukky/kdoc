"""kdoc 검토 API — n8n 워크플로(W1~W5)가 부르는 곳.

    uvicorn kdoc.api:app --host 0.0.0.0 --port 8000

  GET  /health
  POST /extract                     등록 프로젝트 추출 (v1)
  POST /reviews                     등록·자동 수집 프로젝트 검토 → 보고서 UUID
  POST /reviews/upload              시방서·내역서 파일을 올려 검토 (W2)
  GET  /reviews/{id}/report         결과 엑셀
  GET  /reviews/{id}/top?n=10       의심 항목 상위 N개, 본문 없음 (W5)
  POST /reviews/{id}/judgments      판정 열을 채운 결과 엑셀 업로드 (W3)
  GET  /metrics/precision           판정 집계·정밀도
  POST /bids                        나라장터 공고 1건 등록: 첨부 확인·분류 (W1)
  POST /bids/{project}/fetch        검토에 쓸 첨부 다운로드 (W1)
  POST /ops/events  GET /ops/stats  실패 기록·운영 통계 (W4)
  POST /alerts  GET /alerts  POST /alerts/{id}/read  POST /alerts/read-all  GET /ui/alerts  알림함
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import requests
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import RAW, fetch, g2b, ops, pipeline, review
from .sections import PAIRS, REVIEW

app = FastAPI(title="kdoc", version="0.3.0")
logger = logging.getLogger(__name__)
API_REPORTS = REVIEW / "api"
REPORT_FILENAME = "report.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
UI = Path(__file__).parent / "ui"
UPLOAD_PROJECT = "UPLOAD"
SPEC_EXT, BOQ_EXT, DRAWING_EXT = (".pdf",), (".xls", ".xlsx"), (".pdf",)


def _max_upload() -> int:
    return int(os.environ.get("KDOC_UPLOAD_MAX_MB", "50")) * 1024 * 1024


class ExtractRequest(BaseModel):
    project: str | None = None  # 생략하면 sources.yaml 의 전체 프로젝트


class ReviewRequest(BaseModel):
    project: str


class ReviewResponse(BaseModel):
    review_id: UUID
    project: str
    summary: dict
    report_url: str
    seconds: float


class BidRequest(BaseModel):
    bid_no: str = Field(pattern=r"^[A-Z0-9]{8,20}$")
    bid_ord: str = Field(default="000", pattern=r"^\d{3}$")
    name: str = ""
    org: str = ""
    price: int | None = None
    posted: str = ""
    daily_limit: int | None = Field(default=None, ge=1)  # 하루 새 공고 등록 상한 (PLAN_v2 §4.1)


class EventRequest(BaseModel):
    source: str
    code: str
    message: str = ""
    level: str = Field(default="error", pattern=r"^(error|warn|info)$")
    project: str | None = None
    data: dict | None = None


class AlertRequest(BaseModel):
    kind: str = "info"
    title: str
    body: str = ""
    link: str | None = None
    severity: str = Field(default="info", pattern=r"^(info|success|warn|error)$")
    project: str | None = None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/extract")
def extract(req: ExtractRequest) -> dict:
    # def(비동기 아님)로 두면 FastAPI가 스레드에서 실행해 추출 중에도 /health 가 응답한다.
    ids = [p["id"] for p in fetch.load_sources()]
    if req.project:
        if req.project not in ids:
            raise HTTPException(404, f"알 수 없는 프로젝트: {req.project} (가능: {', '.join(ids)})")
        ids = [req.project]

    missing = [pid for pid in ids if not (RAW / pid / "manifest.json").exists()]
    if missing:
        raise HTTPException(409, f"fetch 결과(manifest.json)가 없음: {', '.join(missing)} — 먼저 fetch를 실행")

    results = [pipeline.extract_project(pid) for pid in ids]
    errors = [
        {"project": r["project"], "doc_id": d["doc_id"], "error": d["error"]}
        for r in results for d in r["docs"] if "error" in d
    ]
    # 문서 하나가 실패해도 나머지는 추출된다. n8n은 ok/errors 로 성공·실패를 나눈다.
    return {"ok": not errors, "errors": errors, "results": results}


# ---------------------------------------------------------------- 검토
def _known(project: str) -> bool:
    if project in {p["id"] for p in fetch.load_sources()}:
        return True
    return project.startswith("G-") and ops.get_bid(project) is not None


def _run(project: str, source: str, files: dict, doc_ids: dict | None, pairs_file: Path | None,
         review_id: UUID, run_root: Path) -> ReviewResponse:
    try:
        # API 검토는 자동 재현이 가능하도록 사용자 지정/이름 일치만 사용한다 (AI 없음).
        result = review.run_review(files, run_root, doc_ids=doc_ids, pairs_file=pairs_file, strict_inputs=True)
        Path(result["excel"]).replace(run_root / REPORT_FILENAME)
    except review.ProjectNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    except review.ReviewProcessingError as exc:
        ops.save_run(str(review_id), project, source, None, False, {"error": str(exc)})
        ops.add_event("api", "추출 실패", str(exc), project=project)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("review failed: project=%s review_id=%s", project, review_id)
        ops.save_run(str(review_id), project, source, None, False, {"error": repr(exc)})
        ops.add_event("api", "검토 오류", repr(exc), project=project)
        raise HTTPException(500, "검토 처리에 실패했습니다.") from exc

    summary = {key: value for key, value in result["summary"].items() if key not in
               {"문서", "자재 사전", "공정 짝 경고"}}
    summary["공정 짝 경고 수"] = len(result["summary"]["공정 짝 경고"])
    seconds = result["summary"]["소요(초)"]
    ops.save_run(str(review_id), project, source, seconds, True, summary,
                 [*result["spec_to_boq"], *result["boq_to_spec"]])
    if project.startswith("G-"):
        ops.set_bid_status(project, "reviewed")
    return ReviewResponse(review_id=review_id, project=project, summary=summary,
                          report_url=f"/reviews/{review_id}/report", seconds=seconds)


@app.post("/reviews", status_code=201, response_model=ReviewResponse)
def create_review(req: ReviewRequest) -> ReviewResponse:
    if not _known(req.project):
        raise HTTPException(404, "등록되지 않은 프로젝트입니다.")
    try:
        files, doc_ids = review.project_inputs(RAW, req.project)
    except review.ProjectNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.exception("review inputs failed: project=%s", req.project)
        raise HTTPException(500, "검토 처리에 실패했습니다.") from exc
    review_id = uuid4()
    source = "g2b" if req.project.startswith("G-") else "registered"
    return _run(req.project, source, files, doc_ids, PAIRS / f"{req.project}.csv", review_id,
                API_REPORTS / str(review_id))


def _safe_name(name: str | None, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", Path(name or "").name).strip()
    return name or fallback


async def _save_uploads(uploads: list[UploadFile], exts: tuple, folder: Path, label: str) -> list[Path]:
    out = []
    for i, u in enumerate(uploads, 1):
        name = _safe_name(u.filename, f"{label}{i}{exts[0]}")
        if Path(name).suffix.lower() not in exts:
            raise HTTPException(422, f"{label} 파일 형식이 아닙니다: {name} (허용: {', '.join(exts)})")
        data = await u.read(_max_upload() + 1)
        if len(data) > _max_upload():
            raise HTTPException(413, f"파일이 너무 큽니다: {name} (상한 {_max_upload() // 1024 // 1024} MB)")
        if not data:
            raise HTTPException(422, f"빈 파일입니다: {name}")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{label}{i}_{name}"
        path.write_bytes(data)
        out.append(path)
    return out


@app.post("/reviews/upload", status_code=201, response_model=ReviewResponse)
async def upload_review(spec: list[UploadFile] = File(...), boq: list[UploadFile] = File(...),
                        drawing: list[UploadFile] = File(default=[])) -> ReviewResponse:
    review_id = uuid4()
    run_root = API_REPORTS / str(review_id)
    try:
        files = {
            "spec": await _save_uploads(spec, SPEC_EXT, run_root / "input", "시방서"),
            "boq": await _save_uploads(boq, BOQ_EXT, run_root / "input", "내역서"),
            "drawing": await _save_uploads(drawing, DRAWING_EXT, run_root / "input", "도면"),
        }
    except HTTPException:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
    # 추출은 오래 걸리므로 스레드에서 돌려 /health 가 계속 응답하게 한다.
    return await run_in_threadpool(_run, UPLOAD_PROJECT, "upload", files, None, None, review_id, run_root)


@app.get("/reviews/{review_id}/report", response_class=FileResponse,
         responses={200: {"content": {XLSX: {}}}, 404: {"description": "보고서 없음"}})
def download_report(review_id: UUID) -> FileResponse:
    path = API_REPORTS / str(review_id) / REPORT_FILENAME
    if not path.is_file():
        raise HTTPException(404, "검토 보고서가 없습니다.")
    return FileResponse(path, media_type=XLSX, filename=f"kdoc-{review_id}.xlsx")


@app.get("/reviews/{review_id}/top")
def top(review_id: UUID, n: int = Query(10, ge=1, le=50)) -> dict:
    run = ops.get_run(str(review_id))
    if not run:
        raise HTTPException(404, "검토 기록이 없습니다.")
    return {"review_id": str(review_id), "project": run["project"], "items": ops.top_findings(str(review_id), n)}


# ---------------------------------------------------------------- 판정 (W3)
JUDGE_SHEETS = ("시방서→내역서", "내역서→시방서")


def read_judgments(data: bytes) -> dict[str, str]:
    """판정 열이 채워진 결과 엑셀 → {항목ID: 판정}. 빈 칸·'—'는 건너뛴다."""
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(422, "엑셀 파일을 열지 못했습니다.") from exc
    out: dict[str, str] = {}
    try:
        for title in JUDGE_SHEETS:
            if title not in wb.sheetnames:
                raise HTTPException(422, f"kdoc 결과 엑셀이 아닙니다 ('{title}' 시트 없음).")
            rows = wb[title].iter_rows(values_only=True)
            header = list(next(rows, []))
            if "항목ID" not in header or "판정" not in header:
                raise HTTPException(422, "판정 열이 없는 보고서입니다 (v2 이전 보고서).")
            i, j = header.index("항목ID"), header.index("판정")
            for row in rows:
                item, verdict = row[i], (str(row[j]).strip() if row[j] is not None else "")
                if not item or verdict in ("", "—"):
                    continue
                if verdict not in ops.VERDICTS:
                    raise HTTPException(422, f"{item}: 판정 값은 {', '.join(ops.VERDICTS)} 중 하나여야 합니다 ({verdict}).")
                out[str(item)] = verdict
    finally:
        wb.close()
    return out


@app.post("/reviews/{review_id}/judgments")
async def judgments(review_id: UUID, file: UploadFile = File(...)) -> dict:
    run = ops.get_run(str(review_id))
    if not run or not run["ok"]:
        raise HTTPException(404, "검토 기록이 없습니다.")
    verdicts = read_judgments(await file.read(_max_upload() + 1))
    unknown = sorted(set(verdicts) - ops.finding_ids(str(review_id)))
    if unknown:
        raise HTTPException(422, f"이 검토의 항목이 아닙니다: {', '.join(unknown[:5])} — 다른 보고서 파일인지 확인하십시오.")
    if not verdicts:
        raise HTTPException(422, "판정한 항목이 없습니다.")
    ops.save_judgments(str(review_id), verdicts)
    return {"review_id": str(review_id), "project": run["project"], "saved": len(verdicts),
            "counts": {v: sum(1 for x in verdicts.values() if x == v) for v in ops.VERDICTS},
            "precision": ops.precision()}


@app.get("/metrics/precision")
def metrics_precision() -> dict:
    return ops.precision()


# ---------------------------------------------------------------- 나라장터 자동 수집 (W1)
@app.post("/bids")
def register_bid(req: BidRequest) -> dict:
    project = g2b.project_id(req.bid_no, req.bid_ord)
    old = ops.get_bid(project)
    if old:
        return {"project": project, "status": "seen", "ready": old["status"] in ("ready", "fetched", "reviewed"),
                "reason": old["reason"] or ""}
    if req.daily_limit and ops.bids_created_today() >= req.daily_limit:
        # 이미 본 공고는 상한에 세지 않는다 → 밀린 공고는 다음 날 새 공고로 처리된다
        ops.add_event("g2b", "하루 상한으로 미룸", req.name, level="info", project=project)
        return {"project": project, "status": "deferred", "ready": False, "reason": "하루 상한"}
    try:
        reg = g2b.register(req.bid_no, req.bid_ord)
    except g2b.Blocked as exc:
        ops.add_event("g2b", "수집 중단", str(exc), project=project)
        raise HTTPException(503, f"수집 중단: {exc}") from exc
    except requests.RequestException as exc:
        ops.add_event("g2b", "첨부 확인 실패", repr(exc), project=project)
        raise HTTPException(502, "나라장터 첨부 확인에 실패했습니다.") from exc
    status = "ready" if reg["ready"] else "skipped"
    ops.save_bid(project, bid_no=req.bid_no, bid_ord=req.bid_ord, name=req.name, org=req.org, price=req.price,
                 posted=req.posted, status=status, reason=reg["reason"], files=reg["files"])
    if not reg["ready"]:
        ops.add_event("g2b", reg["reason"], req.name, level="info", project=project)
    return {"project": project, "status": "new", "ready": reg["ready"], "reason": reg["reason"],
            "files": [{k: f.get(k) for k in ("seq", "name", "doc_type")} for f in reg["files"]]}


@app.post("/bids/{project}/fetch")
def fetch_bid(project: str) -> dict:
    bid = ops.get_bid(project)
    if not bid:
        raise HTTPException(404, "등록되지 않은 공고입니다.")
    if bid["status"] not in ("ready", "fetched", "reviewed"):
        raise HTTPException(409, f"검토할 수 없는 공고입니다: {bid['reason']}")
    try:
        manifest = g2b.download(project, bid["bid_no"], bid["bid_ord"], bid["files"], name=bid["name"] or "")
    except g2b.Blocked as exc:
        ops.add_event("g2b", "수집 중단", str(exc), project=project)
        raise HTTPException(503, f"수집 중단: {exc}") from exc
    except requests.RequestException as exc:
        ops.add_event("g2b", "다운로드 실패", repr(exc), project=project)
        raise HTTPException(502, "첨부 다운로드에 실패했습니다.") from exc
    errors = [f for f in manifest["files"] if f.get("error")]
    if errors:
        ops.set_bid_status(project, "failed", "다운로드 실패")
        ops.add_event("g2b", "다운로드 실패", "; ".join(f"{f['name']}: {f['error']}" for f in errors), project=project)
        raise HTTPException(502, "일부 첨부를 받지 못했습니다.")
    if bid["status"] == "ready":
        ops.set_bid_status(project, "fetched")
    return {"project": project, "files": [{k: f.get(k) for k in ("seq", "doc_type", "name", "size")}
                                          for f in manifest["files"]]}


# ---------------------------------------------------------------- 운영 (W4)
@app.post("/ops/events", status_code=201)
def post_event(req: EventRequest) -> dict:
    return {"id": ops.add_event(req.source, req.code, req.message, req.level, req.project, req.data)}


@app.get("/ops/stats")
def get_stats(days: int = Query(7, ge=1, le=90), minutes: int | None = Query(None, ge=1, le=1440)) -> dict:
    return ops.stats(days, minutes)


# ---------------------------------------------------------------- 알림함
@app.post("/alerts", status_code=201)
def post_alert(req: AlertRequest) -> dict:
    return {"id": ops.add_alert(req.kind, req.title, req.body, req.link, req.severity, req.project)}


@app.get("/alerts")
def get_alerts(unread: bool = False, limit: int = Query(200, ge=1, le=1000)) -> dict:
    return {"unread": len(ops.list_alerts(True, 1000)), "items": ops.list_alerts(unread, limit)}


@app.post("/alerts/read-all")
def read_all() -> dict:
    return {"updated": ops.mark_read()}


@app.post("/alerts/{alert_id}/read")
def read_one(alert_id: int) -> dict:
    if not ops.mark_read(alert_id):
        raise HTTPException(404, "알림이 없습니다.")
    return {"updated": 1}


@app.get("/ui/alerts", response_class=HTMLResponse)
def alerts_page() -> HTMLResponse:
    return HTMLResponse((UI / "alerts.html").read_text(encoding="utf-8"))
