# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install Docker CLI so the app can spawn sibling containers
RUN apt-get update && apt-get install -y docker.io && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the port
EXPOSE 8000

# Run the application
CMD ["python", "codex_serve.py"]
