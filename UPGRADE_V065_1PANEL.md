# v0.6.5：1Panel 升级清单

本清单适用于项目目录 `/opt/score-fourfold`、Compose 项目名 `score-fourfold`。

## 升级前

1. 停止 `app`，保留 Caddy：`docker compose stop app`
2. 备份 `.env`：`cp .env .env.backup-before-v065`
3. 备份数据卷：

   `docker run --rm -v score-fourfold_score-data:/data:ro -v /opt/score-fourfold:/backup alpine tar czf /backup/score-data-backup-before-v065.tgz -C /data .`

## 安装

1. 校验发布包 SHA-256。
2. 在 `/opt/score-fourfold` 解压发布包。
3. 保持原 `.env`，并确认以下配置：

   ```dotenv
   DATA_PROVIDER=okooo
   OKOOO_BASE_URL=https://www.okooo.com
   AI_ANALYSIS_ENABLED=true
   DEEPSEEK_API_KEY=服务器上的真实密钥
   DEEPSEEK_API_URL=https://api.deepseek.com/v1/chat/completions
   DEEPSEEK_MODEL=deepseek-chat
   ```

4. `docker compose build app`

## 上线前一次性验收

依次运行，任一步失败都不要启动常驻服务：

```bash
docker compose run --rm app check-config
docker compose run --rm app probe-data
docker compose run --rm app probe-ai
docker compose up -d
docker compose ps
docker compose logs --tail=100 app
```

`probe-data` 应至少显示一个可用市场。澳客当前公开混合投注页直接提供胜平负，并通过每场展开接口提供比分赔率；若某场展开响应不完整，程序会安全地跳过该场比分市场，不会伪造固定奖金。

## 回滚

代码或网络探测异常时保持 `app` 停止，恢复升级前发布包和 `.env.backup-before-v065`。数据库只有在确认迁移或数据损坏时才恢复；恢复数据卷前必须再次确认卷名并停止所有写入者。
