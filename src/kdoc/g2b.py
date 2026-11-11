"""나라장터 자동 수집 공고(W1)의 첨부 확인·분류·다운로드.

공고 목록 검색은 n8n(W1)이 나라장터 통합검색을 직접 부른다. 여기서는 공고 1건에 대해
  1) 첨부를 fileSeq 1부터 차례로 열어 응답 헤더의 파일명만 확인 (없는 번호는 422 → 끝)
  2) 파일명으로 분류 (classify.py, PLAN_v2 §4.2). ZIP 은 받아서 한 단계만 풀어 안의 파일을 분류
  3) 검토에 쓸 파일(시방서·내역서·도면)만 받아 data/raw/{project}/ 에 저장하고 manifest.json 기록
수집 예절 (PLAN_v2 §4.4): 요청 간격 1초, 하루 요청 상한, 403·429 이면 즉시 중단.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import requests

from . import RAW, classify, ops
from .fetch import URL

USER_AGENT = "kdoc-research/0.2 (+https://github.com/tuukky/kdoc)"
MAX_SEQ = 15
DELAY = float(os.environ.get("KDOC_G2B_DELAY", "1.0"))
DAILY_LIMIT = int(os.environ.get("KDOC_G2B_DAILY_LIMIT", "600"))


class Blocked(RuntimeError):
    """사이트가 막음(403·429) 또는 하루 요청 상한 도달 → 그날 수집 중단."""


def project_id(bid_no: str, bid_ord: str) -> str:
    return f"G-{bid_no}-{bid_ord}"


def _name(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if not m:
        return fallback
    raw = m.group(1)
    try:  # 나라장터는 UTF-8 바이트를 그대로 보내고 requests 는 latin-1 로 읽는다
        raw = raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return re.sub(r'[\\/:*?"<>|]', "_", unquote(raw)).strip()


def _get(session: requests.Session, bid_no: str, bid_ord: str, seq: int, *, stream: bool) -> requests.Response:
    if ops.requests_today() >= DAILY_LIMIT:
        raise Blocked(f"하루 요청 상한({DAILY_LIMIT}) 도달")
    ops.count_request()
    r = session.get(URL, params={"bidPbancNo": bid_no, "bidPbancOrd": bid_ord, "fileType": "", "fileSeq": seq,
                                 "prcmBsneSeCd": "07"}, stream=stream, timeout=120,
                    headers={"User-Agent": USER_AGENT})
    if r.status_code in (403, 429):
        r.close()
        raise Blocked(f"나라장터 응답 {r.status_code}")
    return r


def probe(bid_no: str, bid_ord: str, session: requests.Session | None = None, delay: float = DELAY) -> list[dict]:
    """첨부 목록 [{'seq', 'name'}]. 본문은 받지 않는다."""
    s = session or requests.Session()
    out = []
    for seq in range(1, MAX_SEQ + 1):
        r = _get(s, bid_no, bid_ord, seq, stream=True)
        r.close()
        if r.status_code != 200 or "content-disposition" not in r.headers:
            break
        out.append({"seq": seq, "name": _name(r, f"seq{seq}.bin")})
        time.sleep(delay)
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def register(bid_no: str, bid_ord: str, session: requests.Session | None = None, delay: float = DELAY) -> dict:
    """첨부 확인 + 분류. ZIP 은 받아서 풀고 안의 파일을 분류한다. → {'files', 'ready', 'reason'}"""
    s = session or requests.Session()
    project = project_id(bid_no, bid_ord)
    files = []
    for a in probe(bid_no, bid_ord, s, delay):
        t = classify.classify(a["name"])
        if t != classify.ZIP:
            files.append({**a, "doc_type": t})
            continue
        r = _get(s, bid_no, bid_ord, a["seq"], stream=False)
        time.sleep(delay)
        folder = RAW / project / f"zip_{a['seq']:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                members = [m for m in z.infolist() if not m.is_dir()]
                for i, m in enumerate(members, 1):
                    name = m.filename
                    if not (m.flag_bits & 0x800):  # 한글 윈도우 압축은 파일명이 CP949
                        try:
                            name = m.filename.encode("cp437").decode("cp949")
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            pass
                    name = re.sub(r'[\\/:*?"<>|]', "_", Path(name).name)
                    data = z.read(m)
                    (folder / name).write_bytes(data)
                    inner = classify.classify(name)
                    files.append({"seq": a["seq"] * 100 + i, "name": name, "from_zip": a["name"],
                                  "doc_type": classify.OTHER if inner == classify.ZIP else inner,
                                  "file": f"zip_{a['seq']:02d}/{name}", "size": len(data), "sha256": _sha(data)})
        except zipfile.BadZipFile:
            files.append({**a, "doc_type": classify.OTHER, "error": "ZIP을 열지 못함"})
    ready, reason = classify.readiness(files)
    return {"project": project, "files": files, "ready": ready, "reason": reason}


def download(project: str, bid_no: str, bid_ord: str, files: list[dict], session: requests.Session | None = None,
             delay: float = DELAY, name: str = "") -> dict:
    """검토에 쓸 파일(시방서·내역서·도면)만 받아 manifest.json 을 쓴다. 이미 받은 파일은 건너뛴다."""
    s = session or requests.Session()
    out_dir = RAW / project
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for f in files:
        if f["doc_type"] not in classify.REVIEWABLE:
            continue
        if f.get("file"):  # ZIP 에서 이미 꺼낸 파일
            records.append({k: f[k] for k in ("seq", "doc_type", "file", "size", "sha256", "name")})
            continue
        r = _get(s, bid_no, bid_ord, f["seq"], stream=False)
        time.sleep(delay)
        if r.status_code != 200:
            records.append({"seq": f["seq"], "doc_type": f["doc_type"], "name": f["name"],
                            "error": f"HTTP {r.status_code}"})
            continue
        stored = f"{f['seq']:02d}_{f['doc_type']}_{_name(r, f['name'])}"
        (out_dir / stored).write_bytes(r.content)
        records.append({"seq": f["seq"], "doc_type": f["doc_type"], "name": f["name"], "file": stored,
                        "size": len(r.content), "sha256": _sha(r.content), "url": r.url,
                        "fetched_at": datetime.now(timezone.utc).isoformat()})
    manifest = {"project": {"id": project, "name": name, "bid_no": bid_no, "bid_ord": bid_ord, "source": "g2b-auto"},
                "files": records}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
