# Deployment Guide

## GitHub preparation

1. Keep the real secrets in `.env` only.
2. Commit `.env.example` instead of `.env`.
3. Push the repository to GitHub.

## Ubuntu server prerequisites

Install these once on the server:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in after adding your user to the `docker` group.

## First server setup

1. Create the target directory, for example `/opt/vma`.
2. Copy this repo there once or let the workflow clone it automatically.
3. Create `/opt/vma/.env` from `.env.example` and fill in the real values.

## GitHub Actions secrets

Add these repository secrets before enabling CD:

- `SSH_HOST`
- `SSH_PORT`
- `SSH_USER`
- `SSH_PRIVATE_KEY`
- `PROJECT_DIR` example: `/opt/vma`
- `REPO_URL` example: `git@github.com:your-user/your-repo.git`

## Deployment flow

- Every pull request and push runs `.github/workflows/ci.yml`.
- A successful push to `main` triggers `.github/workflows/deploy.yml`.
- The deploy job SSHes into Ubuntu and runs `deploy/deploy.sh`.

## Manual server deploy

```bash
cd /opt/vma
bash deploy/deploy.sh
```
