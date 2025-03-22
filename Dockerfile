FROM python:3.11-slim

# Install system dependencies with additional required packages
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    xvfb \
    libgbm1 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm-dev \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Set up virtual display
ENV DISPLAY=:99

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port for Elastic Beanstalk
EXPOSE 8080

# Start Xvfb and run the application with increased timeout
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x16 & uvicorn main:app --host 0.0.0.0 --port 8080 --timeout-keep-alive 300"]