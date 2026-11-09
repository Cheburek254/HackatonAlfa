FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ALFA_DATABASE_PATH=/var/lib/alfahack/users.sqlite3
WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY fixtures ./fixtures
RUN install -d -m 0700 -o 10001 -g 10001 /var/lib/alfahack

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=4s --start-period=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).close()"]

CMD ["python", "-m", "waitress", "--listen=0.0.0.0:8080", "--max-request-body-size=8192", "--call", "app.ui.web:create_app"]
