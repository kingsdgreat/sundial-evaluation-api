#!/bin/bash

# Generate a unique display number based on container hostname or random number
DISPLAY_NUM=${HOSTNAME##*-}  # Extract number from hostname like app-1, app-2, app-3
if [[ ! "$DISPLAY_NUM" =~ ^[0-9]+$ ]]; then
    # Fallback to random number if hostname doesn't end with number
    DISPLAY_NUM=$((RANDOM % 900 + 100))  # Random number between 100-999
fi

echo "Starting Xvfb on display :$DISPLAY_NUM"

# Start Xvfb with unique display number
Xvfb :$DISPLAY_NUM -screen 0 1024x768x16 &

# Set DISPLAY environment variable
export DISPLAY=:$DISPLAY_NUM

# Wait a moment for services to start
sleep 5

# Start the FastAPI application
uvicorn main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300
