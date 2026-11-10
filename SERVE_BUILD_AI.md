# serve-build.yml — AI 调用指南

> 面向 AI agent 的操作手册。描述如何触发 `serve-build.yml`、与 runner 上的 HTTP build 服务交互、以及限制条件。

## 0. 前置条件

- 调用方（AI 所在机器）必须在**同一个 Tailscale tailnet** 内，否则无法访问 runner 的 tailnet IP。
- 仓库：`nako-ruru/remote-build`（默认 branch `main`）。

## 1. 触发 workflow（纯 HTTP）

使用 GitHub REST API：

```bash
curl -sS -X POST \
  "https://api.github.com/repos/nako-ruru/remote-build/actions/workflows/serve-build.yml/dispatches" \
  -H "Authorization: Bearer $GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  -d '{
    "ref": "main",
    "inputs": {
      "build_id": "<short-id>",
      "token": "<strong-random-token>",
      "plat": "ubuntu-latest",
      "port": "8787",
      "build_command": "cargo build --release",
      "timeout": "3600",
      "max_lifetime": "7200"
    }
  }'
```

成功返回 `204 No Content`（无 body）。

### 获取 run ID（dispatch 不返回 run id）

```bash
# 取最近一次 run
curl -sS \
  "https://api.github.com/repos/nako-ruru/remote-build/actions/workflows/serve-build.yml/runs?per_page=1" \
  -H "Authorization: Bearer $GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['workflow_runs'][0]['id'])"
```

### 获取 runner tailnet IP

> **注意**：workflow 里没有给 runner 指定主机名（带 `--hostname` 会让 Windows 上的
> `tailscale up` 直接 `exit 2`），所以 MagicDNS 名 `rb-<build_id>` **不存在**，
> runner 的名字是自动生成的 `github-runnervmXXXX`。

**推荐**：在调用方本机查 tailnet（一次即可）：

```bash
tailscale status | grep github-runner      # 取新加入那一行的 IP
```

方式 A：从 run 日志（zip，**仅 run 结束后可取**；run 进行中 `runs/<id>/logs` 返回 404）：
```bash
RUN_ID=<id>
curl -sSL -H "Authorization: Bearer $GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/nako-ruru/remote-build/actions/runs/$RUN_ID/logs" \
  -o run-logs.zip
unzip -p run-logs.zip '*Serve*' 2>/dev/null | grep -oE 'tailnet address: [0-9.]+' | head -1
```

### 前置条件（纯 HTTP）

- `GITHUB_PAT`：GitHub Personal Access Token，scope 需 `repo` + `workflow`（或 fine-grained: Actions read/write + Contents read）。
- 调用方必须在**同一个 Tailscale tailnet** 内（runner 仅 tailnet 可达）。
- 需要 `curl`、`python3`、`unzip`（方式 A）。

服务 base URL：`http://<IP>:<port>`（仅 tailnet 内可达，无 HTTPS，token 保护）。

## 3. HTTP API

所有需鉴权的端点：带 `?token=<token>` 或 header `X-Build-Token: <token>`。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `GET` | `/healthz` | 否 | 存活检查，返回 `ok` |
| `PUT` | `/files/<path>` | 是 | 上传文件（binary body + `Content-Length`） |
| `GET` | `/files/<path>` | 是 | 下载文件 |
| `POST` | `/build` | 是 | 触发构建（JSON body） |
| `GET` | `/status` | 是 | 状态 + 日志尾部 |
| `GET` | `/shutdown` | 是 | 关闭服务 |

### 上传源码（两种方式）

**方式 A：上传 zip，让服务解压**
```bash
# 1. 上传 zip
curl -sS -X PUT \
  "http://$IP:$PORT/files/src.zip?token=$TOKEN" \
  --data-binary @source.zip \
  -H "Content-Type: application/octet-stream"

# 2. 触发 build（zip 解压到 build-dir）
curl -sS -X POST "http://$IP:$PORT/build?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"zip":"src.zip","command":"cargo build --release","dir":"src"}'
```

> **PowerShell 坑**：`curl -d '{...}'` 的引号会被 PowerShell 吃掉，服务端返回
> `{"error":"body must be JSON"}`（400）。PowerShell 下要把 JSON 写进文件再发：
> `curl.exe -X POST ... --data-binary @body.json`。

**方式 B：上传单个文件后直接 build**
```bash
curl -sS -X PUT "http://$IP:$PORT/files/src/main.rs?token=$TOKEN" --data-binary @main.rs
curl -sS -X POST "http://$IP:$PORT/build?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"cargo build --release"}'
```
（`dir` 省略时使用服务启动的 `--build-dir`，默认 `src`，且目录必须已存在。）

### `/build` 请求体

```json
{
  "command": "cargo build --release",   // 可选，默认 build_command
  "zip": "src.zip",                     // 可选，zip 文件名（相对 root）
  "dir": "src"                          // 可选，构建目录（相对 root）
}
```

- `command` 是**单条 shell 字符串**（服务端 `shell=True`），可用 `&&` / `;` 串联多步。
  **shell 因平台而异**：Linux/macOS 是 `bash`，**Windows 是 `cmd.exe`** —— `&&` 可用，
  但 `export RUSTFLAGS=...` 这类 bash 语法在 Windows 上会失败（要用 `set RUSTFLAGS=...`）。
- 若提供 `zip`，服务先解压到 `dir`（覆盖已有目录），再 build。
- 若无 `zip`，`dir` 必须已存在。

### 轮询状态

```bash
curl -sS "http://$IP:$PORT/status?token=$TOKEN"
# => {"phase":"building","dir":"src","message":"...","log":"...最后200行..."}
```

`phase` 取值：`idle` → `extracting` → `building` → `done` / `error`。

AI 应轮询直到 `phase` 为 `done` 或 `error`。

### 下载产物

```bash
curl -sS "http://$IP:$PORT/files/target/release/mybin?token=$TOKEN" -o ./mybin
```

路径相对于服务 `--root`（workflow 中为 `${{ github.workspace }}/ws`）。构建产物通常在 `src/target/...` 或 `target/...`。

- 原生构建（`plat=windows-latest`）：`src/target/release/<bin>.exe`
- 交叉编译（`plat=ubuntu-latest` + `--target x86_64-pc-windows-gnu`）：`src/target/x86_64-pc-windows-gnu/release/<bin>.exe`
  （triple 会多一层目录，写错就 404）

### 关闭服务

```bash
curl -sS "http://$IP:$PORT/shutdown?token=$TOKEN"
```

（可选；`max_lifetime` 到期自动退出。）

## 4. 工具链与平台

- `plat=ubuntu-latest`（默认）：额外安装 `gcc-mingw-w64-x86-64` + `rustup target add x86_64-pc-windows-gnu`（支持 Windows 交叉编译）。
- `plat=windows-latest` / `macos-latest`：使用原生工具链，不额外安装。
- 所有平台启用 **sccache**（`RUSTC_WRAPPER=sccache`，`SCCACHE_GHA_ENABLED=true`）+ **cargo registry cache**。
- `RUSTFLAGS` 默认未设 mold；如需 mold，在 `build_command` 里自行 `export RUSTFLAGS=...`。

## 5. AI 调用完整流程（纯 HTTP）

```
1. POST /repos/.../actions/workflows/serve-build.yml/dispatches  (inputs: build_id, token, ...)
2. GET  /repos/.../actions/workflows/serve-build.yml/runs?per_page=1  -> RUN_ID
3. 轮询 run status 直到 in_progress；从 run logs (zip) 提取 "tailnet address: <IP>"
   (或 MagicDNS 直接用 rb-<build_id>)
4. if source is zip:
     PUT  /files/src.zip  (binary)
     POST /build  {"zip":"src.zip","command":...}
   else:
     PUT  /files/<each file>
     POST /build  {"command":...}
5. loop: GET /status ; if phase in {done,error}: break; sleep 5s
6. if phase==done: GET /files/<artifact path> -> local
7. (optional) GET /shutdown
```

所有 GitHub API 调用带 header `Authorization: Bearer $GITHUB_PAT` + `Accept: application/vnd.github+json`。

## 6. 常见陷阱

- **主机名冲突**：重复 `build_id` 会导致 Tailscale 设备名冲突；每次用唯一 id（如加时间戳）。
- **token 泄露**：token 作为 workflow input 会出现在 run UI；用强随机值，build 完立即 shutdown。
- **上传上限**：单文件 512 MB；大项目务必排除 `target/`、`.git/`、`node_modules/`。
- **无增量**：每次全量上传 zip；源码变更仍需重新上传全部。
- **shell 注入**：`command` 走 `shell=True`；调用方负责命令安全。
- **并发**：服务端同一时间只允许一个 build（并发请求返回 409）。
- **产物路径**：构建产物在 `--root` 下的相对路径，下载时用 `/files/<relpath>`；确认 `build_command` 的输出位置。
- **Windows runner**：workflow 会 `taskkill /f /im tailscale-ipn.exe` 杀掉 Tailscale GUI，避免与 CLI 冲突；Tailscale 用 `v4` + `authkey`（**不要加 `args`**，Windows 上会 `exit 2`）。
- **打 zip 必须正斜杠**：Windows 的 `Compress-Archive` 用反斜杠存条目，Linux 解压后没有子目录，cargo 报 `can't find bin ... at path .../src/src/main.rs`。用 Python `zipfile` 打包（`rel.replace('\\','/')`），并排除 `target/ .git/ dist/`。
- **只 PUT 一次**：源码包只上传一遍；重试构建用 `POST /build`，不要重复 `PUT`。
- **token 用 header 更稳**：`X-Build-Token: <token>`，避免 query 被 URL 转义搞坏。
- **下载大产物可加代理**：`curl --proxy http://<proxy>:<port> ...`（调用方直连 tailnet 慢时）。
- **job 超时**：`timeout-minutes: 180`；超大 build 可能被 GitHub 杀掉。
