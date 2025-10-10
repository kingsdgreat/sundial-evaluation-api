# VPS Deployment Guide - Auto-Restart Setup

This guide shows you how to deploy your Docker Compose application with automatic 6-hour restarts to your VPS.

## 🚀 **Step 1: Upload Your Project to VPS**

### Option A: Using SCP/SFTP
```bash
# From your local machine, upload the entire project
scp -r /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape/ user@your-vps-ip:/home/user/

# Or use rsync for better efficiency
rsync -avz /home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape/ user@your-vps-ip:/home/user/realstate-scrape/
```

### Option B: Using Git (Recommended)
```bash
# On your VPS
git clone <your-repository-url>
cd realstate-scrape
```

## 🔧 **Step 2: Set Up Auto-Restart on VPS**

### 1. Make the restart script executable
```bash
# SSH into your VPS
ssh user@your-vps-ip

# Navigate to your project directory
cd /path/to/your/project

# Make the script executable
chmod +x restart_services.sh
```

### 2. Test the script manually first
```bash
# Test the restart script
./restart_services.sh

# Verify services are running
docker compose ps
```

### 3. Set up the cron job
```bash
# Add the cron job (replace with your actual project path)
echo "0 */6 * * * /path/to/your/project/restart_services.sh" | crontab -

# Verify the cron job was added
crontab -l
```

## 🐳 **Step 3: Docker Setup on VPS**

### Install Docker and Docker Compose (if not already installed)
```bash
# Update system
sudo apt update

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add your user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose-plugin

# Log out and back in for group changes to take effect
exit
# SSH back in
```

### Verify Docker installation
```bash
docker --version
docker compose version
```

## 🔒 **Step 4: Security Considerations**

### 1. Firewall Setup
```bash
# Allow only necessary ports
sudo ufw allow 22    # SSH
sudo ufw allow 8000  # Your app port
sudo ufw enable
```

### 2. Environment Variables
Create a `.env` file for sensitive data:
```bash
# Create .env file
nano .env

# Add your environment variables
REDIS_URL=redis://redis:6379
# Add other sensitive configs here
```

### 3. Update docker-compose.yml to use .env
```yaml
services:
  app:
    build: .
    env_file:
      - .env
    # ... rest of config
```

## 📊 **Step 5: Monitoring and Logs**

### 1. Set up log rotation
```bash
# Create logrotate config
sudo nano /etc/logrotate.d/docker-restart

# Add this content:
/path/to/your/project/restart.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
}
```

### 2. Monitor your application
```bash
# Check if services are running
docker compose ps

# View application logs
docker compose logs -f

# View restart logs
tail -f restart.log

# Check cron job execution
sudo journalctl -u cron
```

## 🔄 **Step 6: Alternative - Systemd Timer (More Robust)**

If you prefer systemd over cron on your VPS:

```bash
# Copy systemd files to your project
# (These are already created in your local project)

# Set up systemd timer
sudo ./setup_systemd.sh

# Check timer status
sudo systemctl status docker-restart.timer

# View timer list
sudo systemctl list-timers docker-restart.timer
```

## 🚨 **Step 7: Troubleshooting Commands**

### Check if cron is running
```bash
sudo systemctl status cron
```

### Check cron logs
```bash
sudo journalctl -u cron -f
```

### Manual restart if needed
```bash
cd /path/to/your/project
./restart_services.sh
```

### Check Docker daemon
```bash
sudo systemctl status docker
```

## 📋 **Step 8: Complete VPS Setup Checklist**

- [ ] Project uploaded to VPS
- [ ] Docker and Docker Compose installed
- [ ] `restart_services.sh` is executable
- [ ] Script tested manually
- [ ] Cron job configured
- [ ] Firewall configured
- [ ] Environment variables set up
- [ ] Log rotation configured
- [ ] Monitoring in place

## 🌐 **Step 9: Access Your Application**

Once deployed, your application will be accessible at:
```
http://your-vps-ip:8000
```

## 🔧 **Step 10: Maintenance Commands**

### Update your application
```bash
# Pull latest changes
git pull

# The cron job will automatically rebuild on next restart
# Or manually restart:
./restart_services.sh
```

### Check system resources
```bash
# Check disk space
df -h

# Check memory usage
free -h

# Check running processes
htop
```

## 📞 **Support Commands**

### Emergency stop
```bash
docker compose down
```

### Emergency start
```bash
docker compose up -d
```

### View all logs
```bash
docker compose logs --tail=100
```

## 🎯 **Key Differences from Local Setup**

1. **Path changes**: Update all paths to match your VPS directory structure
2. **User permissions**: Ensure your VPS user has Docker permissions
3. **Firewall**: Configure VPS firewall to allow necessary ports
4. **Monitoring**: Set up proper monitoring for production environment
5. **Backup**: Consider setting up automated backups of your data

Your application will automatically restart every 6 hours on the VPS, just like on your local machine!
