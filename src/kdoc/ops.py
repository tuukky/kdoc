"""운영 기록 DB (SQLite): 공고·검토 실행·의심 항목·판정·실패 이벤트·알림·외부 요청 수.

n8n 워크플로(W1~W5)는 상태를 직접 들고 있지 않고 kdoc-api 를 거쳐 여기에 남긴다.
경로: 환경변수 KDOC_OPS_DB (상대 경로면 프로젝트 루트 기준), 기본 data/ops/kdoc.db.
요청마다 새 연결을 열어 FastAPI 스레드에서 안전하게 쓴다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from . import DATA, ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    project TEXT PRIMARY KEY, bid_no TEXT NOT NULL, bid_ord TEXT NOT NULL, name TEXT, org TEXT,
    price INTEGER, posted TEXT, status TEXT NOT NULL, reason TEXT, files TEXT, created TEXT NOT NULL,
    updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    review_id TEXT PRIMARY KEY, project TEXT NOT NULL, source TEXT NOT NULL, started TEXT NOT NULL,
    seconds REAL, ok INTEGER NOT NULL, summary TEXT);
CREATE TABLE IF NOT EXISTS findings (
    review_id TEXT NOT NULL, item_id TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL,
    material TEXT, spec_file TEXT, spec_page INTEGER, boq_file TEXT, boq_loc TEXT,
    PRIMARY KEY (review_id, item_id));
CREATE TABLE IF NOT EXISTS judgments (
    review_id TEXT NOT NULL, item_id TEXT NOT NULL, verdict TEXT NOT NULL, ts TEXT NOT NULL,
    PRIMARY KEY (review_id, item_id));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT NOT NULL, level TEXT NOT NULL,
    code TEXT NOT NULL, message TEXT, project TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT, link TEXT, project TEXT, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests (day TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""

VERDICTS = ("진짜 문제", "문제 아님", "판단 못 함")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_path() -> Path:
    p = Path(os.environ.get("KDOC_OPS_DB") or DATA / "ops" / "kdoc.db")
    return p if p.is_absolute() else ROOT / p


@contextmanager
def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


# ---- 공고 ----
def get_bid(project: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM bids WHERE project=?", (project,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["files"] = json.loads(d["files"] or "[]")
    return d


def save_bid(project: str, *, bid_no: str, bid_ord: str, name: str, org: str, price: int | None,
             posted: str, status: str, reason: str, files: list[dict]) -> None:
    t = now()
    with connect() as con:
        con.execute(
            "INSERT INTO bids VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
            "status=excluded.status, reason=excluded.reason, files=excluded.files, updated=excluded.updated",
            (project, bid_no, bid_ord, name, org, price, posted, status, reason,
             json.dumps(files, ensure_ascii=False), t, t))


def bids_created_today() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM bids WHERE created LIKE ?", (day + "%",)).fetchone()[0]


def set_bid_status(project: str, status: str, reason: str = "") -> None:
    with connect() as con:
        con.execute("UPDATE bids SET status=?, reason=?, updated=? WHERE project=?", (status, reason, now(), project))


# ---- 검토 실행·의심 항목 ----
def save_run(review_id: str, project: str, source: str, seconds: float | None, ok: bool,
             summary: dict | None = None, findings: list[dict] | None = None) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                    (review_id, project, source, now(), seconds, int(ok),
                     json.dumps(summary or {}, ensure_ascii=False, default=str)))
        for f in findings or []:
            con.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,?,?)",
                        (review_id, f["항목ID"], project, f["종류"], f.get("자재"), f.get("시방서 파일"),
                         f.get("_spec_page"), f.get("내역서 파일"), f.get("내역서 위치")))


def get_run(review_id: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM runs WHERE review_id=?", (review_id,)).fetchone()
    return dict(r) if r else None


def finding_ids(review_id: str) -> set[str]:
    with connect() as con:
        return {r[0] for r in con.execute("SELECT item_id FROM findings WHERE review_id=?", (review_id,))}


# 정밀도 대상이 아닌 종류 (도구가 판정하지 않은 항목)
NOT_JUDGED = ("판단 불가",)
_KIND_ORDER = {"수치 다름": 0, "내역서에 없음": 1, "시방서에 없음": 2}


def top_findings(review_id: str, n: int) -> list[dict]:
    """본문 없이 자재·종류·위치만. W5(설계 질의서 초안) 입력."""
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT item_id, kind, material, spec_file, spec_page, boq_file, boq_loc FROM findings "
            "WHERE review_id=? AND kind NOT IN (%s)" % ",".join("?" * len(NOT_JUDGED)), (review_id, *NOT_JUDGED))]
    rows.sort(key=lambda r: (_KIND_ORDER.get(r["kind"], 9), r["item_id"]))
    return rows[:n]


def save_judgments(review_id: str, verdicts: dict[str, str]) -> None:
    t = now()
    with connect() as con:
        con.executemany("INSERT OR REPLACE INTO judgments VALUES (?,?,?,?)",
                        [(review_id, k, v, t) for k, v in verdicts.items()])


def precision() -> dict:
    """정밀도 = 진짜 문제 / (진짜 문제 + 문제 아님). 판단 못 함은 따로 센다 (PLAN_v2 §5.2)."""
    with connect() as con:
        rows = con.execute(
            "SELECT f.project, j.verdict, COUNT(*) FROM judgments j JOIN findings f "
            "ON f.review_id=j.review_id AND f.item_id=j.item_id WHERE f.kind NOT IN (%s) "
            "GROUP BY f.project, j.verdict" % ",".join("?" * len(NOT_JUDGED)), NOT_JUDGED).fetchall()
    per: dict[str, dict] = {}
    for project, verdict, n in rows:
        per.setdefault(project, dict.fromkeys(VERDICTS, 0))[verdict] = n
    total = dict.fromkeys(VERDICTS, 0)
    for c in per.values():
        for v in VERDICTS:
            total[v] += c[v]

    def fmt(c: dict) -> dict:
        denom = c["진짜 문제"] + c["문제 아님"]
        return {**c, "판정 수": sum(c.values()), "정밀도": round(c["진짜 문제"] / denom, 3) if denom else None}

    return {"전체": fmt(total), "프로젝트별": {p: fmt(c) for p, c in sorted(per.items())}}


# ---- 이벤트·알림 ----
def add_event(source: str, code: str, message: str = "", level: str = "error", project: str | None = None,
              data: dict | None = None) -> int:
    with connect() as con:
        cur = con.execute("INSERT INTO events (ts, source, level, code, message, project, data) VALUES (?,?,?,?,?,?,?)",
                          (now(), source, level, code, message, project,
                           json.dumps(data, ensure_ascii=False) if data else None))
        return cur.lastrowid


def add_alert(kind: str, title: str, body: str = "", link: str | None = None, severity: str = "info",
              project: str | None = None) -> int:
    with connect() as con:
        cur = con.execute("INSERT INTO alerts (ts, kind, severity, title, body, link, project) VALUES (?,?,?,?,?,?,?)",
                          (now(), kind, severity, title, body, link, project))
        return cur.lastrowid


def list_alerts(unread_only: bool = False, limit: int = 200) -> list[dict]:
    q = "SELECT * FROM alerts" + (" WHERE read=0" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
    with connect() as con:
        return [dict(r) for r in con.execute(q, (limit,))]


def mark_read(alert_id: int | None = None) -> int:
    with connect() as con:
        if alert_id is None:
            return con.execute("UPDATE alerts SET read=1 WHERE read=0").rowcount
        return con.execute("UPDATE alerts SET read=1 WHERE id=?", (alert_id,)).rowcount


# ---- 외부 요청 수 (수집 예절: 하루 상한) ----
def count_request(n: int = 1) -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    with connect() as con:
        con.execute("INSERT INTO requests VALUES (?,?) ON CONFLICT(day) DO UPDATE SET n=n+?", (day, n, n))
        return con.execute("SELECT n FROM requests WHERE day=?", (day,)).fetchone()[0]


def requests_today() -> int:
    day = datetime.now().strftime("%Y-%m-%d")
    with connect() as con:
        r = con.execute("SELECT n FROM requests WHERE day=?", (day,)).fetchone()
    return r[0] if r else 0


# ---- 운영 통계 (W4 주간 요약, docs/operations.md) ----
def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def stats(days: int = 7, minutes: int | None = None) -> dict:
    """minutes 를 주면 그 기간만 (W1 한 번 실행의 요약). 시각은 API 쪽 시계 기준."""
    since = (datetime.now() - (timedelta(minutes=minutes) if minutes else timedelta(days=days))).isoformat(
        timespec="seconds")
    with connect() as con:
        bids = con.execute("SELECT status, reason, COUNT(*) n FROM bids WHERE created>=? GROUP BY status, reason",
                           (since,)).fetchall()
        runs = con.execute("SELECT source, ok, seconds FROM runs WHERE started>=?", (since,)).fetchall()
        events = con.execute("SELECT source, code, COUNT(*) n FROM events WHERE ts>=? AND level='error' "
                             "GROUP BY source, code ORDER BY n DESC", (since,)).fetchall()
        infos = con.execute("SELECT code, COUNT(*) n FROM events WHERE ts>=? AND level='info' AND source='g2b' "
                            "GROUP BY code ORDER BY n DESC", (since,)).fetchall()
        lat = con.execute(
            "SELECT b.created, MIN(a.ts) FROM bids b JOIN alerts a ON a.project=b.project AND a.kind='review' "
            "WHERE b.created>=? GROUP BY b.project", (since,)).fetchall()
        reqs = con.execute("SELECT day, n FROM requests WHERE day>=? ORDER BY day", (since[:10],)).fetchall()
    latency = [(datetime.fromisoformat(a) - datetime.fromisoformat(c)).total_seconds() for c, a in lat]
    secs = [r["seconds"] for r in runs if r["ok"] and r["seconds"] is not None]
    by_status: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in bids:
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        if r["reason"]:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + r["n"]
    return {
        "기간(일)": None if minutes else days, "기간(분)": minutes, "시작": since,
        "공고": {"등록": sum(by_status.values()), "상태별": by_status, "검토 못 한 사유": reasons},
        "검토": {"성공": sum(1 for r in runs if r["ok"]), "실패": sum(1 for r in runs if not r["ok"]),
                 "출처별": {s: sum(1 for r in runs if r["source"] == s) for s in sorted({r["source"] for r in runs})},
                 "처리 시간 P50(초)": _pct(secs, .5), "처리 시간 P90(초)": _pct(secs, .9)},
        "공고 등록→알림(초)": {"건수": len(latency), "P50": _pct(latency, .5), "P90": _pct(latency, .9)},
        "실패 이벤트": [dict(r) for r in events],
        "수집 정보": {r["code"]: r["n"] for r in infos},
        "외부 요청 수(일별)": {r["day"]: r["n"] for r in reqs},
    }
