#!/bin/bash

# Start Redis in background
redis-server &

# Start Xvfb in background
Xvfb :99 -screen 0 1024x768x16 &

# Wait a moment for services to start
sleep 5

# Start the FastAPI application
uvicorn main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300
