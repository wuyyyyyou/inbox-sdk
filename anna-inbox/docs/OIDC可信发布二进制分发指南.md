# OIDC 可信发布（Trusted Publishing）二进制分发指南

本文档说明 inbox-tool 二进制如何通过 GitHub Actions 的 **OIDC Trusted Publishing** 直接上传到 Anna 平台 CDN，全程无需长期访问令牌（PAT / API key），也不需要把任何密钥写入仓库 Secrets。

> 背景：Anna 平台的 Executa 二进制发布支持两种方式：`binary_urls`（公网拉取镜像）与 `binary_artifacts`（本地上传推送）。OIDC Trusted Publishing 是 `binary_artifacts` 在 GitHub Actions CI 下的零密钥落地方式，语义与 PyPI 的 Trusted Publishers 一致。

## 一、整体流程

```
GitHub Actions 运行 release-inbox-tool.yml
        │
        │ 1. 各平台构建二进制（build 矩阵 job）
        │ 2. release job 下载产物并整理到 executa 项目根目录
        │ 3. GitHub 向 Actions 注入 OIDC id-token（permissions: id-token: write）
        ▼
anna-app executa upload-binaries --oidc
        │
        │ 用 id-token 向 Anna 平台换取 15 分钟单工具上传令牌
        ▼
Anna 平台校验 id-token：
    - issuer（GitHub 公钥验证签名）
    - audience（固定值）
    - repository / workflow_ref / environment claims 与注册记录匹配
        ▼
按内容寻址上传二进制到平台 CDN，服务端独立重算 SHA-256 校验
```

关键点：

- 二进制**只读一次注册**，之后每次 CI 自动生效。
- 上传按内容寻址（content-addressed），相同产物重复上传零流量，失败可安全重试（幂等）。
- OIDC 只覆盖**二进制上传**这一步；生命周期类命令（`apps push` / `cut` / `release`、`executa publish` 等）仍需 PAT。

## 二、前置条件

1. 已安装并登录 `anna-app` CLI（`@anna-ai/cli`，npm 包）。
2. 拥有该 Executa 的所有权（可在 [developer console](https://staging.anna.partners/developer) 查看）。
3. 已知道 Executa 的 `tool_id`（本仓库为 `tool-riazm4777-inbox-executa-dnsb9fqu`，权威定义在 `inbox-tool/manifest.json`）。

## 三、一次性注册 Trusted Publisher

在本机（已 `anna-app login`）执行一次：

```sh
anna-app executa trusted-publisher add \
  --repository wuyyyyyou/inbox-sdk \
  --workflow release-inbox-tool.yml \
  --environment production
```

参数说明：

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `--repository` | 是 | GitHub 仓库，格式 `owner/repo` |
| `--workflow` | 是 | 允许发布二进制的 workflow 文件名 |
| `--environment` | 否 | 可选门控；指定后，CI 必须通过同名 GitHub Environment 的保护规则，且 id-token 的 `environment` claim 才会匹配 |

未传 `--environment` 时，该 workflow 文件任意分支/标签的触发均可发布（PyPI 语义）。传了则额外要求运行通过对应 GitHub Environment。

管理与查看：

```sh
anna-app executa trusted-publisher list          # 查看已注册列表
anna-app executa trusted-publisher remove <id>   # 删除某条注册
```

也可以在控制台操作：Tool 编辑对话框的 **Binary** 区有 **OIDC Trusted Publishers** 面板（仅 Owner 可见），可增删注册并查看每条注册最近被 CI 使用的时间。

## 四、二进制产物与 Executa 配置

本仓库不在 `executa.json` 中配置 `distribution`。GitHub Actions 将各平台压缩包整理到仓库根目录 `dist/inbox-tool/{version}/`，再通过 `upload-binaries` 的显式 `--tool-id` 和 `--dir` 参数上传。

本仓库压缩包由 `scripts/build/build_binary.sh` 生成，命名规则为 `inbox-tool-<version>-<platform>.<ext>`（Windows 为 `.zip`，其余 `.tar.gz`）。

## 五、Workflow 集成

`.github/workflows/release-inbox-tool.yml` 中需要三块配置：

### 1. 工作流级权限

```yaml
permissions:
  contents: write
  id-token: write
```

`id-token: write` 让 runner 能铸造 GitHub OIDC id-token；`contents: write` 是创建 GitHub Release 所需。

### 2. 上传步骤

在 `release` job 中，产物下载整理后执行：

```yaml
      - name: Install anna-app CLI
        shell: bash
        run: npm install -g @anna-ai/cli

      - name: Upload binaries to Anna CDN via OIDC
        shell: bash
        env:
          ANNA_APP_HOST: https://staging.anna.partners
        run: |
          set -euo pipefail
          anna-app executa upload-binaries \
            --oidc \
            --host "$ANNA_APP_HOST" \
            --tool-id "tool-riazm4777-inbox-executa-dnsb9fqu" \
            --dir anna-inbox/executas/inbox-tool
```

各参数含义：

| 参数 | 含义 |
| --- | --- |
| `--oidc` | 用当前 GitHub Actions OIDC id-token 换取上传令牌，代替 PAT |
| `--host` | Anna 平台地址；也可通过环境变量 `ANNA_APP_HOST` 提供 |
| `--tool-id` | 指定上传归属的 Executa；缺省时读取 `.anna/executa.json` 中的身份 |
| `--dir` | Executa 项目目录；本仓库用于定位上传上下文，不要求其中的 `executa.json` 配置 `distribution` |

> staging 环境地址为 `https://staging.anna.partners`；如切到生产环境请改为正式域名并同步注册对应的 trusted publisher。

### 3. 身份文件（可选）

`--tool-id` 与 `--host` 都显式传入后，不依赖本地 `.anna/executa.json` 身份缓存，CI 环境无需额外配置。若希望由身份文件驱动，可确保 `anna-inbox/executas/inbox-tool/.anna/executa.json` 已由 `executa publish` 等命令生成。

## 六、验证与常见问题

### 验证命令

- 本地规划验证（不实际上传）：

  ```sh
  anna-app executa upload-binaries --dry-run --dir anna-inbox/executas/inbox-tool
  ```

- 注册与身份检查：

  ```sh
  anna-app executa trusted-publisher list --tool-id tool-riazm4777-inbox-executa-dnsb9fqu
  python scripts/sync/sync_executa_identity.py --check
  ```

### 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `upload-binaries --oidc` 报 id-token 校验失败 | 未注册 trusted publisher，或注册的 repository / workflow / environment 与本次运行不匹配；先执行 `trusted-publisher add` 并核对参数 |
| 提示需要 `--host` | 未传 `--host` 且未设置 `ANNA_APP_HOST` 环境变量 |
| 提示身份未知 | `--tool-id` 拼写错误，或该 Executa 不属于当前账号 |
| 上传后安装校验失败 | 服务端按 SHA-256 重新校验；确认 CI 产物目录、文件命名与压缩包内 `manifest.json` / 入口正确 |

## 七、安全说明

- 全程不需要 `ANNA_APP_PAT` 或任何长期密钥。
- 权限最小化：仅上传步骤所在 job 需要 `id-token: write`；如注册时加了 `--environment`，还要求该环境保护规则通过。
- 若要在日志 / 诊断中出现凭证信息，严禁记录 access token、refresh token、API key 或完整 credentials 上下文（详见仓库 AGENTS.md 安全规约）。
