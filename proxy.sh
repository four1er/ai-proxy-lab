#!/usr/bin/env bash
# ai-proxy-lab one-click manager (macOS / Mac mini first, Linux also works).
#
#   ./proxy.sh setup     one-click install & start (only OAuth logins stay manual)
#   ./proxy.sh login gemini|claude|codex
#   ./proxy.sh start|stop|restart|status|logs|test|key|backup|restore|update|tailscale|dataset
#
# Secrets boundary: this script NEVER uploads or prints your account tokens.
# The local API key is generated on your machine, written only into your local
# cliproxy/config.yaml (marked skip-worktree so it is not committed by accident).
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
CONFIG="cliproxy/config.yaml"
PLACEHOLDER="sk-local-change-me"
BACKUP_KEEP="${BACKUP_KEEP:-14}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose "$@"; }

data_dir() {
  if [ -f .env ] && grep -q '^AI_PROXY_DATA_DIR=' .env; then
    sed -n 's/^AI_PROXY_DATA_DIR=//p' .env | tail -1
  else
    echo "./data"
  fi
}

ensure_env_file() {
  if [ ! -f .env ]; then
    {
      echo "# Local runtime settings (not committed; .env is gitignored)."
      echo "AI_PROXY_DATA_DIR=${DATA_DIR:-./data}"
    } > .env
    say "Created .env (data dir: $(data_dir))"
  fi
  mkdir -p "$(data_dir)/raw_logs" "$(data_dir)/dataset" cliproxy/auths backups
}

ensure_api_key() {
  [ -f "$CONFIG" ] || die "$CONFIG missing — run: git checkout -- $CONFIG (or re-clone)"
  if grep -q "$PLACEHOLDER" "$CONFIG"; then
    local key
    if command -v openssl >/dev/null 2>&1; then
      key="sk-local-$(openssl rand -hex 24)"
    else
      key="sk-local-$(date +%s)-$$-$RANDOM$RANDOM"
    fi
    # perl in-place edit works the same on macOS and Linux (sed -i differs).
    KEY="$key" perl -i -pe 'BEGIN{$k=$ENV{KEY}} s/sk-local-change-me/$k/' "$CONFIG"
    say "Generated a local API key and wrote it into $CONFIG (this machine only)"
  fi
  # Keep the locally-generated key out of future commits on this clone.
  if git rev-parse --git-dir >/dev/null 2>&1; then
    git update-index --skip-worktree "$CONFIG" 2>/dev/null || true
  fi
}

local_key() {
  sed -n 's/^[[:space:]]*-[[:space:]]*"\(sk-[^"]*\)".*/\1/p' "$CONFIG" | head -1
}

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker not found."
    if [ "$(uname -s)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
      say "Installing Docker Desktop via Homebrew (may ask for your password)..."
      brew install --cask docker || die "Homebrew install failed — install Docker Desktop or OrbStack manually, then re-run ./proxy.sh setup"
    else
      die "Install Docker Desktop (macOS) or OrbStack first, start it, then re-run: ./proxy.sh setup"
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    if [ "$(uname -s)" = "Darwin" ]; then
      say "Docker daemon not running — trying to start Docker / OrbStack..."
      open -a Docker 2>/dev/null || open -a OrbStack 2>/dev/null || true
    fi
    for _ in $(seq 1 60); do
      docker info >/dev/null 2>&1 && break
      sleep 2
    done
    docker info >/dev/null 2>&1 || die "Docker daemon still not up. Start Docker Desktop/OrbStack manually, then re-run setup."
  fi
  docker compose version >/dev/null 2>&1 || die "'docker compose' v2 not available — update Docker Desktop/OrbStack."
  say "Docker OK: $(docker --version)"
}

wait_healthy() {
  say "Waiting for the proxy to become healthy..."
  for _ in $(seq 1 45); do
    if curl -fsS http://127.0.0.1:8400/healthz >/dev/null 2>&1; then
      say "Proxy is up: http://127.0.0.1:8400"
      return 0
    fi
    sleep 2
  done
  warn "Health check did not pass in time. Recent logs:"
  compose logs --tail 50 || true
  die "Start failed — fix the log errors above, then run ./proxy.sh setup again."
}

cmd_setup() {
  say "ai-proxy-lab setup on $(uname -s) $(uname -m) — data dir: ${DATA_DIR:-./data}"
  check_docker
  ensure_env_file
  ensure_api_key
  say "Pulling images and building the logger proxy..."
  compose pull cliproxyapi || warn "Image pull failed (network?) — will try to use a local image"
  compose build logger-proxy
  compose up -d
  wait_healthy
  cmd_test || true
  cat <<EOF

Done. Only the account logins are manual (OAuth must happen in your browser):

  ./proxy.sh login gemini     # Gemini
  ./proxy.sh login claude     # Claude
  ./proxy.sh login codex      # Codex / ChatGPT
  (repeat a login to add more accounts of the same provider — they rotate round-robin)

Your clients connect to:  http://127.0.0.1:8400
Your local API key:       ./proxy.sh key
From your other devices (private, via Tailscale):  ./proxy.sh tailscale

Mac mini always-on checklist (one-time, in System Settings):
  - Energy: prevent automatic sleeping / start up automatically after a power failure
  - Keep this Mac logged in and Docker Desktop/OrbStack set to start at login
EOF
}

cmd_login() {
  local provider="${1:-}"
  check_docker
  case "$provider" in
    gemini) compose run --rm cliproxyapi ./CLIProxyAPI --login ;;
    claude) compose run --rm cliproxyapi ./CLIProxyAPI --claude-login ;;
    codex)  compose run --rm cliproxyapi ./CLIProxyAPI --codex-login ;;
    *) die "Usage: ./proxy.sh login gemini|claude|codex" ;;
  esac
  say "Login saved under cliproxy/auths/ (gitignored). Restarting the pool..."
  compose up -d cliproxyapi
}

cmd_test() {
  say "Health: $(curl -fsS http://127.0.0.1:8400/healthz || echo 'unreachable')"
  local key; key="$(local_key)"
  if [ -n "$key" ]; then
    say "Models endpoint (via logger proxy):"
    curl -fsS -H "Authorization: Bearer $key" http://127.0.0.1:8400/v1/models | head -c 500 || warn "models call failed — have you run ./proxy.sh login ... yet?"
    echo
  fi
}

cmd_backup() {
  local stamp file
  stamp="$(date +%Y%m%d-%H%M%S)"
  file="backups/backup-${stamp}.tar.gz"
  mkdir -p backups
  say "Backing up config + account logins + raw logs -> $file"
  warn "This archive contains account tokens and your prompts. Keep it private, never share it."
  tar -czf "$file" "$CONFIG" cliproxy/auths "$(data_dir)/raw_logs" 2>/dev/null || \
    tar -czf "$file" "$CONFIG" cliproxy/auths
  chmod 600 "$file"
  # Retention: keep the newest $BACKUP_KEEP archives.
  ls -t backups/backup-*.tar.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do rm -f "$old"; done
  say "Backup done: $file ($(du -h "$file" | cut -f1))"
}

cmd_restore() {
  local file="${1:-}"
  [ -n "$file" ] && [ -f "$file" ] || die "Usage: ./proxy.sh restore backups/backup-YYYYMMDD-HHMMSS.tar.gz"
  printf 'Restore %s? This overwrites local config, logins and raw logs. [y/N] ' "$file"
  read -r ans
  case "$ans" in y|Y) ;; *) die "Aborted." ;; esac
  compose down || true
  tar -xzf "$file" -C "$ROOT"
  compose up -d
  wait_healthy
  say "Restored from $file"
}

cmd_tailscale() {
  command -v tailscale >/dev/null 2>&1 || die "Tailscale CLI not found. Install Tailscale on this Mac and your other devices first."
  say "Exposing http://127.0.0.1:8400 to your tailnet only (not the public internet)..."
  tailscale serve --bg http://127.0.0.1:8400
  tailscale serve status || true
  say "Use the https URL above as the base URL on your other devices, with the same local API key."
}

cmd_update() {
  if git rev-parse --git-dir >/dev/null 2>&1; then
    say "Updating code (your local config.yaml is skip-worktree and will not be touched)..."
    git pull --ff-only || warn "git pull failed — continuing with the local code"
  fi
  check_docker
  compose pull cliproxyapi || true
  compose build logger-proxy
  compose up -d
  wait_healthy
}

case "${1:-help}" in
  setup)   cmd_setup ;;
  login)   shift; cmd_login "${1:-}" ;;
  start)   check_docker; compose up -d; wait_healthy ;;
  stop)    compose down ;;
  restart) compose down; check_docker; compose up -d; wait_healthy ;;
  status)  compose ps; echo; curl -fsS http://127.0.0.1:8400/healthz || echo "healthz: unreachable"; echo; du -sh "$(data_dir)" cliproxy/auths backups 2>/dev/null || true ;;
  logs)    shift; compose logs -f --tail 200 "${@}" ;;
  test)    cmd_test ;;
  key)     local_key ;;
  backup)  cmd_backup ;;
  restore) shift; cmd_restore "${1:-}" ;;
  update)  cmd_update ;;
  tailscale) cmd_tailscale ;;
  dataset) python3 scripts/clean_dataset.py --out "$(data_dir)/dataset/train.jsonl" ;;
  help|-h|--help|*)
    sed -n '2,10p' "$0" ;;
esac
