FROM python:3.12-slim
WORKDIR /app
# Patch OS packages (self-heals base-image openssl/libssl CVE lag at build time)
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
EXPOSE 8085
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085", "--proxy-headers"]
