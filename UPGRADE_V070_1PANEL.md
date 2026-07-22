# v0.6.5 升级到 v0.7.0：1Panel 小白操作手册

本文假设项目目录是 `/opt/score-fourfold`，Compose 项目名是 `score-fourfold`。如果你的目录不同，把命令中的目录替换成实际目录。

## 先记住三条安全规则

1. 不要执行 `docker compose down -v`，其中 `-v` 会删除数据库卷。
2. 不要用新的 `.env.example` 覆盖服务器现有 `.env`；现有 `.env` 里有邮箱授权码、DeepSeek Key 等秘密。
3. 必须先停止 app、备份数据库卷，再替换代码。1Panel OpenResty 不需要停止，升级期间网页会暂时显示上游不可用。

## 一、升级前确认

在 1Panel 左侧进入「容器」→「编排」，找到本项目，确认项目目录。然后进入「主机」→「终端」，执行：

```bash
cd /opt/score-fourfold
pwd
docker compose ps
docker compose config --services
docker network inspect 1panel-network
```

`config --services` 应只显示 `app`。`network inspect` 应显示 1Panel 的网络信息；OpenResty 由 1Panel 管理，不属于本项目 Compose。如果网络不存在，先确认 OpenResty 已在 1Panel 应用商店安装并启动，不要继续执行 Compose。如果 `cd` 报错，说明项目不在该目录，先在 1Panel 编排详情里找实际路径。

查看当前数据卷的真实名称：

```bash
docker inspect score-fourfold-app-1 --format '{{range .Mounts}}{{println .Name "->" .Destination}}{{end}}'
```

如果容器名不是 `score-fourfold-app-1`，先执行 `docker compose ps`，把命令中的容器名换成 app 那一行显示的名字。记录指向 `/app/data` 的卷名，通常是 `score-fourfold_score-data`。

## 二、停止写入并备份

只停止 app，不停止 1Panel OpenResty：

```bash
cd /opt/score-fourfold
docker compose stop app
cp -a .env .env.backup-before-v070
```

下面命令中的 `score-fourfold_score-data` 必须换成上一步查到的真实卷名：

```bash
docker run --rm \
  -v score-fourfold_score-data:/data:ro \
  -v /opt/score-fourfold:/backup \
  alpine:3.20 \
  tar czf /backup/score-data-backup-before-v070.tgz -C /data .
```

确认两个备份文件存在且压缩包不是 0 字节：

```bash
ls -lh .env.backup-before-v070 score-data-backup-before-v070.tgz
```

可选但推荐：检查压缩包能读取文件列表。

```bash
tar tzf score-data-backup-before-v070.tgz | head
```

## 三、上传并替换 v0.7.0 代码

### 方式 A：用 1Panel 文件管理器上传发布包

1. 在 1Panel 左侧打开「文件」，进入 `/opt/score-fourfold`。
2. 上传 v0.7.0 压缩包。
3. 先把旧代码复制或改名为 `/opt/score-fourfold-v065-code-backup`。
4. 解压新包，确保 `compose.yaml`、`Dockerfile`、`src` 直接位于 `/opt/score-fourfold`，不要多套一层目录。
5. 把刚才备份的 `.env.backup-before-v070` 复制回 `/opt/score-fourfold/.env`。

### 方式 B：服务器目录本身是 Git 仓库

只有你一直用 Git 部署时才用此方式：

```bash
cd /opt/score-fourfold
git status
git fetch --all --tags
git checkout v0.7.0
```

如果没有 `v0.7.0` 标签，使用你实际发布的 v0.7.0 分支或提交。不要在不清楚本地修改用途时执行 `git reset --hard`。

## 四、合并 .env 配置

保留旧 `.env` 的账号、密码和 Key，只调整或补充以下项目：

```dotenv
MIN_LEAD_MINUTES=60
MAX_ODDS_AGE_MINUTES=120
MIN_SCORE_PROBABILITY=0.02
MIN_HAD_PROBABILITY=0.28

# 要使用看板上的 AI 分析按钮时：
AI_ANALYSIS_ENABLED=true
DEEPSEEK_API_KEY=你的真实DeepSeekKey
DEEPSEEK_API_URL=https://api.deepseek.com/v1/chat/completions
DEEPSEEK_MODEL=deepseek-chat
```

`MIN_SCORE_PROBABILITY`、`MIN_JOINT_PROBABILITY`、`MIN_HAD_PROBABILITY`、`MIN_HAD_JOINT_PROBABILITY` 在 v0.7.0 策略中不再参与过滤，保留只是为了兼容旧配置。

如果只通过 SSH 隧道访问看板，保持：

```dotenv
WEB_ACCESS_MODE=ssh
WEB_HOST=0.0.0.0
WEB_PORT=8080
```

此模式不要在云服务器安全组放行 8080。SSH 隧道示例（在自己的电脑运行）：

```bash
ssh -L 8080:127.0.0.1:8080 root@你的服务器IP
```

然后在电脑浏览器打开 `http://127.0.0.1:8080`。

如果通过 1Panel OpenResty 和域名公开访问，改为：

```dotenv
WEB_ACCESS_MODE=public
WEB_HOST=0.0.0.0
WEB_PORT=8080
WEB_PUBLIC_ORIGIN=https://你的域名
WEB_USERNAME=owner
WEB_PASSWORD_HASH=使用程序生成的密码哈希
WEB_TRUST_PROXY_HEADERS=true
```

生成密码哈希（把命令执行后提示输入的密码记好）：

```bash
docker compose run --rm app hash-password
```

将输出的完整哈希复制到 `.env` 的 `WEB_PASSWORD_HASH=` 后面。不要填写明文密码。

## 五、构建前检查

```bash
cd /opt/score-fourfold
docker compose config
```

这条命令必须无报错，并且输出中能看到：

- app 镜像 `score-fourfold:0.7.0`；
- app 端口绑定 `127.0.0.1`；
- app 接入外部网络 `1panel-network`，容器名是 `score-fourfold-app`；
- 数据卷挂载到 `/app/data`。

构建新镜像：

```bash
docker compose build --pull app
```

构建完成后先做只读/临时容器检查：

```bash
docker compose run --rm app check-config
docker compose run --rm app health
docker compose run --rm app probe-data
```

如果启用了 AI，再执行：

```bash
docker compose run --rm app probe-ai
```

`probe-ai` 会产生一次很小的 DeepSeek API 请求。未启用 AI 时不要执行。

## 六、正式启动与自动迁移

```bash
docker compose up -d app
docker compose ps
docker compose logs --tail=200 app
```

app 首次启动会自动迁移数据库：

- 为 `plans` 增加 `ai_summary`；
- 把 `recommendation_days` 主键调整为 `(recommendation_date, market, plan_id)`；
- 允许同一天最多 3 张 CRS 计划，同时 HAD 仍最多 1 张。

不要在迁移过程中关闭容器。看到 app 持续运行且没有 traceback 后执行：

```bash
docker compose exec app score-fourfold health
docker compose exec app score-fourfold status
docker compose ps
```

`docker compose ps` 中 app 应为 `Up`，稍后应显示 `healthy`。

## 七、网页验收

进入个人看板，逐项确认：

1. 历史计划仍能显示，统计数字正常。
2. 表格有「AI分析」列。
3. 有摘要时能点击「展开全文」。
4. 配置 DeepSeek 后，「AI 分析」按钮能写入结果，刷新页面后结果仍存在。
5. CRS 计划行末有删除单场按钮，删除后串数、联合赔率、概率、奖金同步变化。
6. 删除整张计划后，该计划消失且统计同步变化。

注意：删除是正式数据库操作。首次验收尽量使用不需要保留的测试计划；生产计划删除前先确认。

## 八、常见问题

### 用 1Panel OpenResty 建立 HTTPS 反向代理

1. 在 1Panel 打开「网站」→「创建网站」→「反向代理」。
2. 填写已经解析到服务器的域名。
3. 代理地址填写 `http://score-fourfold-app:8080`。OpenResty 与 app 都在 `1panel-network`，不要填写 OpenResty 容器自己的 `127.0.0.1`。
4. 创建后进入该网站的「HTTPS」，申请并启用证书，开启 HTTP 跳转 HTTPS。
5. 云服务器安全组只开放 80、443 和管理所需的 SSH 端口，不开放 8080。
6. 确认 `.env` 的 `WEB_PUBLIC_ORIGIN` 与浏览器实际地址完全一致，例如 `https://score.example.com`，末尾不要加 `/`。

OpenResty 应传递 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto`。1Panel 默认反向代理模板通常已经包含；若网页登录提示“请求地址、HTTPS代理或来源校验失败”，在网站反向代理配置中确认存在：

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

### app 一直 restarting

```bash
docker compose logs --tail=300 app
docker compose run --rm app check-config
```

最常见原因是 `.env` 缺少值、格式错误，或旧 `.env` 被示例文件覆盖。

### AI 按钮提示未启用或返回空结果

检查 `.env` 中 `AI_ANALYSIS_ENABLED=true`、`DEEPSEEK_API_KEY` 非空，然后重建/重启 app：

```bash
docker compose up -d --force-recreate app
docker compose run --rm app probe-ai
```

### 看不到 2 串 1

只有数据源明确提供并允许 2 串 1公式时才会生成；如果官方/上游当时只开放 3 串 1、4 串 1，程序会安全跳过 2 串 1。

## 九、回滚到 v0.6.5

只有出现无法修复的启动或数据问题才回滚。先停止 app：

```bash
cd /opt/score-fourfold
docker compose stop app
```

恢复旧代码和旧 `.env`。如果数据库已经完成 v0.7.0 迁移，稳妥做法是连数据库一起恢复。再次确认下面的卷名正确，然后清空的目标只能是该项目的数据卷：

```bash
docker run --rm \
  -v score-fourfold_score-data:/data \
  -v /opt/score-fourfold:/backup:ro \
  alpine:3.20 \
  sh -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar xzf /backup/score-data-backup-before-v070.tgz -C /data'
```

恢复 `.env` 并重建旧版本：

```bash
cp -a .env.backup-before-v070 .env
docker compose build app
docker compose up -d app
docker compose logs --tail=200 app
```

不要恢复数据库时让 app 运行，也不要把卷名写成其他项目的卷。

## 十、升级成功的最终标准

- `docker compose config` 无错误；
- `docker compose ps` 中 app 是 Up，最终变成 healthy；
- 1Panel OpenResty 网站的 HTTPS 反向代理可正常打开登录页；
- `score-fourfold health` 成功；
- 历史数据仍在；
- 看板 AI、删除计划、删除单场功能可用；
- 日志没有数据库迁移错误、Python traceback 或持续重启。
