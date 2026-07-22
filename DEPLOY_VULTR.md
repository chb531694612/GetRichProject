# Vultr Debian 12 小白部署指南

本指南对应服务器：Debian 12 x64、1GB 内存、IP `66.42.99.245`。

下面始终区分两个窗口：

- **Windows PowerShell**：你自己的电脑。
- **服务器终端**：成功执行 SSH 后，提示符类似 `root@vultr:~#`。

不要把服务器 root 密码或 QQ 邮箱授权码发到聊天、截图或代码仓库。

## 1. 从 Windows 登录服务器

在 Windows 开始菜单搜索并打开 PowerShell：

```powershell
ssh root@66.42.99.245
```

第一次会询问是否信任主机，输入 `yes`。随后粘贴 Vultr 页面上的 root 密码并回车。输入密码时屏幕不会出现星号或字符，这是正常现象。

成功后应看到类似：

```text
root@vultr:~#
```

## 2. 初始化 Debian 并增加 1GB 交换空间

以下命令在**服务器终端**执行：

```bash
apt update
apt upgrade -y
timedatectl set-timezone Asia/Shanghai

if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

free -h
```

`free -h` 最后一行附近应能看到约 `1.0Gi` 的 Swap。交换空间是为了降低 1GB 内存服务器构建 Docker 镜像时被系统杀掉的概率。

## 3. 安装 Docker 和 Compose

仍然在**服务器终端**逐段执行：

```bash
apt update
apt install -y ca-certificates curl nano
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

```bash
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version
docker compose version
docker run --rm hello-world
```

看到 `Hello from Docker!` 表示 Docker 安装成功。

## 4. 从 Windows 上传部署包

保持服务器终端不动，另外打开一个新的 **Windows PowerShell**，执行：

```powershell
scp "D:\陈焕彬工作\GetRichProject\dist\score-fourfold-deploy.tar.gz" root@66.42.99.245:/root/
```

再次输入 root 密码。成功后回到**服务器终端**执行：

```bash
mkdir -p /opt/score-fourfold
tar -xzf /root/score-fourfold-deploy.tar.gz -C /opt/score-fourfold
cd /opt/score-fourfold
ls -la
```

应该能看到 `Dockerfile`、`compose.yaml`、`src`、`.env.example` 和 `.dockerignore`。

## 5. 填写 QQ 邮箱配置

先复制配置模板：

```bash
cd /opt/score-fourfold
cp .env.example .env
nano .env
```

在编辑器中找到并修改下面几行：

```dotenv
MAIL_TO=531694612@qq.com
SMTP_USERNAME=531694612@qq.com
SMTP_AUTH_CODE=这里填写QQ邮箱生成的SMTP授权码
MAIL_FROM=531694612@qq.com
MAIL_DRY_RUN=false
```

`SMTP_AUTH_CODE` 必须是 QQ 邮箱设置中生成的 SMTP 授权码，不是 QQ 登录密码。

保存方法：按 `Ctrl+O`，按回车，再按 `Ctrl+X`。然后限制配置文件权限：

```bash
chmod 600 .env
```

## 6. 构建程序

在 `/opt/score-fourfold` 目录执行：

```bash
docker compose build
```

这一步可能持续数分钟。没有出现红色错误并回到命令提示符，即表示构建完成。

检查配置：

```bash
docker compose run --rm app check-config
```

输出中应看到：

```json
"smtp_username_configured": true,
"smtp_auth_code_configured": true,
"mail_dry_run": false,
"mail_errors": []
```

## 7. 测试 QQ 邮件

```bash
docker compose run --rm app test-mail
```

然后检查 `531694612@qq.com` 的收件箱和垃圾邮件箱。应收到标题含“邮件配置测试”的邮件，该邮件不会计入投注账本。

如果报 `535`、`authentication failed`，通常是授权码错误或 QQ 邮箱没有启用 SMTP。

## 8. 测试官方数据接口

```bash
docker compose run --rm app probe-data
```

成功时会输出可售比赛数量和最近赛果数量。

这一步对你的洛杉矶服务器非常关键：中国体彩网网页内部接口可能拒绝海外机房 IP。如果出现 `HTTP 403`、`WAF`、`non-JSON` 或连接失败，先不要启动正式服务，也不要用代理绕过；把完整错误文字发给我，我们再切换到合规、稳定的数据源。

## 9. 正式启动

只有测试邮件和 `probe-data` 都成功后才执行：

```bash
docker compose up -d
docker compose ps
```

等待约一分钟，再看日志：

```bash
docker compose logs --tail=100 app
```

查看运行健康状态和累计账本：

```bash
docker compose run --rm app health
docker compose run --rm app status
```

`docker compose ps` 中状态应为 `Up` 或 `healthy`。服务器重启后，Docker 会自动恢复程序。

## 10. 日常常用命令

```bash
cd /opt/score-fourfold

# 查看是否运行
docker compose ps

# 查看最近日志
docker compose logs --tail=100 app

# 实时看日志，按 Ctrl+C 退出查看，不会停止程序
docker compose logs -f app

# 重启
docker compose restart app

# 停止
docker compose down

# 启动
docker compose up -d
```

不要执行 `docker compose down -v`：其中的 `-v` 会删除保存历史计划和邮件状态的数据库卷。

程序不提供网站页面，也不需要开放 80、443 等端口；它只从服务器向外读取数据并发送邮件。

## 11. 安全提醒

首次部署成功后，应立即配置 SSH 密钥、创建非 root 管理用户、关闭 root 密码登录，并在 Vultr Firewall 中只允许你的 IP 访问 TCP 22。操作时必须保留当前 root 窗口，先在第二个窗口验证新账号和密钥成功，再关闭旧登录方式；顺序不能反，否则可能把自己锁在服务器外。建议同时启用 Vultr 自动备份。
