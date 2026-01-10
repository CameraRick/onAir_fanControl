FROM python:3.12-alpine

RUN apk add --no-cache tzdata smartmontools

WORKDIR /app
COPY src/ /app/

RUN pip install --no-cache-dir paho-mqtt flask

ENV PYTHONUNBUFFERED=1
EXPOSE 8088

CMD ["python", "/app/app.py"]
