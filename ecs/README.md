# 运维手册（多账号池 · ECS 部署 · 添加账号）

> 本文沉淀自 2026-11 实际部署运维过程，面向长期维护。含账号池调度语义、ECS 标准部署流程、加号标准化作业（SOP）、风控隔离要点与已踩过的坑。

## 一、当前部署形态（容器 · 端口 1114）

```
opencode / hermes / 任意 OpenAI 兼容客户端（本机或局域网）
        │  http://115.29.231.25:1114/v1   (api_key 鉴权，OpenAI Chat 格式)
        ▼
ECS Ubuntu 24.04 (115.29.231.25)
  docker container: wb2api      # image wb2api:1.11.10-panel，0.0.0.0:1114->1114
  /opt/wb2api_docker/
    src/                        # 源码 + Dockerfile（构建上下文）
    docker-compose.yml          # 端口映射 / 卷 / user 10001:10001
    config.json                 # 主配置（listen 0.0.0.0:1114 / api_key / pool / schedule）
    auths/*.json                # 每号一个凭证文件（accessToken/refreshToken 明文）
    data/                       # state.json / usage.json / request-logs/
        │
        ▼ 直连 copilot.tencent.com（国内域，无需代理）
  账号池（cn realm）：多账号轮转 + 会话粘性 + 熔断冷却 + 定时任务
```

- 容器内固定以 `app(10001)` 运行 → 宿主机挂载目录必须 `chown -R 10001:10001`。
- 公网 1114 需在**阿里云安全组**放行 TCP 1114（主机 ufw/iptables 均已放行；安全组是独立一层）。
- 安全组未开放时的临时通道：`ssh -p 222 -N -L 11114:127.0.0.1:1114 root@<ecs>` + baseURL `http://127.0.0.1:11114/v1`。
- ECS 上另有 mihomo（`127.0.0.1:7890`，面板 `:9090`）可做出站代理备用；当前网关出站直连。

### 与旧 systemd:7863 部署的关系（重要）

原 systemd `wb2api.service`（`/opt/wb2api`，端口 7863）**已停止且 disable**，代码/凭证目录保留作回滚。**切勿与服务同时运行**：两者共享 `auths/` 会重复签到、并发刷新 token，直接触碰风控。

```bash
systemctl is-active wb2api     # 期望 inactive
systemctl is-enabled wb2api    # 期望 disabled
docker ps --filter name=wb2api # 期望 Up (healthy)
```

### 容器日常操作

```bash
cd /opt/wb2api_docker
docker compose ps                       # 状态（healthy 由镜像内 HEALTHCHECK 判定）
docker compose logs -f --tail 50        # 请求日志（含 uid/TTFB/tok）
docker compose restart                  # 重启（凭证/状态在挂载卷，不受影响）
docker compose up -d --build            # 改代码后重建（GOPROXY 走 goproxy.cn）
```

**健康检查端口是自适应的**：`scripts/docker-healthcheck.sh` 从挂载的 `config.json` 的 `listen` 字段推导端口，因此同一镜像既能跑 1114 也能跑 7863；`EXPOSE 7863` 仅是文档性提示。

**新增账号后热加载**：把 `auths/workbuddy-<uid>.json` 放进 `/opt/wb2api_docker/auths/` 并 `chown 10001:10001`，网关会识别新文件；必要时 `docker compose restart`。

## 二、账号池语义（本项目实际使用的配置）

| 项 | 值 | 说明 |
|---|---|---|
| `pool.max_in_flight` | 4 | 每账号最多 4 并发（真实 agent 场景调高；global 域同步调 4） |
| `session_sticky.enabled` | true | 会话粘性：同一 `conversation_id` 固定同一账号，**不飘号** |
| `session_sticky.ttl` | 24h | 粘性时长拉长到 24h，多轮长会话不换号 |
| `prompt.mode` | `passthrough` | 透传客户端 system（保留 agent 自身指令），遇内容拦截自动降级重试 |
| 请求 ID | 自动 | 每响应带 `X-Request-Id`；每行请求日志含 uid / TTFB / tok/s，可对账 |
| 模型名 | 透传 | `/v1/models` 返回 `cn:xxx` 与裸名 `xxx` 双形态均可请求 |

**选号顺序**（简述）：过滤禁用/冷却/熔断 → 成本分层 → 最早到期积分优先 → 加权随机（积分比例+闲置补偿）。**新登录号热加载进池免重启**；`auths/` 目录加文件即被识别（重启用 `docker compose restart`）。

**会话粘性**：客户端带 `metadata.conversation_id`（或不带时按 system+首条 user 派生 `d-` 键）绑定账号，TTL 滚动续期，失败自动解绑。已实测同一 conversation 两次请求稳定落同一账号。

## 三、添加账号标准化作业（每个新号 5 分钟）

前置：本机 Windows + Chrome/Edge + playwright（`pip install playwright && python -m playwright install chromium`）；网关在跑。

```bash
# 1) 拿授权 state（面板 API）
curl -X POST http://115.29.231.25:1114/panel/api/login/start \
  -H "Authorization: Bearer $WB2A_API_KEY" -H "Content-Type: application/json" \
  -d '{"realm":"cn"}' > login_state.json

# 2) 独立指纹登录（真实 Chrome 有头窗口；每个号用不同 --tag 自然隔离）
python scripts/login_account.py --phone 139xxxxxxxx --tag acc1

# 3) 短信到达后写码文件（脚本读后即删）：
printf '123456' > sms_code.txt
#    - 如触发「安全验证」第二步会有第二条短信，同样写入
#    - 滑块验证码在窗口里手动拖

# 4) 登录成功自动: 凭证落盘 auths/workbuddy-<uid>.json + 热加载进池 + 首次签到
# 5) 同步 ECS（容器卷）:
scp -P 222 auths/workbuddy-<uid>.json root@115.29.231.25:/opt/wb2api_docker/auths/
ssh -p 222 root@115.29.231.25 'chown -R 10001:10001 /opt/wb2api_docker/auths && cd /opt/wb2api_docker && docker compose restart'
```

**指纹隔离原则**：每号一套浏览器 profile（Playwright Chromium / Edge 副本 / 有头 Chrome 交替使用），cookie/指纹互不共享；出站 IP 相同无妨，但**凭证文件绝不混用**。`login_account.py` 每次运行删除旧 `sms_code.txt`，验证码一次性。

## 四、容器构建与部署

```bash
# 源码同步到 ECS（本机无 Docker，构建在 ECS 上跑）
tar --exclude=.git --exclude=auths --exclude=data -czf - . \
  | ssh -p 222 root@115.29.231.25 'tar -xzf - -C /opt/wb2api_docker/src'

# ECS 上构建 + 起容器（首次或代码变更后）
ssh -p 222 root@115.29.231.25
cd /opt/wb2api_docker && docker compose up -d --build

# 单独构建镜像（调试用；必须在含 Dockerfile 的 src/ 下）
cd /opt/wb2api_docker/src && docker build -t wb2api:1.11.10-panel --build-arg GOPROXY=https://goproxy.cn,direct .
```

**升级 = 同步源码 + `docker compose up -d --build`**；凭证与状态在挂载卷 `auths/` `data/`，重建镜像不影响。

**必须知道的两个构建约束**：
1. ECS 访问不到 `proxy.golang.org`（实测超时），`Dockerfile` 里 `ARG GOPROXY` 默认已是 `https://goproxy.cn,direct`；海外节点可 `--build-arg GOPROXY=https://proxy.golang.org,direct` 覆盖。
2. Docker Hub 拉基础镜像需镜像加速：`/etc/docker/daemon.json` 配 `registry-mirrors`（已配 daocloud / 1ms.run / dockerproxy.net）。

**`config.json` 挂载必须是文件**：compose 里 `./config.json:/app/config.json`，若宿主机文件缺失，Docker 会建成**目录**导致容器启动失败（报 `is a directory`）。首次部署先 `cp config.example.json config.json` 再改。

**改端口**：改 `config.json` 的 `listen`（如 `0.0.0.0:7863`）+ compose 的 `ports` 映射 + 安全组，三处一致；健康检查脚本会自动跟随 `listen`。

## 五、已踩过的坑（勿重复）

1. **无头浏览器登录必触发风控**：本机/远端无头 Chrome/Edge 均被「登录环境异常」拦（错误码 b3433666，安全验证第二关必败）。**必须用有头真实浏览器**（`channel="chrome", headless=False` + `--disable-blink-features=AutomationControlled`）——实测一次过。
2. **验证码有时效**：短信码约 1-2 分钟内有效，拿到立即写入 `sms_code.txt`；过期页面会提示「验证码过期，请重新获取」，脚本需重发逻辑。
3. **登录共两条短信**：第一条登录码 + 可能的「安全验证」第二条（完成验证并登录）。别当失败处理。
4. **Edge Cookies 是 v20 app-bound 加密**：DPAPI 主密钥解不开（app-bound），别再尝试本地解密；CDP `Storage.getCookies` 只能看到当前 profile 会话，**登录态在网页 cookie 里不持久，token 在桌面端是 `$wbEncrypted` 私有加密**——都拿不到可用 token，唯一可行路径就是 OAuth 设备授权流程重新登录。
5. **Edge 无头模式在无桌面会话下反复崩溃**（SSH 会话进程树随会话终止）——远端需 `Invoke-WmiMethod Win32_Process Create` 启动才能脱离会话存活；但既然有头登录是必经之路，此路已弃用。
6. **models.dev 国内直连超时**：opencode 卡在 build 阶段的根因。`HTTPS_PROXY` 指向 Clash（7897）+ `NO_PROXY` 排除内网地址；改代理后必须 `opencode service stop` 重启后台 server（旧 server 不继承新环境变量）。
7. **GitHub API 匿名限额**在共享出口 IP 上极易耗尽；`git ls-remote` / push 走 `git config http.proxy http://127.0.0.1:7897`。
8. **bash grep 管道**：Windows GBK 控制台下 Python 输出中文会 UnicodeEncodeError，一律 `PYTHONIOENCODING=utf-8`；后台任务的 grep 管道会缓冲吞输出，落盘文件再 grep。

## 六、客户端接入

### opencode desktop（`~/.config/opencode/opencode.json`）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "disabled_providers": ["nvidia", "opencode"],
  "provider": {
    "wb2api": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "WorkBuddy2API (ECS :1114)",
      "options": { "baseURL": "http://115.29.231.25:1114/v1", "apiKey": "<your-key>" },
      "models": {
        "hy4flash-free": { "name": "HY4 Flash Free", "limit": {"context": 960000, "output": 64000}, "capabilities": {"tools": true}, "cost": {"input": 0, "output": 0} },
        "glm-5.3-flash": { "...": "同上结构" }
      }
    }
  }
}
```

- 模型 id 透传网关 `/v1/models` 的裸名（`hy4flash-free` / `glm-5.3-flash` / `deepseek-v4-pro` …）；带 realm 前缀 `cn:` 也可。
- 模型条目**必须内联 `limit/capabilities/cost`**，否则 opencode 会去 models.dev 查元数据（国内超时卡 build）。
- opencode 后台 server 拉取 models.dev 需代理：`HTTPS_PROXY=http://127.0.0.1:7897` + `NO_PROXY=127.0.0.1,localhost,<ecs-ip>`，改完 `opencode service stop` 让下次 run 重启 server。
- **`disabled_providers` 是清理「自定义模型」的正确手段**：任何 `*_API_KEY` 环境变量（如 `NVIDIA_API_KEY`）会让 opencode 自动加载对应 provider 的一堆模型，这些**不在配置文件里**，光删配置删不掉。schema 中 `disabled_providers` 的语义就是 "Disable providers that are loaded automatically"。验证：`opencode-cli.exe models | sed 's|/.*||' | sort -u` 应只剩 `wb2api`。

### hermes Agent（`%LOCALAPPDATA%\hermes\config.yaml`）

```yaml
providers:
  wb2api:
    base_url: "http://115.29.231.25:1114/v1"
    api_key: "<your-key>"
    api_mode: chat_completions
```

```bash
hermes config set model.default glm-5.3-flash
hermes config set model.provider custom:wb2api
hermes config unset model.base_url     # 关键：残留 base_url 会覆盖命名 provider
hermes -z "Reply with exactly: HERMES_OK"
```

**坑（已踩）**：`gateway_elevated` / `service_identity_unverified` —— hermes 的本地后台 gateway 若以管理员（RunLevel=Highest）注册的计划任务启动，非提权客户端一律拒绝连接。修法：`hermes gateway stop` 后 `hermes gateway install --force --start-now`，在 UAC 提示处**拒绝提权**（脚本会退回直接 spawn 非提权 gateway），再跑 `-z`。

## 七、验证清单

```bash
KEY=<your-key>
curl -s http://115.29.231.25:1114/healthz                                    # {"healthy":N,...}
curl -s http://115.29.231.25:1114/v1/models -H "Authorization: Bearer $KEY"  # 19 个模型
curl -s http://115.29.231.25:1114/status -H "Authorization: Bearer $KEY"     # 每号积分/状态
curl -s http://115.29.231.25:1114/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy4flash-free","messages":[{"role":"user","content":"Reply OK"}],"stream":false,"metadata":{"conversation_id":"t1"}}'
ssh -p 222 root@115.29.231.25 'cd /opt/wb2api_docker && docker compose logs --tail 5'
# 请求日志行含 uid（粘性验证：同 conversation_id 同 uid）
```

## 八、安全红线

- `auths/*.json` 是明文 accessToken/refreshToken——**永不入库、永不外传**；`.gitignore` 已覆盖
- `config.json` / `config.1114.json`（含 api_key）不入库，仓库内只保留脱敏模板 `ecs/config.json`
- 公网部署必须改掉示例 api_key；面板 `http://<ecs>:1114/panel/` 同 api_key 鉴权
- 桌面端 `$wbEncrypted` token（CodeBuddyExtension auth 文件）密钥在客户端内部，**不可导出**——不要在这条路上浪费时间，直接走设备授权登录
