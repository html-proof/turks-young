FROM python:alpine

RUN addgroup -S app && adduser -S app -G app

WORKDIR /turks-young

COPY api api/
COPY app.py app.py
COPY requirements.txt requirements.txt

RUN apk add g++ make libffi-dev openssl-dev --no-cache \
 && pip3 install --no-cache-dir -r requirements.txt \
 && chown -R app:app /turks-young

EXPOSE 8000

USER app

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]