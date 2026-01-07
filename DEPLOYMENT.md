# Discharge Summary App - Hostinger VPS Deployment Guide

## Prerequisites

- A Hostinger VPS with Ubuntu 20.04/22.04 or Debian 11/12
- SSH access to your VPS
- A domain name (optional, but recommended)
- Your Gemini API key

---

## Quick Deployment (Automated)

### Step 1: Upload Project to VPS

From your local machine, upload the project files:

```bash
# Using SCP (replace with your VPS IP)
scp -r "D:\dsumarry - Copy\*" root@YOUR_VPS_IP:/tmp/dsumarry/
```

Or use FileZilla/WinSCP to upload files to `/tmp/dsumarry/`

### Step 2: Run Deployment Script

SSH into your VPS and run:

```bash
ssh root@YOUR_VPS_IP
cd /tmp/dsumarry
chmod +x deploy.sh
sudo ./deploy.sh
```

### Step 3: Configure Environment Variables

```bash
sudo nano /var/www/dsumarry/.env
```

Update these values:
```
SECRET_KEY=generate-a-random-32-character-string
GEMINI_API_KEY=your-actual-gemini-api-key
```

To generate a secure secret key:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Step 4: Update Domain in Nginx

```bash
sudo nano /etc/nginx/sites-available/dsumarry
```

Replace `your-domain.com` with your actual domain.

### Step 5: Restart Services

```bash
sudo systemctl restart dsumarry
sudo systemctl reload nginx
```

---

## Manual Deployment (Step-by-Step)

### 1. System Update & Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv nginx
```

### 2. Create Application Directory

```bash
sudo mkdir -p /var/www/dsumarry
sudo chown -R $USER:$USER /var/www/dsumarry
```

### 3. Upload Files

Upload all project files to `/var/www/dsumarry/`:
- main.py
- requirements.txt
- template.docx
- templates/ folder
- env.example

### 4. Setup Python Environment

```bash
cd /var/www/dsumarry
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Configure Environment Variables

```bash
cp env.example .env
nano .env
```

Add your values:
```
SECRET_KEY=your-secret-key-here
GEMINI_API_KEY=your-gemini-api-key
PORT=8000
FLASK_ENV=production
```

Secure the file:
```bash
chmod 600 .env
sudo chown www-data:www-data .env
```

### 6. Setup Systemd Service

```bash
sudo cp dsumarry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dsumarry
sudo systemctl start dsumarry
```

### 7. Configure Nginx

```bash
sudo cp nginx.conf /etc/nginx/sites-available/dsumarry
sudo ln -s /etc/nginx/sites-available/dsumarry /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 8. Configure Firewall

```bash
sudo ufw allow 'Nginx Full'
sudo ufw allow OpenSSH
sudo ufw enable
```

---

## SSL/HTTPS Setup (Recommended)

### Install Certbot

```bash
sudo apt install certbot python3-certbot-nginx
```

### Get SSL Certificate

```bash
sudo certbot --nginx -d your-domain.com -d www.your-domain.com
```

### Auto-Renewal (already configured by certbot)

Test renewal:
```bash
sudo certbot renew --dry-run
```

---

## Useful Commands

### Application Management

```bash
# Check app status
sudo systemctl status dsumarry

# Restart app
sudo systemctl restart dsumarry

# Stop app
sudo systemctl stop dsumarry

# View logs (live)
sudo journalctl -u dsumarry -f

# View last 100 log lines
sudo journalctl -u dsumarry -n 100
```

### Nginx Management

```bash
# Test config
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx

# View access logs
sudo tail -f /var/log/nginx/access.log

# View error logs
sudo tail -f /var/log/nginx/error.log
```

### Updating the Application

```bash
cd /var/www/dsumarry

# Activate virtual environment
source venv/bin/activate

# Pull new files (if using git)
git pull

# Or upload new files via SCP/SFTP

# Install new dependencies (if any)
pip install -r requirements.txt

# Restart the app
sudo systemctl restart dsumarry
```

---

## Troubleshooting

### App won't start

1. Check logs:
   ```bash
   sudo journalctl -u dsumarry -n 50
   ```

2. Verify .env file exists and has correct permissions:
   ```bash
   ls -la /var/www/dsumarry/.env
   ```

3. Test manually:
   ```bash
   cd /var/www/dsumarry
   source venv/bin/activate
   python main.py
   ```

### 502 Bad Gateway

1. Check if app is running:
   ```bash
   sudo systemctl status dsumarry
   ```

2. Check if port 8000 is listening:
   ```bash
   sudo netstat -tlnp | grep 8000
   ```

3. Restart the app:
   ```bash
   sudo systemctl restart dsumarry
   ```

### Permission Issues

```bash
sudo chown -R www-data:www-data /var/www/dsumarry
sudo chmod -R 755 /var/www/dsumarry
sudo chmod 600 /var/www/dsumarry/.env
```

### Gemini API Errors

1. Verify API key in .env file
2. Check if key has quota remaining
3. View app logs for specific errors

---

## Security Recommendations

1. **Keep system updated:**
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```

2. **Use strong passwords** for SSH and any admin interfaces

3. **Disable root SSH login:**
   ```bash
   sudo nano /etc/ssh/sshd_config
   # Set: PermitRootLogin no
   sudo systemctl restart sshd
   ```

4. **Setup fail2ban:**
   ```bash
   sudo apt install fail2ban
   sudo systemctl enable fail2ban
   ```

5. **Regular backups** of your .env and template.docx files

---

## Support

If you encounter issues:
1. Check the logs: `sudo journalctl -u dsumarry -f`
2. Verify all configuration files are correct
3. Ensure your Gemini API key is valid and has quota


