# 공지 수집·스레드 봇을 한 이미지로 돌린다. 코드와 도구만 담고, 데이터·비밀값·로그인은 모두 볼륨(홈 폴더)에 둔다.
# 같은 이미지로 두 서비스를 띄운다: `ajou schedule`(수집·요약·주간 정리 시계)과 `ajou-bot`(Discord 스레드).
FROM python:3.12-slim-bookworm

ARG CLAUDE_CODE_VERSION=2.1.316

# git·git-crypt: 데이터 폴더 동기화(AJOU_GIT_SYNC=1). poppler-utils: PDF 글자·쪽 이미지. catdoc: 옛 .doc 첨부.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git git-crypt openssh-client poppler-utils catdoc curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Claude Code: 스레드 대화에서 쓴다. 판을 고정하고 자동 업데이트를 끈다.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" \
 && cp -L /root/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.local /root/.claude* \
 && claude --version

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra aws --no-install-project
COPY ajou_assistant ./ajou_assistant
RUN uv sync --frozen --no-dev --extra aws && chmod -R a+rX /app

ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    LANG=C.UTF-8

ENTRYPOINT ["tini", "--"]
CMD ["ajou", "schedule"]
