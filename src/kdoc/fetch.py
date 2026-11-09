"""G2B(나라장터) 입찰공고 첨부파일 수집.

config/sources.yaml 의 목록대로 받아 data/raw/{project_id}/ 에 저장하고
manifest.json(원본 파일명, 크기, sha256, URL, 받은 시각)을 남긴다.
이미 같은 sha256 파일이 있으면 다시 받지 않는다 → n8n에서 주기 실행해도 안전.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import requests
import yaml

from . import CONFIG, RAW

URL = "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"


def _filename(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    name = unquote(m.group(1)) if m else fallback
    # Windows 금지 문자 제거
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def load_sources(path: Path | None = None) -> list[dict]:
    path = path or CONFIG / "sources.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["projects"]


def fetch_project(proj: dict, retries: int = 3, timeout: int = 300) -> list[dict]:
    out_dir = RAW / proj["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    old = {}
    if manifest_path.exists():
        old = {f["seq"]: f for f in json.loads(manifest_path.read_text(encoding="utf-8"))["files"]}

    records = []
    for f in proj["files"]:
        seq = f["seq"]
        params = {
            "bidPbancNo": proj["bid_no"], "bidPbancOrd": proj["bid_ord"],
            "fileType": "", "fileSeq": seq, "prcmBsneSeCd": "07",
        }
        prev = old.get(seq)
        if prev and (out_dir / prev["file"]).exists():
            records.append(prev)
            continue

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(URL, params=params, timeout=timeout)
                r.raise_for_status()
                if len(r.content) < 1024 and "content-disposition" not in r.headers:
                    raise RuntimeError(f"첨부파일 없음 (seq={seq}, {len(r.content)} bytes)")
                name = f"{seq:02d}_{f['doc_type']}_{_filename(r, f'seq{seq}.bin')}"
                (out_dir / name).write_bytes(r.content)
                records.append({
                    **f, "file": name, "size": len(r.content),
                    "sha256": hashlib.sha256(r.content).hexdigest(),
                    "url": r.url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
                break
            except Exception as e:  # 네트워크·서버 오류는 재시도
                last_err = e
                time.sleep(2 * attempt)
        else:
            records.append({**f, "error": str(last_err)})

    manifest = {"project": {k: v for k, v in proj.items() if k != "files"}, "files": records}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return records
