FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей для asyncpg
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все файлы проекта
COPY bot.py .
COPY i18n/ ./i18n/
COPY knowledge_structure.yaml .
COPY config/ ./config/
COPY db/ ./db/
COPY core/ ./core/
COPY clients/ ./clients/
COPY engines/ ./engines/
COPY topics/ ./topics/
COPY states/ ./states/

CMD ["python", "bot.py"]
