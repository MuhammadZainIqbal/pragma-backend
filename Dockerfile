FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for PostgreSQL if needed
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend assets to the working directory
COPY . .

# Expose port and run on 7860 as required by Hugging Face
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
