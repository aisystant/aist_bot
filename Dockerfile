FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для asyncpg
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY requirements.txt .
ARG GITHUB_BOT_PAT
ENV GIT_TERMINAL_PROMPT=0
RUN if [ -n "$GITHUB_BOT_PAT" ]; then \
      git config --global url."https://${GITHUB_BOT_PAT}@github.com/".insteadOf "https://github.com/"; \
    fi
RUN pip install --no-cache-dir -r requirements.txt
RUN git config --global --remove-section url."https://${GITHUB_BOT_PAT}@github.com/" 2>/dev/null || true

# Копируем все файлы проекта
COPY bot.py .
COPY oauth_server.py .
COPY i18n/ ./i18n/
COPY knowledge_structure.yaml .
COPY config/ ./config/
COPY db/ ./db/
COPY core/ ./core/
COPY clients/ ./clients/
COPY engines/ ./engines/
COPY integrations/ ./integrations/
COPY topics/ ./topics/
COPY states/ ./states/
COPY handlers/ ./handlers/
COPY helpers/ ./helpers/
COPY data/ ./data/

EXPOSE ${PORT:-8080}
CMD ["python", "bot.py"]
