FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Tokyo
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ur_client.py monitor.py config.yaml watchlist.yaml ./
CMD ["python", "monitor.py"]
