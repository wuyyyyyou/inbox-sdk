# 本地 Google/Gmail Token 工具

这个目录只用于本地开发时生成和管理 Gmail OAuth token。它不是 Anna Executa，也不会被 Anna App 作为工具启动。

## 本地数据目录

默认本地数据放在当前工具目录：

```sh
scripts/google_token/
```

其中敏感文件放在：

```sh
scripts/google_token/.secrets/
```

仓库 `.gitignore` 已忽略所有 `.secrets/` 目录。不要提交其中的 client secret、access token 或 refresh token。

## 准备 Google OAuth Client

把 Google Cloud Console 下载的 OAuth client JSON 放到：

```sh
scripts/google_token/.secrets/client_secret_<your-client>.json
```

脚本会自动扫描 `scripts/google_token/.secrets/client_secret*.json`。如果有多个 client secret 文件，按文件名排序使用第一个。

也可以通过参数或环境变量显式指定：

```sh
python scripts/google_token/gmail_local_oauth.py --client-secrets /path/to/client_secret.json
```

```sh
GOOGLE_OAUTH_CLIENT_SECRETS=/path/to/client_secret.json \
python scripts/google_token/gmail_local_oauth.py
```

## 生成 Gmail Token

从仓库根目录运行：

```sh
python scripts/google_token/gmail_local_oauth.py --email your@gmail.com
```

脚本会打开浏览器完成授权，并把 token 保存到：

```sh
scripts/google_token/.secrets/gmail_tokens/<sanitized-email>.json
```

默认 scope 支持 Gmail 状态操作、账户头像和已保存联系人头像：

```text
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/contacts.readonly
openid email profile
```

旧 token 不会自动获得新增的 Contacts 权限。需要重新运行 OAuth helper，并在 Google 授权页确认只读联系人权限；未授权时邮件功能仍可使用，联系人头像回退为首字母。

如需显式覆盖 scope，可传入完整的空格分隔列表：

```sh
python scripts/google_token/gmail_local_oauth.py \
  --email your@gmail.com \
  --scope "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/contacts.readonly openid email profile"
```

## 环境变量覆盖

可以用这些环境变量覆盖默认位置：

```sh
ANNA_INBOX_TOKEN_DIR=/path/to/gmail_tokens
GOOGLE_OAUTH_CLIENT_SECRETS=/path/to/client_secret.json
```

`ANNA_INBOX_TOKEN_DIR` 优先级最高。Executa 本地 token fallback 也会优先读取这个变量。
