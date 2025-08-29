FROM python:3.11-slim

# Avoid .pyc and ensure unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Modbus TCP port
EXPOSE 5020

# Optional envs: HOST, PORT, UNIT_ID
ENV HOST=0.0.0.0 PORT=5020 UNIT_ID=1

CMD ["python", "app.py"]
