#!/usr/bin/env bash
# Self-heal the Comgu demo host.
#
# Runs every 5 minutes. Deliberately conservative — a watchdog that restarts
# eagerly turns a slow start into a restart loop and is worse than no watchdog:
#
#   * only acts after two consecutive failures, so a single slow response or a
#     service still booting is ignored
#   * at most one restart per service per 30 minutes
#   * checks DataHub before Comgu, because Comgu failing is usually a symptom of
#     DataHub being down, and restarting Comgu would not fix that
#
# Install:  sudo cp infra/watchdog.{sh,service,timer} … ; see infra/README.md
set -uo pipefail

STATE=/var/lib/comgu-watchdog
mkdir -p "$STATE"
LOG=/var/log/comgu-watchdog.log
COOLDOWN=1800

log() { echo "[$(date -Is)] $*" >> "$LOG"; }

# Returns 0 when a restart is allowed (not attempted in the last COOLDOWN).
cooled_down() {
  local f="$STATE/$1.last"
  [ -f "$f" ] || return 0
  local last now
  last=$(cat "$f" 2>/dev/null || echo 0)
  now=$(date +%s)
  [ $(( now - last )) -ge "$COOLDOWN" ]
}

mark_restart() { date +%s > "$STATE/$1.last"; }

# Two consecutive failures before acting.
failed_twice() {
  # Declared separately: under `set -u`, a later assignment in the same `local`
  # statement cannot reference an earlier one.
  local name="$1"
  local ok="$2"
  local f="$STATE/$name.fails"
  local n
  if [ "$ok" = "yes" ]; then echo 0 > "$f"; return 1; fi
  n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$f"
  [ "$n" -ge 2 ]
}

# --- DataHub first: Comgu depends on it ---------------------------------------

DH=no
curl -fsS --max-time 10 http://localhost:8080/config >/dev/null 2>&1 && DH=yes

if failed_twice datahub "$DH"; then
  if cooled_down datahub; then
    log "DataHub unreachable twice — restarting containers"
    mark_restart datahub
    for c in $(docker ps -a --format '{{.Names}}' | grep -E 'datahub-(mysql|kafka|opensearch|datahub-gms|frontend|datahub-actions)'); do
      docker start "$c" >/dev/null 2>&1
    done
  else
    log "DataHub still down but within cooldown — leaving it alone"
  fi
fi

# --- Comgu --------------------------------------------------------------------

APP=no
curl -fsS --max-time 10 http://127.0.0.1:8000/health >/dev/null 2>&1 && APP=yes

if failed_twice comgu "$APP"; then
  if cooled_down comgu; then
    log "Comgu unhealthy twice — restarting service"
    mark_restart comgu
    systemctl restart comgu
  else
    log "Comgu still unhealthy but within cooldown — leaving it alone"
  fi
fi

# --- Caddy --------------------------------------------------------------------

if ! systemctl is-active --quiet caddy; then
  if cooled_down caddy; then
    log "Caddy not active — restarting"
    mark_restart caddy
    systemctl restart caddy
  fi
fi

# Keep the log from growing without bound.
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ] && tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
