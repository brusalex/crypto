# ./crypto_monitor/Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy strategy code
COPY eldar_strategy.py .

# Run the strategy
CMD ["python", "eldar_strategy.py"]