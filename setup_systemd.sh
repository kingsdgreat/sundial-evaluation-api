#!/bin/bash

# Setup script for systemd timer (alternative to cron)
# Run this script with sudo to set up systemd timer

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Setting up systemd timer for Docker Compose auto-restart..."

# Copy service and timer files to systemd directory
sudo cp "$SCRIPT_DIR/docker-restart.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/docker-restart.timer" /etc/systemd/system/

# Reload systemd daemon
sudo systemctl daemon-reload

# Enable and start the timer
sudo systemctl enable docker-restart.timer
sudo systemctl start docker-restart.timer

echo "Systemd timer setup complete!"
echo ""
echo "To check timer status:"
echo "  sudo systemctl status docker-restart.timer"
echo ""
echo "To check service status:"
echo "  sudo systemctl status docker-restart.service"
echo ""
echo "To view timer list:"
echo "  sudo systemctl list-timers docker-restart.timer"
echo ""
echo "To stop the timer:"
echo "  sudo systemctl stop docker-restart.timer"
echo "  sudo systemctl disable docker-restart.timer"
