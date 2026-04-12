# AWS Deployment Guide

This app is now ready for AWS with two practical options:

1. `EC2 + Nginx + systemd`
   Best if you want the simplest lift from your current VPS-style setup.
2. `Docker + App Runner`
   Best if you want a cleaner managed deployment with less server maintenance.

## Environment Variables

Set these in AWS instead of hardcoding them:

```env
SECRET_KEY=generate-a-random-64-char-value
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4.1
PORT=8000
MAX_UPLOAD_MB=16
GUNICORN_WORKERS=3
GUNICORN_TIMEOUT=180
FLASK_ENV=production
```

The app exposes a health check at `/health`.

---

## Option 1: Deploy on AWS EC2

### 1. Launch an EC2 instance

- Use Ubuntu 22.04 LTS
- Open security group ports `22`, `80`, and `443`
- Attach an Elastic IP if this will be public

### 2. Install system packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv nginx git
```

### 3. Copy the app to the server

```bash
sudo mkdir -p /var/www/dsumarry
sudo chown -R $USER:$USER /var/www/dsumarry
cd /var/www/dsumarry
git clone YOUR_REPO_URL .
```

### 4. Create the Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Create the runtime config

```bash
cp env.example .env
nano .env
```

Add your real values, then lock it down:

```bash
chmod 600 .env
sudo chown www-data:www-data .env
```

### 6. Install systemd and Nginx config

```bash
sudo cp dsumarry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dsumarry
sudo systemctl start dsumarry

sudo cp nginx.conf /etc/nginx/sites-available/dsumarry
sudo ln -sf /etc/nginx/sites-available/dsumarry /etc/nginx/sites-enabled/dsumarry
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 7. Add HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com -d www.your-domain.com
```

### 8. Useful checks

```bash
curl http://127.0.0.1:8000/health
sudo systemctl status dsumarry
sudo journalctl -u dsumarry -n 100
```

---

## Option 2: Deploy with Docker on AWS App Runner

### 1. Build the container locally

```bash
docker build -t dsumarry .
```

### 2. Create an ECR repository

```bash
aws ecr create-repository --repository-name dsumarry
```

### 3. Log in to ECR and push the image

Replace `REGION` and `ACCOUNT_ID` with your values:

```bash
aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com
docker tag dsumarry:latest ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/dsumarry:latest
docker push ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/dsumarry:latest
```

### 4. Create the App Runner service

- Source: `Amazon ECR`
- Image: `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/dsumarry:latest`
- Port: `8000`
- Health check path: `/health`

### 5. Set runtime environment variables in App Runner

- `SECRET_KEY`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `PORT=8000`
- `MAX_UPLOAD_MB=16`
- `GUNICORN_WORKERS=3`
- `GUNICORN_TIMEOUT=180`
- `FLASK_ENV=production`

For production, store `OPENAI_API_KEY` and `SECRET_KEY` in AWS Secrets Manager and inject them into App Runner instead of typing them manually.

---

## Recommendation

If you want the least risky path, start with `EC2 + Nginx + systemd` because it matches the files already in this repo.

If you want easier long-term ops, use `Docker + App Runner`.
