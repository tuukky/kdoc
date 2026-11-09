"""등록된 프로젝트의 문서 추출과 비교 검토 API.

    uvicorn kdoc.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import RAW, fetch, pipeline, review
from .sections import PAIRS, REVIEW

app = FastAPI(title="kdoc", version="0.2.0")
logger = logging.getLogger(__name__)
API_REPORTS = REVIEW / "api"
REPORT_FILENAME = "report.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExtractRequest(BaseModel):
    project: str | None = None  # 생략하면 sources.yaml 의 전체 프로젝트


class ReviewRequest(BaseModel):
    project: str


class ReviewResponse(BaseModel):
    review_id: UUID
    project: str
    summary: dict
    report_url: str


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


@app.post("/reviews", status_code=201, response_model=ReviewResponse)
def create_review(req: ReviewRequest) -> ReviewResponse:
    if req.project not in {p["id"] for p in fetch.load_sources()}:
        raise HTTPException(404, "등록되지 않은 프로젝트입니다.")
    review_id = uuid4()
    run_root = API_REPORTS / str(review_id)
    try:
        files, doc_ids = review.project_inputs(RAW, req.project)
        # API 검토는 자동 재현이 가능하도록 사용자 지정/이름 일치만 사용한다.
        result = review.run_review(files, run_root, doc_ids=doc_ids, pairs_file=PAIRS / f"{req.project}.csv",
                                   strict_inputs=True)
        Path(result["excel"]).replace(run_root / REPORT_FILENAME)
    except review.ProjectNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    except review.ReviewProcessingError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception("review failed: project=%s review_id=%s", req.project, review_id)
        raise HTTPException(500, "검토 처리에 실패했습니다.") from exc

    summary = {key: value for key, value in result["summary"].items() if key not in
               {"문서", "자재 사전", "공정 짝 경고"}}
    summary["공정 짝 경고 수"] = len(result["summary"]["공정 짝 경고"])
    return ReviewResponse(review_id=review_id, project=req.project, summary=summary,
                          report_url=f"/reviews/{review_id}/report")


@app.get("/reviews/{review_id}/report", response_class=FileResponse,
         responses={200: {"content": {XLSX: {}}}, 404: {"description": "보고서 없음"}})
def download_report(review_id: UUID) -> FileResponse:
    path = API_REPORTS / str(review_id) / REPORT_FILENAME
    if not path.is_file():
        raise HTTPException(404, "검토 보고서가 없습니다.")
    return FileResponse(path, media_type=XLSX, filename=f"kdoc-{review_id}.xlsx")
