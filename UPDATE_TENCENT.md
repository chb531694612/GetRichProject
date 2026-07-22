# 腾讯云服务器升级到 0.4.0（小白版）

0.4.0 / 0.5.0 在保留推荐记录、赛果、2 元账本和手动推荐的基础上：

- **0.4.0**：改为用 **Caddy HTTPS + 应用内登录** 通过公网 IP 访问网页。
- **0.5.0**：新增非让球胜平负独立票种（每天最多比分一张 + 胜平负一张；胜平负优先 6/5/4 串 1）。

**当前线上如果是正常的 0.3.0，在备份和本地验收完成前不要升级。**  
**不要执行 `docker compose down -v`**，那会删掉数据库和 Caddy 证书数据。

## 零、升级前必须做的备份

在服务器执行：

```bash
cd /opt/score-fourfold
sudo cp .env /home/ubuntu/score-fourfold.env.before-v040
sudo docker compose stop app
sudo docker run --rm -v "$(docker volume ls -q | grep score-data | tail -n 1)":/data -v /home/ubuntu:/backup alpine \
  tar czf /backup/score-data-before-v040.tgz -C /data .
sudo tar czf /home/ubuntu/opt-score-fourfold-before-v040.tgz -C /opt score-fourfold
sudo docker compose start app
```

确认这三个文件都存在且大小不为 0：

```bash
ls -lh /home/ubuntu/score-fourfold.env.before-v040 \
  /home/ubuntu/score-data-before-v040.tgz \
  /home/ubuntu/opt-score-fourfold-before-v040.tgz
```

## 一、上传新压缩包

把电脑上的：

`dist/score-fourfold-v0.4.0.tar.gz`

用 FinalShell 拖到 `/home/ubuntu/`，然后：

```bash
ls -lh /home/ubuntu/score-fourfold-v0.4.0.tar.gz
```

## 二、解压并生成公网登录配置

把下面的 `你的公网IPv4` 换成腾讯云控制台里的真实公网 IP：

```bash
cd /opt/score-fourfold
sudo docker compose stop app
sudo tar -xzf /home/ubuntu/score-fourfold-v0.4.0.tar.gz -C /opt/score-fourfold
sudo cp /home/ubuntu/score-fourfold.env.before-v040 /opt/score-fourfold/.env
sudo sh scripts/upgrade_env_v040.sh .env 你的公网IPv4
```

升级脚本会：

1. 备份为 `.env.backup-v040`；
2. 写入 public 模式、HTTPS 来源、用户名和会话时长；
3. 构建 `score-fourfold:0.4.0` 镜像；
4. 若还没有密码哈希，生成强随机密码和 scrypt 哈希；
5. 把一次性明文密码写到 `.web-login-password-once.txt`（权限 600）；
6. 检查 `docker compose config`；
7. **不会自动替换正在运行的容器**。

查看一次性密码（只在自己屏幕看，不要发给别人）：

```bash
sudo cat /opt/score-fourfold/.web-login-password-once.txt
```

## 三、启动新版本（仍先不要开放安全组 443）

先校验 Compose 与 Caddyfile（失败则先不要 `up`）：

```bash
cd /opt/score-fourfold
sudo docker compose config --quiet
sudo docker run --rm -v "$PWD/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e PUBLIC_IP=你的公网IPv4 caddy:2.11.4-alpine \
  caddy validate --config /etc/caddy/Caddyfile
```

然后启动：

```bash
cd /opt/score-fourfold
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
sudo docker compose port app 8080
curl -fsS http://127.0.0.1:8080/healthz
```

正常时应看到：

- `app` 与 `caddy` 都是 `Up`；
- `docker compose port app 8080` 仍是 `127.0.0.1:8080`；
- `healthz` 返回 `ok`；
- `check-config` 中 `web_access_mode` 是 `public`，`web_trust_proxy_headers` 是 `true`，`web_password_hash_configured` 是 `true`。

```bash
sudo docker compose run --rm app check-config
```

在服务器本机用 HTTPS 自检（会提示证书不受系统信任，属于预期）：

```bash
curl -vk https://127.0.0.1/healthz -H "Host: 你的公网IPv4"
```

## 四、导出 Caddy 根证书（只导出证书，绝不导出私钥）

```bash
cd /opt/score-fourfold
sudo docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt /home/ubuntu/caddy-root.crt
sudo chown ubuntu:ubuntu /home/ubuntu/caddy-root.crt
sha256sum /home/ubuntu/caddy-root.crt
```

用 FinalShell 下载 `caddy-root.crt` 到自己电脑后：

1. 在本机再次计算 SHA-256，必须与服务器一致；
2. Windows：双击证书 → 安装到“受信任的根证书颁发机构”；
3. Firefox 需要在自己的证书管理中单独导入；
4. **绝不要下载 `root.key`，也不要把整个 `caddy-data` 卷拷到电脑。**

## 五、再开放腾讯云安全组 TCP 443

确认上面步骤都通过后，才在腾讯云安全组新增：

- 协议：TCP
- 端口：443
- 来源：按需（个人使用可先写自己的公网 IP）

**不要开放 8080。**

浏览器访问：

`https://你的公网IPv4`

使用 `.web-login-password-once.txt` 中的用户名和密码登录。登录成功并自己妥善保存密码后：

```bash
sudo rm -f /opt/score-fourfold/.web-login-password-once.txt
```

## 六、升级后验收清单

- [ ] 未登录访问 `/` 会跳到登录页，看不到推荐和盈亏。
- [ ] 登录后可以看到历史记录，右上角有“退出登录”。
- [ ] 退出后旧会话立即失效。
- [ ] 手动“立即尝试今日推荐”仍受 17:45 / 18:00 规则和每日一票限制约束。
- [ ] QQ 邮件、自动调度未被破坏。
- [ ] 公网访问 `http://公网IP:8080` 失败。
- [ ] 公网访问 `https://公网IP` 成功。

## 七、一键检查

```bash
cd /opt/score-fourfold
sudo sh scripts/check_v040.sh 你的公网IPv4
```

## 八、回滚到 0.3.0

如果必须回滚：

```bash
cd /opt/score-fourfold
sudo docker compose down
sudo tar -xzf /home/ubuntu/opt-score-fourfold-before-v040.tgz -C /opt
sudo cp /home/ubuntu/score-fourfold.env.before-v040 /opt/score-fourfold/.env
# 如需恢复数据库：
# sudo docker run --rm -v "$(docker volume ls -q | grep score-data | tail -n 1)":/data -v /home/ubuntu:/backup alpine \
#   sh -c 'rm -rf /data/* && tar xzf /backup/score-data-before-v040.tgz -C /data'
cd /opt/score-fourfold
sudo docker compose up -d
```

回滚后网页恢复为 SSH 隧道方式，请关闭安全组里新加的 443 入站规则。旧的 v0.3.0 说明仍适用于回滚后的访问方式：不要在公网开放 8080，改用 SSH 本地转发到 `127.0.0.1:8080`。

## 九、常见问题

### 浏览器证书警告

未安装 `root.crt` 前，Chrome/Edge 会警告，这是无域名内部 CA 的预期现象。核对 SHA-256 并安装根证书后应消失。

### 登录一直失败

先看是否记错一次性密码，再检查：

```bash
cd /opt/score-fourfold
sudo docker compose logs --tail=100 app caddy
```

同一 IP 15 分钟内连续失败太多次会被临时限制。

### 想确认 8080 仍未对公网开放

```bash
cd /opt/score-fourfold
sudo docker compose port app 8080
```

必须是 `127.0.0.1:8080`。
