#!/bin/sh
set -eu

umask 077
ENV_FILE="${1:-.env}"
PUBLIC_IP="${2:-}"

if [ ! -f "$ENV_FILE" ]; then
    echo "找不到 $ENV_FILE；已停止，未修改任何配置。" >&2
    exit 1
fi
if [ -z "$PUBLIC_IP" ]; then
    echo "用法：sudo sh scripts/upgrade_env_v040.sh .env 腾讯云公网IP" >&2
    exit 1
fi

validate_ipv4() {
    old_ifs="$IFS"
    IFS=.
    set -- $1
    IFS="$old_ifs"
    [ "$#" -eq 4 ] || return 1
    for octet in "$@"; do
        case "$octet" in
            ''|*[!0-9]*) return 1 ;;
        esac
        [ "$octet" -le 255 ] || return 1
    done
    [ "$1" -ge 1 ] && [ "$1" -le 223 ] && [ "$1" -ne 127 ]
}

if ! validate_ipv4 "$PUBLIC_IP"; then
    echo "公网IP格式不正确：$PUBLIC_IP" >&2
    exit 1
fi

BACKUP="${ENV_FILE}.backup-v040"
if [ ! -f "$BACKUP" ]; then
    cp -p "$ENV_FILE" "$BACKUP"
fi

set_value() {
    key="$1"
    value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

# 先写入非敏感配置，使新版 compose 可以构建应用镜像。
set_value WEB_ENABLED true
set_value WEB_HOST 0.0.0.0
set_value WEB_PORT 8080
set_value WEB_ACCESS_MODE public
set_value WEB_PUBLIC_ORIGIN "https://${PUBLIC_IP}"
set_value WEB_USERNAME owner
set_value WEB_TRUST_PROXY_HEADERS true
set_value WEB_SESSION_HOURS 12
set_value PUBLIC_IP "$PUBLIC_IP"
chmod 600 "$ENV_FILE"

echo "正在构建 v0.4.0 应用镜像，用于安全生成密码哈希……"
docker compose build app

EXISTING_HASH=$(sed -n 's/^WEB_PASSWORD_HASH=//p' "$ENV_FILE" | tail -n 1)
PASSWORD_FILE="$(dirname "$ENV_FILE")/.web-login-password-once.txt"
if [ -z "$EXISTING_HASH" ]; then
    PASSWORD=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    # 丢弃 docker 警告/进度到 stderr，只保留 scrypt 哈希行，避免污染 .env。
    HASH=$(
        printf '%s\n' "$PASSWORD" \
            | docker compose run --rm --no-deps -T app hash-password --stdin 2>/dev/null \
            | tr -d '\r' \
            | grep '^scrypt:' \
            | tail -n 1
    )
    case "$HASH" in
        scrypt:16384:8:1:*) ;;
        *) echo "密码哈希生成失败；请保留备份 $BACKUP 并重试。" >&2; exit 1 ;;
    esac
    set_value WEB_PASSWORD_HASH "$HASH"
    {
        printf '网页登录地址：https://%s\n' "$PUBLIC_IP"
        printf '用户名：owner\n'
        printf '密码：%s\n' "$PASSWORD"
        printf '登录成功并妥善保存密码后，请删除本文件。\n'
    } > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
    unset PASSWORD HASH
    echo "已生成强随机登录密码。"
else
    echo "检测到已有 WEB_PASSWORD_HASH，已保留原登录密码。"
fi

docker compose config --quiet
chmod 600 "$ENV_FILE"

echo "配置已升级；原配置备份：$BACKUP"
if [ -f "$PASSWORD_FILE" ]; then
    echo "稍后用以下命令只在自己屏幕查看登录信息（不要截图或发给别人）："
    echo "sudo cat $PASSWORD_FILE"
fi
echo "准备完成，尚未替换正在运行的容器。"
