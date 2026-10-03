FROM python:3.11-slim AS application

WORKDIR /app

# Установка системных зависимостей для asyncpg
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY wheels/ ./wheels/
RUN pip install --no-cache-dir wheels/activity_hub-0.1.0-py3-none-any.whl
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все файлы проекта
COPY bot.py .
COPY oauth_server.py .
COPY readiness.py .
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
COPY archive/ ./archive/
COPY handlers/ ./handlers/
COPY helpers/ ./helpers/
COPY data/ ./data/

EXPOSE ${PORT:-8080}
CMD ["python", "bot.py"]

# The preparation workflow supplies this validated file separately from Git.
# This target is opt-in and cannot build without the candidate manifest.
FROM application AS release
COPY release-manifest.candidate.json ./release-manifest.json

# Ordinary Railway builds keep using the existing application target.
FROM application AS development
