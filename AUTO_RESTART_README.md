# Docker Compose Auto-Restart Setup

This setup automatically restarts your Docker Compose services every 6 hours using `docker compose down -v` followed by `docker compose up --build -d`.

## Files Created

- `restart_services.sh` - Main script that handles the restart process
- `docker-restart.service` - Systemd service file
- `docker-restart.timer` - Systemd timer file  
- `setup_systemd.sh` - Setup script for systemd timer
- `restart.log` - Log file (created when script runs)

## Current Setup: Cron Job

A cron job has been configured to run every 6 hours:
```bash
0 */6 * * * /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape/restart_services.sh
```

### Cron Job Management

**View current cron jobs:**
```bash
crontab -l
```

**Edit cron jobs:**
```bash
crontab -e
```

**Remove the auto-restart cron job:**
```bash
crontab -r
```

## Alternative: Systemd Timer

If you prefer systemd timers over cron, you can use the provided systemd setup:

**Setup systemd timer:**
```bash
sudo ./setup_systemd.sh
```

**Check timer status:**
```bash
sudo systemctl status docker-restart.timer
```

**View timer list:**
```bash
sudo systemctl list-timers docker-restart.timer
```

**Stop systemd timer:**
```bash
sudo systemctl stop docker-restart.timer
sudo systemctl disable docker-restart.timer
```

## Testing the Setup

### 1. Test the restart script manually:
```bash
cd /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape
./restart_services.sh
```

### 2. Check the log file:
```bash
tail -f restart.log
```

### 3. Verify services are running:
```bash
docker compose ps
```

### 4. Test cron job (optional):
You can temporarily modify the cron job to run every minute for testing:
```bash
# Edit crontab
crontab -e

# Change the line to:
* * * * * /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape/restart_services.sh

# Remember to change it back to every 6 hours:
0 */6 * * * /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape/restart_services.sh
```

## What the Script Does

1. **Stops all services**: `docker compose down -v` (removes containers, networks, and volumes)
2. **Waits 5 seconds**: Allows cleanup to complete
3. **Rebuilds and starts**: `docker compose up --build -d` (rebuilds images and starts in detached mode)
4. **Logs everything**: All actions are logged to `restart.log`
5. **Shows status**: Displays running containers and recent logs

## Schedule Times

The cron job runs at:
- 00:00 (midnight)
- 06:00 (6 AM)
- 12:00 (noon)
- 18:00 (6 PM)

## Troubleshooting

### Check if cron is running:
```bash
sudo systemctl status cron
```

### Check cron logs:
```bash
sudo journalctl -u cron
```

### Check script permissions:
```bash
ls -la restart_services.sh
```

### Manual restart if needed:
```bash
cd /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape
./restart_services.sh
```

## Monitoring

- **Log file**: `restart.log` contains all restart activities
- **Docker status**: Use `docker compose ps` to check service status
- **Service logs**: Use `docker compose logs` to view application logs

## Important Notes

- The script removes volumes (`-v` flag), so any data stored in Docker volumes will be lost
- The script rebuilds images (`--build` flag), so it will pick up any code changes
- Services are started in detached mode (`-d` flag)
- All actions are logged with timestamps
- The script includes error handling and will exit if any step fails
