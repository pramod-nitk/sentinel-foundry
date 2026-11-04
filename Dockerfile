FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sentinel ./sentinel
ENV SENTINEL_DB_PATH=/data/sentinel.db SENTINEL_PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "sentinel"]
