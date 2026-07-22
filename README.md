# 比分/胜平负串关邮件助手

这是一个可长期运行在服务器上的个人 CLI 服务。它从数据源读取竞彩足球**比分**和**非让球胜平负**固定奖金，每个推荐日最多生成两张 2 元单式基准计划（比分一张 + 胜平负一张），发送到 QQ 邮箱；比赛正式开奖后，再发送 90 分钟赛果和 2 元基准收益。

- 比分票：每场 1 个明确比分；优先 4 串 1，候选仅 3 场时可用 3 串 1。
- 胜平负票：每场只选主胜 / 平 / 客胜之一；优先 6 串 1，不足则降为 5 / 4 串 1。

当前范围不包含自动购票、倍投、资金管理或线上彩票销售。系统按用户约定把每封推荐记作已购买 2 元，但邮件中的收益仍是基于推荐时固定奖金快照的策略模拟值；实体票赔率和官方兑奖结果优先。

## 已实现

- 比分只选择明确比分，默认排除“胜其它 / 平其它 / 负其它”。
- 胜平负只使用官方非让球 `had` 玩法，不使用让球胜平负。
- 两个市场彼此独立：同日可同时存在一张比分票和一张胜平负票；同市场仍每日最多一张。
- 同一市场内不会重复使用另一张尚未结算计划中的比赛；跨市场允许同一场出现在不同玩法票中。
- 当天不足以形成有效计划时，允许合并今天和明天两个比赛编号日期；默认同一联赛最多 2 场。
- 每天北京时间 10:00、14:00、17:30 尝试；17:45 后不再请求推荐赔率，购买推荐邮件 17:50 后自动作废，18:00 后只处理赛果及通知。
- 保存每场推荐、固定奖金、赔率时间和基线概率到 SQLite。
- QQ SMTP 邮件：推荐、无推荐、赛果结算和运行异常。
- 幂等入队和邮件租约：并发任务不会同时发送同一邮件，并使用稳定 `Message-ID` 降低重发概率。SMTP 在“服务端已接收、进程尚未记账”这一极小窗口仍只能保证至少发送一次。
- 只在官方 `matchResultStatus=2` 且 `poolStatus=Payout` 后结算。
- 赛果按 90 分钟含伤停补时，不包含加时和点球。
- 无效场次按固定奖金 1 处理；全部无效时按 2 元退款记录。
- 奖金以 2 元乘综合固定奖金估算，并实现串关奖金上限和超过 1 万元时的预计税额。
- 个人网页仪表盘：查看已发送推荐、赛果、2 元基准投入和累计盈亏，并可手动尝试今日全部推荐。
- Docker 常驻运行、标准 Python 命令行运行和 `.pyz` 可执行包三种方式。
- v0.4.0 起支持公网 HTTPS + 应用内登录（见下文部署说明）。

## 推荐算法口径

系统默认启用不依赖外部 API 的本地泊松自动分析。它从完整比分市场估算双方预期进球，
将泊松模型概率与去除市场水位后的隐含概率加权；数据不完整时自动回退到市场基线。

**比分**

1. 对单场 31 个比分固定奖金取倒数。
2. 归一化为市场隐含概率。
3. 利用明确比分及“胜/平/负其它”的概率质量估算主客队预期进球。
4. 生成泊松比分概率，并按 `POISSON_MODEL_WEIGHT` 与市场概率混合。
5. 排除三个“其它”选项后，选择混合概率最高的明确比分。
6. 优先枚举满足时间、赔率新鲜度、联赛集中度和联合概率阈值的 4 场组合。
7. 合格候选只有 3 场且官方计算器提供 3×1 时，才降为 3 串 1。

**胜平负**

1. 对单场主胜 / 平 / 客胜三项固定奖金取倒数并归一化。
2. 根据同场比分市场拟合的预期进球计算主胜 / 平 / 客胜概率，并与三项市场概率混合。
3. 选择混合概率最高且不低于阈值的一项。
4. 优先尝试 6 串 1，再 5 串 1，再 4 串 1。

这仍不是已验证的盈利模型，也不能保证月收入。模型的信息来源仍是赔率结构，不包含实时伤停和首发；必须积累历史推荐与赛果后回测校准。串关方差很大；“月入 10 万”不适合作为软件验收指标。

## 本地快速验证

要求 Python 3.11 或更高版本，无第三方运行依赖。

```powershell
Copy-Item .env.example .env
```

先修改 `.env`：

```dotenv
DATA_PROVIDER=json
JSON_DATA_FILE=examples/demo-data.json
MAIL_DRY_RUN=true
```

然后运行：

```powershell
python -m pip install -e .
score-fourfold --env-file .env init-db
score-fourfold --env-file .env recommend --now 2026-07-14T12:00:00+08:00
score-fourfold --env-file .env send-mail --now 2026-07-14T12:01:00+08:00
score-fourfold --env-file .env settle --now 2026-07-15T12:00:00+08:00
score-fourfold --env-file .env send-mail --now 2026-07-15T12:01:00+08:00
score-fourfold --env-file .env status
```

演示邮件会写到 `data/mail-preview/`，不会真的发送。

## 配置 QQ 邮箱

在 QQ 邮箱账户设置中启用 SMTP 并生成授权码。授权码不是 QQ 登录密码。将下面配置只写到服务器的 `.env`，不要提交到版本库：

```dotenv
MAIL_TO=531694612@qq.com
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USERNAME=531694612@qq.com
SMTP_AUTH_CODE=这里填写QQ邮箱授权码
MAIL_FROM=531694612@qq.com
MAIL_DRY_RUN=false
```

切换正式数据源：

```dotenv
DATA_PROVIDER=okooo
OKOOO_BASE_URL=https://www.okooo.com
```

首次上线建议先保留 `MAIL_DRY_RUN=true` 跑一轮，确认数据和邮件内容后再改为 `false`。

可选的 DeepSeek AI 分析使用 OpenAI 兼容的聊天接口。启用前必须配置 Key，并先执行只产生极少 token 的连通性检查：

```dotenv
AI_ANALYSIS_ENABLED=true
DEEPSEEK_API_KEY=只写在服务器.env中的密钥
DEEPSEEK_API_URL=https://api.deepseek.com/v1/chat/completions
DEEPSEEK_MODEL=deepseek-chat
```

```bash
score-fourfold --env-file .env probe-ai
```

改成 `MAIL_DRY_RUN=false` 后，可发送一封不计入账本的测试邮件：

```bash
score-fourfold --env-file .env test-mail
```

在目标服务器只读探测一次数据接口：

```bash
score-fourfold --env-file .env probe-data
```

若返回 403、非 JSON 或结构异常，程序会失败退出，不会生成计划；不要通过代理绕过网站访问控制。

## Docker 服务器部署

服务器需安装 Docker Engine 和 Compose 插件。

```bash
cp .env.example .env
# 编辑 .env，填写 QQ SMTP 授权码并设置 MAIL_DRY_RUN=false
docker compose build
docker compose up -d
docker compose logs -f app
```

容器默认每 1800 秒检查待结算结果和发件箱，每 4 小时最多抓取一次推荐赔率；同一业务日期建单后会停止该日后续赔率请求。比赛预计开赛 150 分钟后才开始查赛果。SQLite 数据保存在 Docker 卷 `score-data` 中。

从 v0.4.0 起，推荐用公网 HTTPS + 应用内登录访问网页：

1. Compose 会启动 Caddy，把 `https://公网IPv4`（无域名，内部 CA）反代到容器内 `app:8080`。
2. 应用端口仍只发布到服务器 `127.0.0.1:8080`，**绝不要**在腾讯云安全组开放 8080；公网只开放 TCP 443。
3. 设置 `WEB_ACCESS_MODE=public`、`WEB_PUBLIC_ORIGIN=https://公网IP`、`WEB_TRUST_PROXY_HEADERS=true`，并用：

```bash
score-fourfold hash-password
```

生成 `WEB_PASSWORD_HASH`。服务器上也可运行：

```bash
sudo sh scripts/upgrade_env_v040.sh .env 公网IPv4
```

浏览器首次会不信任 Caddy 内部根证书；请按 `UPDATE_TENCENT.md` 只导出 `root.crt`（不要导出 `root.key`），核对 SHA-256 后安装到本机受信任根。

若仍使用旧的 SSH 隧道模式，将 `WEB_ACCESS_MODE=ssh`，不要启动公网登录，并通过：

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@服务器公网IP
```

访问 `http://127.0.0.1:8080`。腾讯云完整升级、检查和回滚步骤见 `UPDATE_TENCENT.md`。

常用运维命令：

```bash
docker compose run --rm app check-config
docker compose run --rm app status
docker compose run --rm app health
docker compose restart app
docker compose down
```

常驻容器运行时不要并行执行写入型的 `run-cycle`。如需手工完整执行，请先 `docker compose stop app`，执行后再启动。

升级前备份数据库：

```bash
docker compose stop app
docker run --rm -v getrichproject_score-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/score-data-backup.tgz -C /data .
docker compose start app
```

实际卷名可能因 Compose 项目名不同而变化，可用 `docker volume ls` 确认。

## 构建单文件可执行包

本项目只有 Python 标准库依赖，可以打成一个 `.pyz` 文件：

```bash
python scripts/build_zipapp.py
python dist/score-fourfold.pyz --env-file .env check-config
python dist/score-fourfold.pyz --env-file .env daemon
```

Linux 上还可以执行 `chmod +x dist/score-fourfold.pyz` 后直接运行。该文件仍要求服务器安装 Python 3.11+；完全无 Python 运行时的原生二进制可在确定目标服务器架构后再用 PyInstaller 构建。生产部署优先使用 Docker，以固定 Python 和时区环境。

## 重要配置

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `AUTOMATIC_ANALYSIS_ENABLED` | true | 启用本地预期进球 + 泊松自动分析；数据不足时回退到市场概率 |
| `POISSON_MODEL_WEIGHT` | 0.35 | 泊松模型在最终概率中的权重，范围 0–1 |
| `MIN_LEAD_MINUTES` | 120 | 距离开赛至少多少分钟才可进入候选 |
| `MAX_LOOKAHEAD_HOURS` | 48 | 最远查看未来多少小时，用于覆盖两个比赛编号日期 |
| `MAX_ODDS_AGE_MINUTES` | 60 | 允许的赔率最大年龄 |
| `MIN_SCORE_PROBABILITY` | 0.06 | 单个明确比分的最低基线概率 |
| `MIN_JOINT_PROBABILITY` | 0.0001 | 3/4 场全中的最低联合基线概率 |
| `MAX_MATCHES_PER_LEAGUE` | 2 | 单张票同一联赛最多场数 |
| `MAX_PLANS_PER_BUSINESS_DATE` | 1 | 每个推荐日每个市场最多计划数（比分与胜平负各自一张） |
| `HAD_ENABLED` | true | 是否启用非让球胜平负独立票种 |
| `HAD_PASS_SIZES` | 6,5,4 | 胜平负串关优先顺序 |
| `MIN_HAD_PROBABILITY` | 0.40 | 单项胜平负最低隐含概率 |
| `MIN_HAD_JOINT_PROBABILITY` | 0.01 | 胜平负串关最低联合隐含概率 |
| `POLL_INTERVAL_SECONDS` | 1800 | 常驻模式轮询间隔，最少 60 秒 |
| `RECOMMENDATION_TIMES` | 10:00,14:00,17:30 | 每日固定推荐尝试时点，北京时间 |
| `RECOMMENDATION_LATEST_START` | 17:45 | 此时起不再开始推荐请求 |
| `RECOMMENDATION_DEADLINE` | 18:00 | 用户要求的购买推荐截止时间 |
| `RECOMMENDATION_SEND_BUFFER_MINUTES` | 10 | 提前停止 SMTP 提交，给邮箱投递留缓冲 |
| `RESULT_CHECK_DELAY_MINUTES` | 150 | 最晚一场开赛后多久才开始查赛果 |
| `WEB_ENABLED` | true | 是否启用个人网页仪表盘 |
| `WEB_HOST` | 0.0.0.0（Docker） | 容器内监听地址；公网隔离由 Compose 的 `127.0.0.1` 绑定保证 |
| `WEB_PORT` | 8080 | 网页监听端口和宿主机本地发布端口 |
| `WEB_ACCESS_MODE` | ssh | `ssh`=隧道免登录；`public`=公网 HTTPS + 登录 |
| `WEB_PUBLIC_ORIGIN` | 空 | public 模式必须是 `https://公网IPv4` |
| `WEB_USERNAME` | owner | 网页登录用户名 |
| `WEB_PASSWORD_HASH` | 空 | `hash-password` 生成的 scrypt 哈希 |
| `WEB_TRUST_PROXY_HEADERS` | false | public 模式必须为 true，且只信任本机 Caddy |
| `WEB_SESSION_HOURS` | 12 | 登录会话绝对有效期 |
| `PUBLIC_IP` | 空 | Caddy 证书和站点名使用的公网 IPv4 |

配置检查不会输出邮箱授权码：

```bash
score-fourfold --env-file .env check-config
```

## 数据源边界

默认适配的是中国体彩网网页当前使用的内部接口，而不是有公开版本和 SLA 的开放 API。接口可能改版，也可能因服务器出口、频率或 WAF 无法访问。程序遇到 HTTP 错误、非 JSON 页面、字段异常或过旧赔率时会停止本轮，不会沿用旧数据或伪造推荐。

部署前应先从目标服务器低频试跑 24–72 小时。如果出口持续被拦截，应更换取得授权的稳定数据服务，并保持相同 Provider 输入格式；不要用代理绕过网站访问控制。数据只用于个人低频分析，商业使用或再分发前应取得数据方授权。

## 测试

在仓库根目录执行（Windows 上也可用同一命令）：

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -v
```

v0.7.0 本地验收时全套测试通过。测试覆盖比分 2/3/4 串 1、胜平负 4/5/6 串 1、同日多计划并发门禁、数据库迁移、结算、邮件、网页操作安全边界和 DeepSeek 分析存储。

服务器上还须额外执行 `docker compose config`、`caddy validate`、以及 `scripts/check_v040.sh`；本机 Windows 开发环境若没有 Docker，这些检查放到腾讯云服务器完成。

## 规则参考

- [中国体彩网竞彩足球规则说明](https://www.sporttery.cn/help/2968.html?gid=9)
- [中国体彩网竞彩足球赛果](https://www.sporttery.cn/jc/zqsgkj/)
- [财政部、税务总局关于彩票兑奖与税收口径的公告](https://szs.mof.gov.cn/zhengcefabu/202406/t20240621_3937768.htm)
