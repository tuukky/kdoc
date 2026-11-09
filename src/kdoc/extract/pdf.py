"""PDF 공통: 페이지 텍스트 + 스캔(이미지) 페이지 판별."""

from __future__ import annotations

from pathlib import Path

import pymupdf

# 페이지 문자 수가 이보다 적고 이미지가 있으면 스캔 페이지로 보고 OCR 대상으로 표시
SCANNED_MAX_CHARS = 30


def read_pages(path: Path) -> list[dict]:
    pages = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text")
            n_img = len(page.get_images(full=False))
            n_chars = len(text.strip())
            pages.append({
                "page": i,
                "text": text,
                "n_chars": n_chars,
                "n_images": n_img,
                "needs_ocr": n_chars < SCANNED_MAX_CHARS and n_img > 0,
                "width": round(page.rect.width, 1),
                "height": round(page.rect.height, 1),
            })
    return pages
