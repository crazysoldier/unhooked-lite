FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd -m -u 1000 bot && mkdir -p /app/data && chown bot:bot /app/data
COPY . .
USER bot
CMD ["python", "bot.py"]
