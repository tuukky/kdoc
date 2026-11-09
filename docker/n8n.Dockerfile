# n8n + fetch 실행용 파이썬.
# n8n 1.123.21 부터 이미지가 "Docker Hardened Images"로 바뀌어 apk·python3 가 없다
# (2026-11-03 직접 확인). 그래서 apk 가 남아 있는 마지막 버전으로 고정한다.
# 이 버전은 보안 업데이트를 더 받지 못하므로 로컬 실습·포트폴리오용으로만 쓴다.
FROM n8nio/n8n:1.123.20

USER root
RUN apk add --no-cache python3 py3-pip

# fetch 는 requests·pyyaml 만 필요하다 (__main__ 은 추출 모듈을 fetch 때 불러오지 않음).
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV \
 && $VIRTUAL_ENV/bin/pip install --no-cache-dir "requests>=2.31" "pyyaml>=6.0"
# Execute Command 노드에서 `python3 -m kdoc fetch` 가 venv 파이썬과 /work/src 코드를 쓰게 한다.
ENV PATH="$VIRTUAL_ENV/bin:$PATH" \
    PYTHONPATH=/work/src \
    PYTHONIOENCODING=utf-8

USER node
