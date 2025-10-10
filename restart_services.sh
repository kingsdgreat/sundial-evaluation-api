#!/bin/bash

# Docker Compose Auto-Restart Script
# This script stops all services with volumes, then rebuilds and starts them

# Set the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Log file for tracking restarts
LOG_FILE="$SCRIPT_DIR/restart.log"

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "Starting Docker Compose restart process..."

# Check if docker-compose.yml exists
if [ ! -f "docker-compose.yml" ]; then
    log "ERROR: docker-compose.yml not found in $SCRIPT_DIR"
    exit 1
fi

# Stop and remove containers, networks, and volumes
log "Stopping and removing containers, networks, and volumes..."
if docker compose down -v; then
    log "Successfully stopped and removed containers and volumes"
else
    log "ERROR: Failed to stop containers and volumes"
    exit 1
fi

# Wait a moment for cleanup
sleep 5

# Rebuild and start services in detached mode
log "Rebuilding and starting services..."
if docker compose up --build -d; then
    log "Successfully rebuilt and started services"
    
    # Show running containers
    log "Current running containers:"
    docker compose ps | tee -a "$LOG_FILE"
    
    # Show logs for a few seconds to verify startup
    log "Recent logs from services:"
    timeout 10s docker compose logs --tail=20 | tee -a "$LOG_FILE" || true
    
else
    log "ERROR: Failed to rebuild and start services"
    exit 1
fi

log "Docker Compose restart process completed successfully"
