"""PLAN_v2 Phase A: 첨부 분류(G1)·공고 등록·업로드 검토·판정 집계·운영 통계·알림함."""

import json
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from kdoc import RAW, api, classify, g2b, ops, review


# ---------------------------------------------------------------- 분류 (§4.2)
@pytest.mark.parametrize("name, expected", [
    ("입찰공고문.hwp", "unsupported"),
    ("공고문.hwpx", "unsupported"),
    ("공사시방서.pdf", "spec"),
    ("물량내역서.xlsx", "boq"),
    ("131-2. 공고용내역서(외벽방수).xlsx", "boq"),   # '공고'보다 '내역' 규칙이 먼저
    ("설계서.xls", "boq"),
    ("[설계도면]도담중외2교.pdf", "drawing"),
    ("131. 입찰공고[외벽방수]_별표6.pdf", "notice"),
    ("첨부.zip", "zip"),
    ("계약이행 특수조건.pdf", "other"),
    ("시방서.hwp", "unsupported"),                     # HWP 시방서는 처리 불가
])
def test_classify_rules(name, expected):
    assert classify.classify(name) == expected


@pytest.mark.skipif(not all((RAW / p / "manifest.json").exists() for p in ("P1", "P2", "P3")), reason="P1~P3 필요")
def test_gate_g1_classifier_matches_registered_projects():
    """관문 G1: 손으로 분류한 P1~P3 첨부 13개를 규칙이 모두 맞힌다."""
    pairs = [(classify.classify(classify.original_name(f["file"])), f["doc_type"])
             for p in ("P1", "P2", "P3")
             for f in json.loads((RAW / p / "manifest.json").read_text(encoding="utf-8"))["files"]]
    assert len(pairs) == 13 and all(a == b for a, b in pairs)


def test_readiness_reasons():
    assert classify.readiness([{"doc_type": "spec"}, {"doc_type": "boq"}]) == (True, "")
    assert classify.readiness([{"doc_type": "notice"}, {"doc_type": "boq"}]) == (False, "시방서 없음")
    assert classify.readiness([{"doc_type": "unsupported"}]) == (False, "HWP만 있음")
    assert classify.readiness([]) == (False, "첨부 없음")


# ---------------------------------------------------------------- 나라장터 공고 (W1)
class FakeResp:
    def __init__(self, status, name=None, content=b"x" * 2048):
        self.status_code, self.content, self.url = status, content, "https://example/download"
        # 나라장터처럼 UTF-8 바이트를 헤더에 그대로 실음 (requests 는 latin-1 로 읽는다)
        self.headers = ({"content-disposition": f"attachment;filename={name};".encode().decode("latin-1")}
                        if name else {})

    def close(self):
        pass


class FakeSession:
    def __init__(self, names, status=None):
        self.names, self.status, self.calls = names, status, 0

    def get(self, url, params, **kw):
        self.calls += 1
        if self.status:
            return FakeResp(self.status)
        seq = params["fileSeq"]
        return FakeResp(200, self.names[seq - 1]) if seq <= len(self.names) else FakeResp(422)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "RAW", tmp_path / "raw")
    monkeypatch.setattr(g2b, "RAW", tmp_path / "raw")
    monkeypatch.setattr(api, "API_REPORTS", tmp_path / "reports")
    monkeypatch.setattr(g2b, "DELAY", 0)
    monkeypatch.setattr(api.fetch, "load_sources", lambda: [{"id": "P1"}])
    return TestClient(api.app)


def _use_session(monkeypatch, session):
    monkeypatch.setattr(g2b.requests, "Session", lambda: session)


def test_register_bid_ready_then_seen_then_fetch(client, monkeypatch):
    s = FakeSession(["입찰공고문.hwp", "공사시방서.pdf", "물량내역서.xlsx"])
    _use_session(monkeypatch, s)
    body = {"bid_no": "R26BK01808933", "bid_ord": "000", "name": "외벽 방수공사", "price": 249490000}
    r = client.post("/bids", json=body).json()
    assert r["status"] == "new" and r["ready"] and r["project"] == "G-R26BK01808933-000"
    assert [f["doc_type"] for f in r["files"]] == ["unsupported", "spec", "boq"]
    assert r["files"][1]["name"] == "공사시방서.pdf"
    assert s.calls == 4                       # 첨부 3개 + 끝을 알리는 422
    assert client.post("/bids", json=body).json()["status"] == "seen"
    assert s.calls == 4                       # 이미 본 공고는 나라장터에 다시 묻지 않음

    f = client.post("/bids/G-R26BK01808933-000/fetch").json()
    assert [x["doc_type"] for x in f["files"]] == ["spec", "boq"]   # 공고문(HWP)은 받지 않음
    manifest = json.loads((api.RAW / "G-R26BK01808933-000" / "manifest.json").read_text(encoding="utf-8"))
    assert all(x["sha256"] for x in manifest["files"])
    assert ops.get_bid("G-R26BK01808933-000")["status"] == "fetched"
    assert ops.requests_today() == s.calls


def test_register_bid_without_spec_is_skipped_with_reason(client, monkeypatch):
    _use_session(monkeypatch, FakeSession(["입찰공고.pdf", "공내역서.xlsx"]))
    r = client.post("/bids", json={"bid_no": "R26BK01819396"}).json()
    assert (r["ready"], r["reason"]) == (False, "시방서 없음")
    assert client.post("/bids/G-R26BK01819396-000/fetch").status_code == 409
    assert client.get("/ops/stats").json()["공고"]["검토 못 한 사유"] == {"시방서 없음": 1}


def test_blocked_site_stops_collection(client, monkeypatch):
    _use_session(monkeypatch, FakeSession([], status=429))
    r = client.post("/bids", json={"bid_no": "R26BK01819397"})
    assert r.status_code == 503 and ops.get_bid("G-R26BK01819397-000") is None
    monkeypatch.setattr(g2b, "DAILY_LIMIT", 0)
    assert client.post("/bids", json={"bid_no": "R26BK01819398"}).status_code == 503


def test_bid_number_is_validated(client):
    assert client.post("/bids", json={"bid_no": "../../etc"}).status_code == 422
    assert client.post("/reviews", json={"project": "G-NOPE-000"}).status_code == 404


# ---------------------------------------------------------------- 업로드 검토 (W2)
def test_upload_rejects_wrong_type_empty_and_large(client, monkeypatch):
    pdf, xlsx = ("a.pdf", b"%PDF-1.4"), ("b.xlsx", b"PK..")
    assert client.post("/reviews/upload", files=[("spec", ("a.txt", b"x")), ("boq", xlsx)]).status_code == 422
    assert client.post("/reviews/upload", files=[("spec", ("a.pdf", b"")), ("boq", xlsx)]).status_code == 422
    assert client.post("/reviews/upload", files=[("spec", pdf)]).status_code == 422   # 내역서 없음
    monkeypatch.setenv("KDOC_UPLOAD_MAX_MB", "0")
    assert client.post("/reviews/upload", files=[("spec", pdf), ("boq", xlsx)]).status_code == 413
    assert not api.API_REPORTS.exists() or not any(api.API_REPORTS.iterdir())   # 실패한 업로드는 남기지 않음


@pytest.mark.skipif(not (RAW / "P1" / "manifest.json").exists(), reason="P1 자료 필요")
def test_upload_review_matches_direct_review_and_accepts_judgments(client):
    """관문 G3 (API 부분): 업로드 검토의 의심 건수가 같은 파일의 직접 검토와 같다. 이어서 W3 판정 흐름."""
    files, _ = review.project_inputs(RAW, "P1")
    direct = review.run_review(files, api.API_REPORTS.parent / "direct", strict_inputs=True)
    up = [("spec", (p.name, p.read_bytes())) for p in files["spec"]] + \
         [("boq", (p.name, p.read_bytes())) for p in files["boq"]]
    r = client.post("/reviews/upload", files=up)
    assert r.status_code == 201, r.text
    body = r.json()
    for k in ("내역서에 없음", "시방서에 없음", "수치 다름", "수치 판단 불가"):
        assert body["summary"][k] == direct["summary"][k]

    rid = body["review_id"]
    top = client.get(f"/reviews/{rid}/top?n=5").json()["items"]
    assert 0 < len(top) <= 5 and all(t["kind"] != "판단 불가" for t in top)
    assert not any("내용" in k for t in top for k in t)   # 본문은 내보내지 않음

    wb = load_workbook(BytesIO(client.get(body["report_url"]).content))
    ws = wb["시방서→내역서"]
    cols = [c.value for c in ws[1]]
    assert ws.data_validations.dataValidation      # 판정 열은 목록 선택
    judgeable = [row for row in ws.iter_rows(min_row=2) if row[cols.index("판정")].value != "—"]
    for row, verdict in zip(judgeable[:3], ["진짜 문제", "문제 아님", "판단 못 함"]):
        row[cols.index("판정")].value = verdict
    buf = BytesIO()
    wb.save(buf)
    res = client.post(f"/reviews/{rid}/judgments", files={"file": ("j.xlsx", buf.getvalue())}).json()
    assert res["saved"] == 3
    assert res["precision"]["전체"]["정밀도"] == 0.5   # 진짜 1 / (진짜 1 + 아님 1)


# ---------------------------------------------------------------- 판정 (W3)
def _fake_run(kinds):
    rid = str(uuid4())
    findings = [{"항목ID": f"S{i:03d}", "종류": k, "자재": f"자재{i}"} for i, k in enumerate(kinds, 1)]
    ops.save_run(rid, "P9", "upload", 1.0, True, {}, findings)
    return rid


def _judgment_xlsx(rows):
    wb = Workbook()
    wb.remove(wb.active)
    for title in api.JUDGE_SHEETS:
        wb.create_sheet(title).append(["종류", "자재", "항목ID", "판정"])
    for item, verdict in rows:
        wb[api.JUDGE_SHEETS[0]].append(["내역서에 없음", "x", item, verdict])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_judgments_validate_and_compute_precision(client):
    rid = _fake_run(["내역서에 없음", "시방서에 없음", "수치 다름", "판단 불가"])

    def post(rows):
        return client.post(f"/reviews/{rid}/judgments", files={"file": ("j.xlsx", _judgment_xlsx(rows))})

    assert post([("S999", "진짜 문제")]).status_code == 422          # 다른 보고서 항목
    assert post([("S001", "맞음")]).status_code == 422               # 목록 밖 값
    assert post([("S001", "")]).status_code == 422                   # 판정 없음
    assert client.post(f"/reviews/{uuid4()}/judgments",
                       files={"file": ("j.xlsx", _judgment_xlsx([]))}).status_code == 404
    rows = [("S001", "진짜 문제"), ("S002", "진짜 문제"), ("S003", "문제 아님"), ("S004", "진짜 문제")]
    assert post(rows).status_code == 200
    p = client.get("/metrics/precision").json()
    # S004(판단 불가 행)는 정밀도 대상이 아니다
    assert p["전체"]["진짜 문제"] == 2 and p["전체"]["정밀도"] == round(2 / 3, 3)
    post([("S003", "진짜 문제")])                                      # 다시 올리면 덮어씀
    assert client.get("/metrics/precision").json()["전체"]["정밀도"] == 1.0


def test_old_report_without_judge_column_is_rejected(client):
    rid = _fake_run(["내역서에 없음"])
    wb = Workbook()
    for title in api.JUDGE_SHEETS:
        wb.create_sheet(title).append(["종류", "자재"])
    buf = BytesIO()
    wb.save(buf)
    r = client.post(f"/reviews/{rid}/judgments", files={"file": ("j.xlsx", buf.getvalue())})
    assert r.status_code == 422 and "판정 열" in r.json()["detail"]


# ---------------------------------------------------------------- 운영·알림 (W4)
def test_events_alerts_and_ui(client):
    assert client.post("/ops/events", json={"source": "n8n", "code": "W1 실패", "message": "timeout"}).status_code == 201
    assert client.post("/ops/events", json={"source": "n8n", "code": "x", "level": "fatal"}).status_code == 422
    a = client.post("/alerts", json={"kind": "review", "title": "검토 완료", "link": "/reviews/x/report",
                                     "severity": "success"}).json()["id"]
    client.post("/alerts", json={"title": "두 번째"})
    assert client.get("/alerts").json()["unread"] == 2
    assert client.post(f"/alerts/{a}/read").json() == {"updated": 1}
    assert client.get("/alerts?unread=true").json()["items"][0]["title"] == "두 번째"
    assert client.post("/alerts/read-all").json()["updated"] == 1
    assert client.post("/alerts/99999/read").status_code == 404
    page = client.get("/ui/alerts")
    assert page.status_code == 200 and "kdoc 알림함" in page.text
    assert client.get("/ops/stats?days=7").json()["실패 이벤트"][0]["code"] == "W1 실패"


def test_stats_latency_from_bid_to_alert(client, monkeypatch):
    _use_session(monkeypatch, FakeSession(["시방서.pdf", "내역서.xlsx"]))
    client.post("/bids", json={"bid_no": "R26BK00000001"})
    client.post("/alerts", json={"kind": "review", "title": "t", "project": "G-R26BK00000001-000"})
    lat = client.get("/ops/stats").json()["공고 등록→알림(초)"]
    assert lat["건수"] == 1 and lat["P50"] >= 0


def test_daily_limit_counts_only_new_bids(client, monkeypatch):
    """하루 상한은 새로 등록하는 공고에만 적용 — 이미 본 공고 때문에 밀린 공고가 계속 밀리지 않는다."""
    _use_session(monkeypatch, FakeSession(["시방서.pdf", "내역서.xlsx"]))
    first = {"bid_no": "R26BK00000011", "daily_limit": 1}
    assert client.post("/bids", json=first).json()["status"] == "new"
    assert client.post("/bids", json=first).json()["status"] == "seen"
    r = client.post("/bids", json={"bid_no": "R26BK00000012", "daily_limit": 1}).json()
    assert r["status"] == "deferred" and ops.get_bid("G-R26BK00000012-000") is None
    assert client.get("/ops/stats").json()["수집 정보"]["하루 상한으로 미룸"] == 1
