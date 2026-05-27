#!/usr/bin/env bash
set -euo pipefail

: "${PROJECT_DIR:?PROJECT_DIR is required}"
: "${REPO_URL:?REPO_URL is required}"
BRANCH="${BRANCH:-main}"

if [ ! -d "$PROJECT_DIR/.git" ]; then
  mkdir -p "$PROJECT_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env created from .env.example. Fill it before restarting the stack."
fi

docker compose pull
docker compose up -d --build
