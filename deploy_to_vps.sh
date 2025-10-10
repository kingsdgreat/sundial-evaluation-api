#!/bin/bash

# VPS Deployment Script
# This script helps you deploy your Docker Compose app with auto-restart to a VPS

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Get VPS details
echo "🚀 VPS Deployment Setup"
echo "======================="
echo ""

read -p "Enter your VPS IP address: " VPS_IP
read -p "Enter your VPS username: " VPS_USER
read -p "Enter your VPS project directory (e.g., /home/user/realstate-scrape): " VPS_PATH

echo ""
print_status "Setting up deployment to $VPS_USER@$VPS_IP:$VPS_PATH"

# Create deployment package
print_status "Creating deployment package..."
tar -czf deployment.tar.gz \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='env' \
    --exclude='screenshots' \
    --exclude='restart.log' \
    .

print_success "Deployment package created: deployment.tar.gz"

# Create VPS setup script
cat > vps_setup.sh << 'EOF'
#!/bin/bash

# VPS Setup Script - Run this on your VPS after uploading files

set -e

echo "🔧 Setting up Docker Compose with Auto-Restart on VPS"
echo "====================================================="

# Get current directory
PROJECT_DIR=$(pwd)
echo "Project directory: $PROJECT_DIR"

# Make restart script executable
chmod +x restart_services.sh
echo "✅ Made restart_services.sh executable"

# Test Docker Compose
echo "🐳 Testing Docker Compose..."
if docker compose version > /dev/null 2>&1; then
    echo "✅ Docker Compose is available"
else
    echo "❌ Docker Compose not found. Installing..."
    sudo apt update
    sudo apt install -y docker-compose-plugin
fi

# Test the restart script
echo "🧪 Testing restart script..."
if ./restart_services.sh; then
    echo "✅ Restart script works correctly"
else
    echo "❌ Restart script failed. Please check the logs."
    exit 1
fi

# Set up cron job
echo "⏰ Setting up cron job..."
CRON_JOB="0 */6 * * * $PROJECT_DIR/restart_services.sh"
echo "$CRON_JOB" | crontab -
echo "✅ Cron job configured: $CRON_JOB"

# Verify cron job
echo "📋 Current cron jobs:"
crontab -l

# Set up log rotation
echo "📊 Setting up log rotation..."
sudo tee /etc/logrotate.d/docker-restart > /dev/null << EOL
$PROJECT_DIR/restart.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
}
EOL
echo "✅ Log rotation configured"

# Check firewall
echo "🔥 Checking firewall..."
if command -v ufw > /dev/null 2>&1; then
    if ufw status | grep -q "Status: active"; then
        echo "⚠️  UFW is active. Make sure port 8000 is allowed:"
        echo "   sudo ufw allow 8000"
    else
        echo "ℹ️  UFW is not active"
    fi
else
    echo "ℹ️  UFW not installed"
fi

echo ""
echo "🎉 VPS Setup Complete!"
echo "======================"
echo ""
echo "Your application should be running at: http://$(curl -s ifconfig.me):8000"
echo ""
echo "Useful commands:"
echo "  Check services:    docker compose ps"
echo "  View logs:         docker compose logs -f"
echo "  View restart log:  tail -f restart.log"
echo "  Manual restart:    ./restart_services.sh"
echo "  Check cron:        crontab -l"
echo ""
EOF

chmod +x vps_setup.sh
print_success "VPS setup script created: vps_setup.sh"

# Create upload instructions
cat > upload_instructions.txt << EOF
📤 Upload Instructions
=====================

1. Upload files to your VPS:
   scp deployment.tar.gz vps_setup.sh $VPS_USER@$VPS_IP:~/

2. SSH into your VPS:
   ssh $VPS_USER@$VPS_IP

3. Extract and setup:
   mkdir -p $VPS_PATH
   cd $VPS_PATH
   tar -xzf ~/deployment.tar.gz
   chmod +x ~/vps_setup.sh
   ~/vps_setup.sh

4. Clean up:
   rm ~/deployment.tar.gz ~/vps_setup.sh

Your app will be available at: http://$VPS_IP:8000
Auto-restart will run every 6 hours automatically!
EOF

print_success "Upload instructions created: upload_instructions.txt"

echo ""
echo "🎯 Next Steps:"
echo "=============="
echo "1. Upload files: scp deployment.tar.gz vps_setup.sh $VPS_USER@$VPS_IP:~/"
echo "2. SSH to VPS: ssh $VPS_USER@$VPS_IP"
echo "3. Run setup: mkdir -p $VPS_PATH && cd $VPS_PATH && tar -xzf ~/deployment.tar.gz && ~/vps_setup.sh"
echo ""
echo "📋 Files created:"
echo "  - deployment.tar.gz (your project files)"
echo "  - vps_setup.sh (VPS setup script)"
echo "  - upload_instructions.txt (step-by-step guide)"
echo ""
print_success "Deployment package ready! Follow upload_instructions.txt"
