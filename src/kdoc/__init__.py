"""kdoc — 건설 문서(도면·시방서·내역서) 불일치 검토 보조 도구."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
CONFIG = ROOT / "config"
