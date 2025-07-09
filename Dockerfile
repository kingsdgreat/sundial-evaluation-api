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
    redis-server \
    && rm -rf /var/lib/apt/lists/*

# Set up virtual display
ENV DISPLAY=:99

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy startup script first and make it executable
COPY start.sh .
RUN chmod +x start.sh

# Copy rest of application code
COPY . .

# Expose port
EXPOSE 8000

# Use the startup script
CMD ["./start.sh"]
