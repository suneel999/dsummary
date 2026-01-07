#!/bin/bash

# ==============================================
# Discharge Summary App - VPS Deployment Script
# For Hostinger VPS (Ubuntu/Debian)
# ==============================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Discharge Summary App Deployment${NC}"
echo -e "${GREEN}========================================${NC}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Please run as root (use sudo)${NC}"
    exit 1
fi

# Variables
APP_DIR="/var/www/dsumarry"
APP_USER="www-data"

# Step 1: Update system
echo -e "${YELLOW}[1/8] Updating system packages...${NC}"
apt update && apt upgrade -y

# Step 2: Install required packages
echo -e "${YELLOW}[2/8] Installing required packages...${NC}"
apt install -y python3 python3-pip python3-venv nginx git

# Step 3: Clone from GitHub
echo -e "${YELLOW}[3/8] Cloning application from GitHub...${NC}"
if [ -d "$APP_DIR" ]; then
    rm -rf $APP_DIR
fi
git clone https://github.com/suneel999/dsummary.git $APP_DIR
chown -R $APP_USER:$APP_USER $APP_DIR

# Step 4: Create virtual environment and install dependencies
echo -e "${YELLOW}[4/8] Setting up Python virtual environment...${NC}"
cd $APP_DIR
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Step 5: Setup environment file
echo -e "${YELLOW}[5/8] Setting up environment variables...${NC}"
if [ ! -f "$APP_DIR/.env" ]; then
    cp $APP_DIR/env.example $APP_DIR/.env
    echo -e "${RED}IMPORTANT: Edit /var/www/dsumarry/.env and add your GEMINI_API_KEY and SECRET_KEY${NC}"
fi
chmod 600 $APP_DIR/.env

# Step 6: Setup systemd service
echo -e "${YELLOW}[6/8] Setting up systemd service...${NC}"
cp $APP_DIR/dsumarry.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable dsumarry
systemctl start dsumarry

# Step 7: Setup nginx
echo -e "${YELLOW}[7/8] Setting up nginx...${NC}"
cp $APP_DIR/nginx.conf /etc/nginx/sites-available/dsumarry
ln -sf /etc/nginx/sites-available/dsumarry /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# Step 8: Setup firewall
echo -e "${YELLOW}[8/8] Configuring firewall...${NC}"
ufw allow 'Nginx Full'
ufw allow OpenSSH
ufw --force enable

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "1. Edit /var/www/dsumarry/.env with your API keys:"
echo "   sudo nano /var/www/dsumarry/.env"
echo ""
echo "2. Update nginx.conf with your domain name:"
echo "   sudo nano /etc/nginx/sites-available/dsumarry"
echo ""
echo "3. Restart the service after editing .env:"
echo "   sudo systemctl restart dsumarry"
echo ""
echo "4. For SSL/HTTPS (recommended), run:"
echo "   sudo apt install certbot python3-certbot-nginx"
echo "   sudo certbot --nginx -d your-domain.com"
echo ""
echo -e "${GREEN}Your app should be accessible at: http://YOUR_VPS_IP${NC}"
echo ""
echo "Useful commands:"
echo "  - View logs: sudo journalctl -u dsumarry -f"
echo "  - Restart app: sudo systemctl restart dsumarry"
echo "  - Check status: sudo systemctl status dsumarry"

