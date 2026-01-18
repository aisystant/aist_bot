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

COPY bot.py .
COPY knowledge_structure.yaml .

CMD ["python", "bot.py"]
