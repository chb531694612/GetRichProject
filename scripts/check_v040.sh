#!/bin/sh
set -eu

PUBLIC_IP="${1:-}"
if [ -z "$PUBLIC_IP" ]; then
    echo "用法：sudo sh scripts/check_v040.sh 腾讯云公网IP" >&2
    exit 1
fi

cd "$(dirname "$0")/.."

fail=0
ok() { printf 'OK  %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fail=1; }

if docker compose ps --status running --services 2>/dev/null | grep -qx app; then
    ok "app 容器在运行"
else
    bad "app 容器未运行"
fi

if docker compose ps --status running --services 2>/dev/null | grep -qx caddy; then
    ok "caddy 容器在运行"
else
    bad "caddy 容器未运行"
fi

port="$(docker compose port app 8080 2>/dev/null || true)"
case "$port" in
    127.0.0.1:*)
        ok "8080 只绑定本机：$port"
        ;;
    *)
        bad "8080 绑定异常：$port"
        ;;
esac

if curl -fsS http://127.0.0.1:8080/healthz | grep -qx ok; then
    ok "本机 healthz 返回 ok"
else
    bad "本机 healthz 失败"
fi

config_json="$(docker compose run --rm --no-deps -T app check-config 2>/dev/null || true)"
printf '%s\n' "$config_json" | grep -q '"web_access_mode": "public"' \
    && ok "web_access_mode=public" || bad "web_access_mode 不是 public"
printf '%s\n' "$config_json" | grep -q '"web_trust_proxy_headers": true' \
    && ok "web_trust_proxy_headers=true" || bad "未信任代理头"
printf '%s\n' "$config_json" | grep -q '"web_password_hash_configured": true' \
    && ok "已配置网页密码哈希" || bad "缺少网页密码哈希"
printf '%s\n' "$config_json" | grep -q "\"web_public_origin\": \"https://${PUBLIC_IP}\"" \
    && ok "WEB_PUBLIC_ORIGIN 匹配公网 IP" || bad "WEB_PUBLIC_ORIGIN 与公网 IP 不一致"

# 证书不受系统信任时，用 -k 只验证服务可握手。
if curl -ksS --resolve "${PUBLIC_IP}:443:127.0.0.1" "https://${PUBLIC_IP}/healthz" | grep -qx ok; then
    ok "本机经 443 可达 healthz"
else
    bad "本机经 443 访问 healthz 失败"
fi

if [ -f .web-login-password-once.txt ]; then
    printf 'WARN 仍存在一次性密码文件 .web-login-password-once.txt；登录并保存后请删除。\n'
fi

if [ "$fail" -ne 0 ]; then
    echo "检查未全部通过。" >&2
    exit 1
fi
echo "v0.4.0 基础检查通过。"
