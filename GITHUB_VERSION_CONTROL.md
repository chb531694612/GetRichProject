# 将本项目纳入 GitHub 版本控制

远程空仓库地址：`git@github.com:chb531694612/GetRichProject.git`

## 一、提交前安全检查

项目已经通过 `.gitignore` 排除以下内容：

- `.env` 及其备份（邮箱授权码、DeepSeek Key、网页登录哈希）；
- SQLite 数据库和 `data` 运行目录；
- 数据卷备份压缩包；
- Python 缓存、构建产物和本地预览邮件。

真实密钥只能留在电脑或服务器的 `.env` 中。仓库只提交 `.env.example`。

## 二、设置 Git 身份（每台电脑通常只做一次）

在项目目录打开 PowerShell：

```powershell
cd "D:\陈焕彬工作\GetRichProject"
git config --global user.name "你的GitHub用户名"
git config --global user.email "你的GitHub邮箱"
```

建议在 GitHub「Settings → Emails」查看并使用 GitHub 提供的 `noreply` 邮箱，避免公开私人邮箱。

## 三、配置 GitHub SSH 密钥

先检查是否已有公钥：

```powershell
Get-ChildItem $env:USERPROFILE\.ssh\*.pub
```

如果能看到 `.pub` 文件，打开并复制公钥内容，例如：

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

然后进入 GitHub「头像 → Settings → SSH and GPG keys → New SSH key」，粘贴保存。

如果没有公钥，创建一把：

```powershell
ssh-keygen -t ed25519 -C "你的GitHub邮箱"
```

一路按 Enter 使用默认路径。私钥不能发给任何人，GitHub 页面只填写 `.pub` 公钥。

测试连接：

```powershell
ssh -T git@github.com
```

第一次会询问是否信任 GitHub 主机，确认地址无误后输入 `yes`。出现成功认证提示即可。

## 四、初始化本地仓库并首次提交

先检查当前目录：

```powershell
cd "D:\陈焕彬工作\GetRichProject"
git status
```

如果提示 `not a git repository`，执行：

```powershell
git init
git branch -M main
```

提交前必须检查忽略规则是否生效：

```powershell
git status --short
git check-ignore -v .env
git check-ignore -v data\score_fourfold.db
```

后两条应显示它们被 `.gitignore` 命中。再检查即将提交的文件：

```powershell
git add .
git status
```

确认列表中没有 `.env`、数据库、备份压缩包或真实密钥，然后创建第一个提交：

```powershell
git commit -m "release: v0.7.0"
```

## 五、连接截图中的空仓库并推送

查看是否已经配置远程：

```powershell
git remote -v
```

如果没有 `origin`：

```powershell
git remote add origin git@github.com:chb531694612/GetRichProject.git
```

如果已有错误的 `origin`，不要再次 `add`，改用：

```powershell
git remote set-url origin git@github.com:chb531694612/GetRichProject.git
```

首次推送：

```powershell
git push -u origin main
```

刷新 GitHub 仓库页面，应能看到项目文件和提交记录。

## 六、为 v0.7.0 建立版本标签

```powershell
git tag -a v0.7.0 -m "Score Fourfold v0.7.0"
git push origin v0.7.0
```

以后 1Panel 服务器可以明确检出这个版本，而不是依赖随时变化的 main。

## 七、以后每次修改的日常流程

```powershell
git status
git diff
git add 路径或文件名
git commit -m "说明本次修改"
git push
```

不要习惯性使用 `git add .`；日常按文件添加更容易发现误提交。

建议发布新版本时依次更新代码版本号、运行测试、提交，再创建类似 `v0.7.1` 的标签。

## 八、误提交密钥怎么办

只从最新提交删除文件还不够，因为旧提交仍可查看。应立即：

1. 在 QQ 邮箱、DeepSeek 等服务端撤销并重新生成泄露的密钥；
2. 暂停继续推送；
3. 使用 Git 历史清理工具移除敏感内容；
4. 清理后强制更新远端历史，并通知所有协作者重新克隆。

因此第一次 `git commit` 前的 `git status` 检查非常重要。
