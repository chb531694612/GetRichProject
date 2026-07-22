# 比分串关服务 v0.4.0 交接文档（交给 Cursor）

更新时间：2026-07-15（Asia/Shanghai）

> **最重要的状态说明**
>
> - 腾讯云服务器当前稳定运行的是 **v0.3.0**，不要把本地工作区直接当成可部署的 v0.4.0。
> - 本地 v0.4.0 的登录和 HTTPS 代码已经写入一部分，但在用户要求停止时，`web.py` 刚由协作任务写完，**测试尚未同步、全套测试尚未执行、部署包尚未生成**。
> - 当前本地文件只能视为“v0.4.0 开发中快照”。Cursor 应先审查和补测试，再部署。
> - 不要向用户索要或回显 QQ 邮箱授权码、网页登录明文密码、Caddy CA 私钥。

## 1. 交给 Cursor 的目标

请在现有代码上完成 v0.4.0：

1. 用户没有域名，通过腾讯云服务器的**公网 IPv4** 在浏览器访问。
2. 公网入口使用 Caddy HTTPS；无域名时使用 Caddy 内部 CA 为公网 IP 签发证书。
3. 网页必须有应用内登录页、退出登录、12 小时会话、CSRF 防护和登录失败限速。
4. 外网只开放 TCP 443；应用端口 8080 绝不能对公网开放。
5. 保留 v0.3.0 已有功能：推荐记录、赛果、2 元基准奖金/盈亏汇总、手动触发今日推荐、邮件发送、自动结算。
6. 完成单元测试、安全测试、Docker/Caddy 配置检查、升级文档和 `dist/score-fourfold-v0.4.0.tar.gz`。
7. 在所有验证通过前，不要让用户替换服务器上正在运行的 v0.3.0。

## 2. 业务口径

- 推荐竞彩足球“明确比分”串关，用户自行到线下购买。
- 每张计划只按最低 2 元计算理论奖金，不管理用户实际投注金额。
- 当候选场次恰好为 3 场时可以推荐 3 串 1；满足 4 场时推荐 4 串 1。
- 当日比赛不足时，可以从当日和下一比赛编号日期合并候选。
- 推荐邮件要求在每天 18:00 前送达；当前时间配置为 10:00、14:00、17:30 尝试，17:45 停止新建，17:50 为邮件安全截止。
- 推荐结果、赛果和结算结果发送到用户 QQ 邮箱。
- 这是赔率规则筛选，不是可保证盈利的 AI；不得承诺“月入 10 万”或任何稳定收益。

当前策略概要：

1. 读取官方页面使用的竞彩足球比分固定奖金数据，要求一场有完整 31 个比分选项且可过关。
2. 对每场赔率做归一化：`(1 / SP) / sum(1 / 所有31项SP)`，得到市场隐含概率。
3. 默认排除“胜其他/平其他/负其他”，选隐含概率最高且不低于 6% 的明确比分。
4. 排除距离开赛不足 120 分钟、超过 48 小时、赔率过旧等场次。
5. 同联赛最多 2 场，组合联合概率至少 `0.0001`。
6. 组合优先按联合概率乘积排序，赔率乘积只用于并列时破局。
7. 目前没有历史回测校准，也没有计算真实正期望值。

## 3. 当前线上服务器状态（已完成并实际运行）

- 云厂商：腾讯云，Ubuntu，Docker CE 27.5.1，Docker Compose 2.32.4。
- 项目目录：`/opt/score-fourfold`。
- 当前镜像：`score-fourfold:0.3.0`。
- 当前容器曾确认 `healthy`。
- 当前映射：`127.0.0.1:8080 -> app:8080`，只允许服务器本机访问。
- `curl -fsS http://127.0.0.1:8080/healthz` 已返回 `ok`。
- 腾讯云服务器已能读取数据：探测结果曾显示 4 场比赛、4 场完整比分数据、17 条赛果。
- QQ 测试邮件已经成功收到。
- v0.3.0 网页、手动推荐、推荐记录、赛果和盈亏汇总已经实现。
- FinalShell 的本地 SSH 隧道没有成功监听，因此用户改为要求“公网 HTTPS + 登录”。
- 线上**尚未部署 Caddy，也没有公网网页登录**。

已有备份信息：

- `/home/ubuntu/score-fourfold.env.before-v030`
- Docker 数据卷内 `/app/data/score_fourfold.db.backup-v030`
- 用户此前上传过 `/home/ubuntu/score-fourfold-v0.3.0.tar.gz`

部署 v0.4.0 前仍应重新备份 `.env`、SQLite 数据库和整个 `/opt/score-fourfold` 目录；不要只依赖上面的旧备份。

## 4. v0.3.0 已完成的代码

- `src/score_fourfold/web.py`：个人看板、记录/赛果/盈亏展示、手动推荐 POST、基础安全响应头。
- `src/score_fourfold/strategy.py`：3 串 1 / 4 串 1 和跨比赛编号日期候选逻辑。
- `src/score_fourfold/database.py`：计划、邮件发件箱、结算和手动请求幂等记录。
- `src/score_fourfold/scheduler.py`：18:00 前的多时段推荐与结算调度。
- `compose.yaml`（v0.3.0 时）：仅向宿主机 `127.0.0.1` 发布 8080。
- `dist/score-fourfold-v0.3.0.tar.gz`：已生成并已部署到腾讯云。
- v0.3.0 当时共 41 个单元测试通过。

## 5. 本地 v0.4.0 已写入、但尚未验收的改动

以下内容已经写到工作区，Cursor 应逐项审查，不能直接认定完成。

### 5.1 密码哈希模块

文件：`src/score_fourfold/auth.py`

- 使用固定参数 scrypt：`N=16384, r=8, p=1`。
- 哈希格式：`scrypt:16384:8:1:<salt_hex>:<derived_hex>`。
- 最短密码 16 字符。
- 提供 `hash_password()`、`valid_password_hash()`、`verify_password()`。
- 已做 Python AST 语法解析，尚未在 `python:3.12-slim` 容器内运行测试。

### 5.2 配置

文件：`src/score_fourfold/config.py`

新增字段：

```text
WEB_ACCESS_MODE=ssh|public
WEB_PUBLIC_ORIGIN=https://公网IP
WEB_USERNAME=owner
WEB_PASSWORD_HASH=<scrypt哈希>
WEB_TRUST_PROXY_HEADERS=true|false
WEB_SESSION_HOURS=12
```

public 模式会校验 HTTPS origin、用户名格式、密码哈希、代理头信任开关和会话时长。`tests/helpers.py` 已补齐这些 `Settings` 字段。

### 5.3 CLI

文件：`src/score_fourfold/cli.py`

- 新增 `score-fourfold hash-password` 交互命令。
- 新增 `score-fourfold hash-password --stdin`，供升级脚本通过标准输入生成哈希，避免密码出现在命令行参数中。
- `check-config` 只输出用户名/密码哈希是否配置，不输出哈希本身。
- public 模式日志文案已调整。

### 5.4 网页登录（重点：刚写入，未测试）

文件：`src/score_fourfold/web.py`

当前代码已经包含：

- `/login` 登录页。
- `POST /login`、`POST /logout`。
- 进程内 opaque 会话，服务端只保存 token 的 SHA-256 key。
- Cookie：`__Host-score_session`、`HttpOnly`、`Secure`、`SameSite=Strict`、`Path=/`。
- 12 小时绝对过期；重启进程后会话自然失效。
- 每会话 CSRF token；手动推荐仍保留原有 request id + HMAC 幂等令牌。
- 10 分钟登录表单 HMAC token。
- 按客户端 IP 的失败限速：15 分钟内最多 5 次。
- 最多 2 个并行 scrypt 校验，防止 1C/4G 服务器被密码请求拖垮。
- public 模式严格校验 Host、`X-Forwarded-Proto=https`、Origin/Referer 和单一 `X-Forwarded-For`。
- `/healthz` 免登录，只返回 `ok`。
- 登录成功后主页显示退出按钮。

已知状态：

- 文件能通过 Python AST 语法解析。
- `tests/test_web.py` 在停止开发时**尚未添加 public/login 测试**。
- 现有单元测试和完整测试套件均**未运行**。
- 需要人工安全审查 handler 的每个提前返回分支、请求体关闭策略、代理头信任边界和会话并发行为。

### 5.5 Caddy 与 Compose

文件：`caddy/Caddyfile`、`compose.yaml`

当前设计：

```text
浏览器 -> TCP 443 / Caddy TLS -> Docker 内网 app:8080
```

- Caddy 使用 `https://{$PUBLIC_IP}` + `tls internal`。
- Caddy 覆盖 `X-Forwarded-Proto=https`、`X-Forwarded-For={remote_host}`，保留原始 Host。
- app 的 8080 仍只绑定宿主机 `127.0.0.1`，便于本机诊断；绝不能在腾讯云安全组开放 8080。
- Caddy 只映射 TCP 443，没有映射 80，也没有启用 UDP 443/HTTP3；用户必须输入 `https://公网IP`。
- Caddy `/data` 和 `/config` 使用持久化卷；不要执行 `docker compose down -v`。
- Compose 镜像版本已改为 `score-fourfold:0.4.0`。
- Caddy 镜像当前写为 `caddy:2-alpine`，上线前应考虑固定已验证的具体版本或 digest。
- Caddyfile 尚未在真实 Caddy 容器中执行 `caddy validate`。

### 5.6 v0.4.0 升级脚本

文件：`scripts/upgrade_env_v040.sh`

计划用法：

```bash
sudo sh scripts/upgrade_env_v040.sh .env <腾讯云公网IPv4>
```

脚本当前会：

1. 备份为 `.env.backup-v040`。
2. 写入 public 模式、public origin、用户名、会话时长和公网 IP。
3. 构建 v0.4.0 app 镜像。
4. 生成 48 位十六进制随机密码。
5. 通过新版容器的 `hash-password --stdin` 生成 scrypt 哈希并写入 `.env`。
6. 将明文登录信息临时写入 `.web-login-password-once.txt`（权限 600），供用户在自己屏幕查看一次。
7. 运行 `docker compose config --quiet`。
8. 不自动替换当前运行容器。

该脚本尚未在 Ubuntu/Docker 服务器实测。Cursor 必须检查：POSIX sh 兼容性、错误回滚、重复执行、Compose 输出是否污染捕获到的 hash、root 文件所有权以及密码文件删除流程。

### 5.7 版本号

- `pyproject.toml` 已从 0.3.0 改为 0.4.0。
- `src/score_fourfold/__init__.py` 已从 0.3.0 改为 0.4.0。

## 6. 明确未完成的事项

### 本地已完成（2026-07-15 Cursor 续作）

- [x] 审查 `web.py` 登录实现（并补上 POST 提前拒绝时的 body 排空，避免 Windows 连接复位）。
- [x] 为 `auth.py` 增加独立单元测试。
- [x] 扩充 `tests/test_web.py` public/login 安全测试。
- [x] 为 `Settings.from_env()` 增加 public/ssh 配置校验测试。
- [x] 运行全套单元测试：`PYTHONPATH=src python -m unittest discover -s tests -t . -v` → **58 passed**。
- [x] 更新 `README.md` / `UPDATE_TENCENT.md`；Caddy 镜像固定为 `caddy:2.11.4-alpine`。
- [x] 生成并检查 `dist/score-fourfold-v0.4.0.tar.gz`。

### 仍需在腾讯云服务器完成（本机 Windows 无 Docker）

- [ ] 在 Ubuntu/Docker 内执行 `docker compose config --quiet`。
- [ ] 使用 `caddy:2.11.4-alpine` 执行 `caddy validate --config /etc/caddy/Caddyfile`。
- [ ] Docker 集成验证：443 可访问、公网 8080 不可访问、登录后才能看主页和触发推荐。
- [ ] 用户确认备份后，再按 `UPDATE_TENCENT.md` 升级。
- [ ] 腾讯云安全组只新增 TCP 443；不要开放 8080。
- [ ] 从 Caddy 容器导出 `root.crt`，在用户 Windows 电脑核对 SHA-256 后安装到受信任根；绝不能导出 `root.key`。
- [ ] 浏览器、邮件、自动调度、手动推荐和结算做最终验收。
- [ ] 演练一次 v0.3.0 回滚步骤。

本地无法替用户完成服务器侧 Docker/Caddy 实测与开放安全组；请严格按 `UPDATE_TENCENT.md`。

## 7. 必须补充的安全测试

至少覆盖：

1. 未登录、过期或篡改 Cookie：GET `/` 跳转登录，POST 推荐不触发业务函数。
2. 登录成功：Set-Cookie 同时包含 `Secure`、`HttpOnly`、`SameSite=Strict`、`Path=/`，且没有 Domain。
3. 错误用户名和错误密码使用同一通用错误，不泄露账户是否存在。
4. 登录前伪造 Cookie 不会被沿用；成功登录必须生成新 token。
5. POST logout 必须带该会话 CSRF；退出后旧 Cookie 立即失效。
6. 推荐 POST 必须同时满足：已登录、会话 CSRF、原 request id/signature、正确 Host、正确 Origin。
7. 另一个会话的 CSRF、缺失 Origin/Referer、恶意 Origin、重复 Host、重复/逗号 X-Forwarded-For、非 HTTPS X-Forwarded-Proto 全部拒绝。
8. 第 5 次失败后 429 且带 Retry-After；不同 IP 不互相锁死；成功登录后清除该 IP 失败记录。
9. 超大 body、错误 Content-Type、错误 Content-Length、坏 UTF-8 和过多表单字段安全返回 4xx，不产生 HTTP/1.1 请求体污染。
10. `/healthz` 无需登录、只返回 `ok`，且不泄露配置。
11. CSP、no-store、nosniff、frame deny、Referrer-Policy、Permissions-Policy 和 HSTS 均符合预期。
12. SSH 模式的 v0.3.0 行为继续可用，旧测试不能回归。
13. Docker 集成：HTTP 后端只在 Docker 内网/宿主机 loopback；公网 `:8080` 连接失败；HTTPS `:443` 成功。

## 8. 建议的完成顺序

1. 先复制整个工作区做快照，因为当前目录虽有 `.git` 目录，但本次环境中 `git status` 报“not a git repository”，不能依赖 Git 回滚。
2. 审查 `auth.py`、`config.py`、`web.py`、`cli.py` 的接口是否一致。
3. 先写/补测试，再修改实现。
4. 跑全套测试和 Docker 内测试。
5. 验证 Caddyfile、Compose 和升级脚本。
6. 更新文档，生成脱敏部署包并检查压缩包内容。
7. 让用户重新备份线上 v0.3.0，再逐步升级。
8. 服务器验证通过后，才开放安全组 TCP 443。
9. 登录成功并保存密码后，删除服务器上的一次性明文密码文件：

   ```bash
   sudo rm -f /opt/score-fourfold/.web-login-password-once.txt
   ```

## 9. 目标环境变量示例（禁止放真实秘密到仓库）

```dotenv
WEB_ENABLED=true
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_ACCESS_MODE=public
WEB_PUBLIC_ORIGIN=https://<腾讯云公网IPv4>
WEB_USERNAME=owner
WEB_PASSWORD_HASH=scrypt:16384:8:1:<salt_hex>:<derived_hex>
WEB_TRUST_PROXY_HEADERS=true
WEB_SESSION_HOURS=12
PUBLIC_IP=<腾讯云公网IPv4>
```

邮箱的 `SMTP_AUTH_CODE` 只能保留在服务器 `.env`（权限 600），不能进入源码、文档、测试日志或压缩包。

## 10. 无域名证书说明

- Caddy 内部 CA 可以为 IP 提供 HTTPS，但浏览器默认不信任该 CA。
- 在没有安装 `root.crt` 前，浏览器会显示证书警告；连接虽加密，但首次身份信任不完整。
- 推荐从容器中只导出根证书：

  ```bash
  cd /opt/score-fourfold
  sudo docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt /home/ubuntu/caddy-root.crt
  sudo chown ubuntu:ubuntu /home/ubuntu/caddy-root.crt
  sha256sum /home/ubuntu/caddy-root.crt
  ```

- 用户通过 FinalShell 下载 `caddy-root.crt`，在 Windows 本地核对 SHA-256 后导入“受信任的根证书颁发机构”。
- 绝不要下载或复制 `root.key`，也不要把整个 `caddy-data` 卷交给客户端。
- Firefox 可能需要在自己的 Authorities 中单独导入。

官方设计依据：

- <https://caddyserver.com/docs/automatic-https>
- <https://caddyserver.com/docs/caddyfile/directives/tls>
- <https://caddyserver.com/docs/caddyfile/directives/reverse_proxy>
- <https://caddyserver.com/docs/running>

## 11. 最终验收标准

只有同时满足以下条件才能宣布 v0.4.0 完成：

- 全套测试通过，测试数量和命令写入交付说明。
- 未登录无法读取任何推荐历史或盈亏数据。
- 未登录/CSRF 错误无法触发推荐。
- `https://公网IP` 可访问并登录，Cookie 属性正确。
- 公网 8080 不通，公网只需 TCP 443。
- Caddy 根证书信任后 Chrome/Edge 不再报警。
- 邮件、定时推荐、手动推荐、赛果结算均未回归。
- 容器重启后自动恢复；登录会话失效并要求重新登录属于预期行为。
- 部署包经清单检查，不含任何秘密和运行数据库。
- 有经过实际演练的 v0.3.0 回滚步骤。

