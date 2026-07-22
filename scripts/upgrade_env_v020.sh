#!/bin/sh
set -eu

ENV_FILE="${1:-.env}"
if [ ! -f "$ENV_FILE" ]; then
    echo "找不到 $ENV_FILE；已停止，未修改任何配置。" >&2
    exit 1
fi

BACKUP="${ENV_FILE}.backup-v020"
cp -p "$ENV_FILE" "$BACKUP"

set_value() {
    key="$1"
    value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

set_value MAX_LOOKAHEAD_HOURS 48
set_value RECOMMENDATION_TIMES 10:00,14:00,17:30
set_value RECOMMENDATION_LATEST_START 17:45
set_value RECOMMENDATION_DEADLINE 18:00
set_value RECOMMENDATION_SEND_BUFFER_MINUTES 10

chmod 600 "$ENV_FILE"
echo "配置已升级；原配置备份在 $BACKUP（不会显示邮箱授权码）。"
