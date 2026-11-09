"""도면 PDF: 문자 정보(줄 단위 + 좌표)를 뽑는다.

CAD에서 출력한 벡터 PDF는 실명·재료명·두께 같은 문자가 그대로 들어 있다.
모든 장에 반복되는 표제란(설계사무소명, 'DRAWING TITLE' 등)은 boilerplate로 표시해서
비교 단계에서 걸러낼 수 있게 한다. 스캔 도면(문자 없음)은 needs_ocr 로 남긴다.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pymupdf

BOILERPLATE_RATIO = 0.5  # 이 비율 이상의 장에 같은 문자열이 나오면 표제란으로 본다


def read_spans(path: Path) -> list[dict]:
    spans = []
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc, start=1):
            d = page.get_text("dict")
            for block in d["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not text:
                        continue
                    size = max(s["size"] for s in line["spans"])
                    x0, y0, x1, y1 = line["bbox"]
                    spans.append({
                        "page": pno, "text": text, "size": round(size, 1),
                        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                    })
        n_pages = len(doc)

    # 장마다 한 번씩만 센다
    per_page = Counter()
    for t in {(s["page"], s["text"]) for s in spans}:
        per_page[t[1]] += 1
    threshold = max(2, int(n_pages * BOILERPLATE_RATIO))
    for s in spans:
        s["boilerplate"] = per_page[s["text"]] >= threshold
    return spans
