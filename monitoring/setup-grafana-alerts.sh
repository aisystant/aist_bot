#!/usr/bin/env bash
# Setup Grafana Cloud alerting for Aist Bot (WP-45)
#
# Prerequisites:
#   1. Grafana Cloud instance with PostgreSQL datasource (DS_NEON) connected
#   2. Service account token with Editor role
#   3. Dashboard "aist-bot-errors" already imported
#
# Usage:
#   export GRAFANA_URL="https://your-org.grafana.net"
#   export GRAFANA_TOKEN="glsa_..."
#   export GRAFANA_DS_UID="<datasource-uid>"  # PostgreSQL datasource UID
#   export TG_BOT_TOKEN="<bot-token>"         # For Telegram contact point
#   export TG_CHAT_ID="<chat-id>"             # Developer chat ID
#   bash monitoring/setup-grafana-alerts.sh
#
# What it creates:
#   1. Telegram contact point (redundant channel, independent of bot process)
#   2. Notification policy routing to Telegram
#   3. Alert rules:
#      - L3+ Critical Errors (every 5 min, fires immediately)
#      - Unknown Error Spike (every 15 min, fires after 5 min)
#      - Error Rate Anomaly (every 15 min, >50 errors/hour)
#      - Bot Heartbeat (every 1h, no errors in 24h = bot may be down)

set -euo pipefail

: "${GRAFANA_URL:?Set GRAFANA_URL (e.g. https://your-org.grafana.net)}"
: "${GRAFANA_TOKEN:?Set GRAFANA_TOKEN (service account token)}"
: "${GRAFANA_DS_UID:?Set GRAFANA_DS_UID (PostgreSQL datasource UID)}"
: "${TG_BOT_TOKEN:?Set TG_BOT_TOKEN (Telegram bot token)}"
: "${TG_CHAT_ID:?Set TG_CHAT_ID (developer chat ID)}"

API="${GRAFANA_URL}/api"
AUTH="Authorization: Bearer ${GRAFANA_TOKEN}"
CT="Content-Type: application/json"

echo "=== Grafana Alerting Setup for Aist Bot ==="

# --- Step 1: Create Telegram contact point ---
echo "[1/4] Creating Telegram contact point..."
CONTACT_POINT=$(cat <<EOF
{
  "name": "aist-bot-telegram",
  "type": "telegram",
  "settings": {
    "bottoken": "${TG_BOT_TOKEN}",
    "chatid": "${TG_CHAT_ID}",
    "parse_mode": "HTML",
    "disable_web_page_preview": true,
    "message": "{{ if gt (len .Alerts.Firing) 0 }}🚨 <b>GRAFANA ALERT</b>\n{{ range .Alerts.Firing }}<b>{{ .Labels.alertname }}</b>: {{ .Annotations.summary }}\n{{ end }}{{ end }}{{ if gt (len .Alerts.Resolved) 0 }}✅ <b>RESOLVED</b>\n{{ range .Alerts.Resolved }}<b>{{ .Labels.alertname }}</b>{{ end }}{{ end }}"
  },
  "disableResolveMessage": false
}
EOF
)

RESULT=$(curl -s -w "\n%{http_code}" -X POST "${API}/v1/provisioning/contact-points" \
  -H "${AUTH}" -H "${CT}" -d "${CONTACT_POINT}")
HTTP_CODE=$(echo "$RESULT" | tail -1)
BODY=$(echo "$RESULT" | sed '$d')

if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
  echo "  ✅ Contact point created"
else
  echo "  ⚠️  HTTP ${HTTP_CODE}: ${BODY}"
  echo "  (May already exist — continuing)"
fi

# --- Step 2: Create notification policy ---
echo "[2/4] Updating notification policy..."
POLICY=$(cat <<EOF
{
  "receiver": "aist-bot-telegram",
  "group_by": ["alertname"],
  "group_wait": "30s",
  "group_interval": "5m",
  "repeat_interval": "4h",
  "routes": [
    {
      "receiver": "aist-bot-telegram",
      "matchers": ["bot=aist"],
      "group_wait": "10s",
      "repeat_interval": "1h",
      "continue": false
    }
  ]
}
EOF
)

RESULT=$(curl -s -w "\n%{http_code}" -X PUT "${API}/v1/provisioning/policies" \
  -H "${AUTH}" -H "${CT}" -d "${POLICY}")
HTTP_CODE=$(echo "$RESULT" | tail -1)
BODY=$(echo "$RESULT" | sed '$d')

if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
  echo "  ✅ Notification policy updated"
else
  echo "  ⚠️  HTTP ${HTTP_CODE}: ${BODY}"
fi

# --- Step 3: Create alert folder ---
echo "[3/4] Creating alert folder..."
FOLDER=$(curl -s -w "\n%{http_code}" -X POST "${API}/folders" \
  -H "${AUTH}" -H "${CT}" \
  -d '{"uid":"aist-bot-alerts","title":"Aist Bot Alerts"}')
HTTP_CODE=$(echo "$FOLDER" | tail -1)
FOLDER_BODY=$(echo "$FOLDER" | sed '$d')

if [ "$HTTP_CODE" = "200" ]; then
  echo "  ✅ Folder created"
else
  echo "  ⚠️  HTTP ${HTTP_CODE} (may already exist — continuing)"
fi

# --- Step 4: Create alert rules ---
echo "[4/4] Creating alert rules..."

create_rule() {
  local NAME="$1"
  local SQL="$2"
  local THRESHOLD="$3"
  local INTERVAL="$4"
  local FOR_DURATION="$5"
  local SUMMARY="$6"
  local SEVERITY="$7"

  local RULE=$(cat <<EOF
{
  "title": "${NAME}",
  "ruleGroup": "aist-bot",
  "folderUID": "aist-bot-alerts",
  "noDataState": "OK",
  "execErrState": "Alerting",
  "for": "${FOR_DURATION}",
  "condition": "B",
  "labels": {
    "bot": "aist",
    "severity": "${SEVERITY}"
  },
  "annotations": {
    "summary": "${SUMMARY}",
    "dashboard_uid": "aist-bot-errors",
    "runbook_url": "https://github.com/TserenTserenov/PACK-digital-platform/blob/main/entities/DP.RUNBOOK.001-aist-bot-errors.md"
  },
  "data": [
    {
      "refId": "A",
      "relativeTimeRange": {"from": 900, "to": 0},
      "datasourceUid": "${GRAFANA_DS_UID}",
      "model": {
        "rawSql": "${SQL}",
        "format": "table",
        "intervalMs": 1000,
        "maxDataPoints": 43200
      }
    },
    {
      "refId": "B",
      "relativeTimeRange": {"from": 0, "to": 0},
      "datasourceUid": "__expr__",
      "model": {
        "type": "threshold",
        "conditions": [
          {
            "evaluator": {"type": "gt", "params": [${THRESHOLD}]},
            "operator": {"type": "and"},
            "reducer": {"type": "last"},
            "query": {"params": ["A"]}
          }
        ],
        "expression": "A"
      }
    }
  ]
}
EOF
)

  RESULT=$(curl -s -w "\n%{http_code}" -X POST \
    "${API}/v1/provisioning/alert-rules" \
    -H "${AUTH}" -H "${CT}" -d "${RULE}")
  HTTP_CODE=$(echo "$RESULT" | tail -1)
  BODY=$(echo "$RESULT" | sed '$d')

  if [ "$HTTP_CODE" = "201" ] || [ "$HTTP_CODE" = "200" ]; then
    echo "  ✅ ${NAME}"
  else
    echo "  ⚠️  ${NAME}: HTTP ${HTTP_CODE}"
  fi
}

# Rule 1: L3+ Critical Errors
create_rule \
  "L3+ Critical Errors" \
  "SELECT COUNT(*)::float AS value FROM error_logs WHERE severity IN ('L3','L4') AND last_seen_at > NOW() - INTERVAL '15 minutes' AND escalated = false" \
  0 \
  "5m" \
  "0s" \
  "L3/L4 ошибки обнаружены — требуется вмешательство. См. /errors в боте." \
  "critical"

# Rule 2: Unknown Error Spike
create_rule \
  "Unknown Error Spike" \
  "SELECT COUNT(*)::float AS value FROM error_logs WHERE category = 'unknown' AND last_seen_at > NOW() - INTERVAL '1 hour' AND occurrence_count >= 3" \
  5 \
  "15m" \
  "5m" \
  "Всплеск неклассифицированных ошибок — нужен triage для новых паттернов RUNBOOK." \
  "warning"

# Rule 3: Error Rate Anomaly (>50 errors/hour)
create_rule \
  "Error Rate Anomaly" \
  "SELECT SUM(occurrence_count)::float AS value FROM error_logs WHERE last_seen_at > NOW() - INTERVAL '1 hour'" \
  50 \
  "15m" \
  "5m" \
  "Аномальный уровень ошибок (>50/час). Проверить: Claude API, Neon, Railway." \
  "warning"

# Rule 4: Bot Heartbeat (no activity = bot may be down)
create_rule \
  "Bot Heartbeat Lost" \
  "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(last_seen_at)))::float / 3600 AS hours_since_last FROM error_logs" \
  24 \
  "1h" \
  "30m" \
  "Нет новых записей в error_logs >24ч — бот может быть остановлен. Проверить Railway." \
  "info"

echo ""
echo "=== Done ==="
echo "Verify at: ${GRAFANA_URL}/alerting/list"
echo ""
echo "Created:"
echo "  - Contact point: aist-bot-telegram"
echo "  - 4 alert rules in folder 'Aist Bot Alerts'"
echo "  - Notification policy: bot=aist → Telegram"
