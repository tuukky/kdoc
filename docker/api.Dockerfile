# kdoc 추출 API 컨테이너. n8n이 http://kdoc-api:8000/extract 로 호출한다.
# 코드·데이터는 이미지에 넣지 않고 compose 에서 프로젝트 폴더를 /work 로 마운트한다
# → 코드를 고치면 컨테이너 재시작만으로 반영, data/ 는 n8n(fetch)과 같은 폴더를 본다.
FROM python:3.13.18-slim-trixie

WORKDIR /work
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/work/src

# 의존성만 먼저 설치해 레이어 캐시를 살린다 (pyproject.toml 이 바뀔 때만 다시 설치)
COPY pyproject.toml /tmp/kdoc/pyproject.toml
RUN python -c "import tomllib; d=tomllib.load(open('/tmp/kdoc/pyproject.toml','rb'))['project']; print('\n'.join(d['dependencies'] + d['optional-dependencies']['api']))" > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && rm -rf /tmp/kdoc /tmp/req.txt

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "kdoc.api:app", "--host", "0.0.0.0", "--port", "8000"]
