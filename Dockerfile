FROM python:3.14.2-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python3", "-u", "gtfs.py"]