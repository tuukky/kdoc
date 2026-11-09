"""kdoc API: n8n이 받는 응답 형태와 오류 코드 확인 (실제 추출은 가짜로 바꿔 빠르게)."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from kdoc import RAW, api


@pytest.fixture
def client(tmp_path, monkeypatch):
    for pid in ("P1", "P2"):
        (tmp_path / pid).mkdir()
    (tmp_path / "P1" / "manifest.json").write_text("{}", encoding="utf-8")  # P2 는 fetch 안 된 상태
    monkeypatch.setattr(api, "RAW", tmp_path)
    monkeypatch.setattr(api.fetch, "load_sources", lambda: [{"id": "P1"}, {"id": "P2"}])

    def fake_extract(pid):
        docs = [{"doc_id": f"{pid}-01"}, {"doc_id": f"{pid}-02", "error": "ValueError: 깨진 파일"}]
        return {"project": pid, "docs": docs if pid == "P1" else docs[:1]}

    monkeypatch.setattr(api.pipeline, "extract_project", fake_extract)
    return TestClient(api.app)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_extract_reports_document_errors(client):
    body = client.post("/extract", json={"project": "P1"}).json()
    assert body["ok"] is False
    assert body["errors"] == [{"project": "P1", "doc_id": "P1-02", "error": "ValueError: 깨진 파일"}]
    assert body["results"][0]["project"] == "P1"


def test_extract_unknown_project_is_404(client):
    r = client.post("/extract", json={"project": "P9"})
    assert r.status_code == 404


def test_extract_without_fetch_is_409(client):
    r = client.post("/extract", json={"project": "P2"})
    assert r.status_code == 409 and "P2" in r.json()["detail"]


def test_review_http_errors(client):
    assert client.post("/reviews", json={"project": "P9"}).status_code == 404
    assert client.post("/reviews", json={"project": "P2"}).status_code == 409
    assert client.post("/reviews", json={}).status_code == 422
    assert client.get("/reviews/not-a-uuid/report").status_code == 422
    assert client.get(f"/reviews/{uuid4()}/report").status_code == 404
    malformed = client.post("/reviews", json={"project": "P1"})
    assert malformed.status_code == 500
    assert malformed.json() == {"detail": "검토 처리에 실패했습니다."}


def test_unreadable_review_input_is_not_a_success(client, monkeypatch):
    project = api.RAW / "P1"
    (project / "broken.pdf").write_bytes(b"bad pdf")
    (project / "broken.xlsx").write_bytes(b"bad xlsx")
    (project / "manifest.json").write_text(
        '{"files":[{"seq":1,"doc_type":"spec","file":"broken.pdf"},'
        '{"seq":2,"doc_type":"boq","file":"broken.xlsx"}]}', encoding="utf-8")
    monkeypatch.setattr(api, "API_REPORTS", project / "reports")
    assert client.post("/reviews", json={"project": "P1"}).status_code == 422
    assert not api.API_REPORTS.exists()


@pytest.mark.skipif(not (RAW / "P1" / "manifest.json").exists(), reason="P1 자료 필요")
def test_concurrent_reviews_return_separate_linked_excel(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "API_REPORTS", tmp_path)
    with TestClient(api.app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/reviews", json={"project": "P1"}), range(2)))
        ids = set()
        for response in responses:
            assert response.status_code == 201, response.text
            body = response.json()
            ids.add(body["review_id"])
            assert body["summary"]["판단 불가"] > 0
            assert "문서" not in body["summary"]
            report = client.get(body["report_url"])
            assert report.status_code == 200
            assert report.headers["content-type"] == api.XLSX
            assert "attachment" in report.headers["content-disposition"]
            assert report.content == (tmp_path / body["review_id"] / api.REPORT_FILENAME).read_bytes()
            with BytesIO(report.content) as data:
                wb = load_workbook(data)
                ws = wb["시방서→내역서"]
                link_col = next(c.column for c in ws[1] if c.value == "시방서 열기")
                row = next(c.row for c in ws["B"] if c.value == "우레탄 방수")
                assert ws.cell(row, link_col).hyperlink.target.endswith(".pdf#page=24")
                wb.close()
        assert len(ids) == 2
