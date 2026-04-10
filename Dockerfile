FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd -m -u 1000 bot
COPY . .
RUN mkdir -p /app/data && chown -R bot:bot /app/data
USER bot
VOLUME ["/app/data"]
CMD ["python", "bot.py"]
