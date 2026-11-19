# 运维手册（多账号池 · ECS 部署 · 添加账号）

> 本文沉淀自 2026-11 实际部署运维过程，面向长期维护。含账号池调度语义、ECS 标准部署流程、加号标准化作业（SOP）、风控隔离要点与已踩过的坑。

## 一、当前部署形态

```
opencode / 任意 OpenAI 兼容客户端
        │  http://115.29.231.25:7863/v1   (api_key 鉴权)
        ▼
ECS Ubuntu 24.04 (115.29.231.25)
  /opt/wb2api/wb2api            # linux/amd64 二进制（CGO_ENABLED=0 交叉编译）
  /opt/wb2api/config.json       # 主配置（api_key / pool / schedule）
  /opt/wb2api/auths/*.json      # 每号一个凭证文件（accessToken/refreshToken 明文，权限 0600）
  /opt/wb2api/data/             # state.json / usage.json / request-logs/
  systemd unit: wb2api.service  # User=wb2api, TZ=Asia/Shanghai, Restart=on-failure
        │
        ▼ 直连 copilot.tencent.com（国内域，无需代理）
  账号池（cn realm）：多账号轮转 + 会话粘性 + 熔断冷却 + 定时任务
```

- 公网 7863 需在**阿里云安全组**放行 TCP 7863（主机 ufw/iptables 均已放行；安全组是独立一层）。
- 安全组未开放时的临时通道：`ssh -p 222 -N -L 17863:127.0.0.1:7863 root@<ecs>` + baseURL `http://127.0.0.1:17863/v1`。
- ECS 上另有 mihomo（`127.0.0.1:7890`，面板 `:9090`）可做出站代理备用；当前网关出站直连。

## 二、账号池语义（本项目实际使用的配置）

| 项 | 值 | 说明 |
|---|---|---|
| `pool.max_in_flight` | 4 | 每账号最多 4 并发（真实 agent 场景调高；global 域同步调 4） |
| `session_sticky.enabled` | true | 会话粘性：同一 `conversation_id` 固定同一账号，**不飘号** |
| `session_sticky.ttl` | 24h | 粘性时长拉长到 24h，多轮长会话不换号 |
| `prompt.mode` | `passthrough` | 透传客户端 system（保留 agent 自身指令），遇内容拦截自动降级重试 |
| 请求 ID | 自动 | 每响应带 `X-Request-Id`；每行请求日志含 uid / TTFB / tok/s，可对账 |
| 模型名 | 透传 | `/v1/models` 返回 `cn:xxx` 与裸名 `xxx` 双形态均可请求 |

**选号顺序**（简述）：过滤禁用/冷却/熔断 → 成本分层 → 最早到期积分优先 → 加权随机（积分比例+闲置补偿）。**新登录号热加载进池免重启**；`auths/` 目录加文件即被识别（重启用 `systemctl restart wb2api`）。

**会话粘性**：客户端带 `metadata.conversation_id`（或不带时按 system+首条 user 派生 `d-` 键）绑定账号，TTL 滚动续期，失败自动解绑。已实测同一 conversation 两次请求稳定落同一账号。

## 三、添加账号标准化作业（每个新号 5 分钟）

前置：本机 Windows + Chrome/Edge + playwright（`pip install playwright && python -m playwright install chromium`）；网关在跑。

```bash
# 1) 拿授权 state（面板 API）
curl -X POST http://127.0.0.1:7863/panel/api/login/start \
  -H "Authorization: Bearer $WB2A_API_KEY" -H "Content-Type: application/json" \
  -d '{"realm":"cn"}' > login_state.json

# 2) 独立指纹登录（真实 Chrome 有头窗口；每个号用不同 --tag 自然隔离）
python scripts/login_account.py --phone 139xxxxxxxx --tag acc1

# 3) 短信到达后写码文件（脚本读后即删）：
printf '123456' > sms_code.txt
#    - 如触发「安全验证」第二步会有第二条短信，同样写入
#    - 滑块验证码在窗口里手动拖

# 4) 登录成功自动: 凭证落盘 auths/workbuddy-<uid>.json + 热加载进池 + 首次签到
# 5) 同步 ECS:
scp -P 222 auths/workbuddy-<uid>.json root@115.29.231.25:/opt/wb2api/auths/
ssh -p 222 root@115.29.231.25 'chown -R wb2api:wb2api /opt/wb2api && systemctl restart wb2api'
```

**指纹隔离原则**：每号一套浏览器 profile（Playwright Chromium / Edge 副本 / 有头 Chrome 交替使用），cookie/指纹互不共享；出站 IP 相同无妨，但**凭证文件绝不混用**。`login_account.py` 每次运行删除旧 `sms_code.txt`，验证码一次性。

## 四、Linux 构建与部署

```bash
# 本机（Windows）交叉编译：
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o wb2api_linux ./cmd/server

# ECS (Ubuntu 24.04, ssh -p 222 root@115.29.231.25)：
#   目录 /opt/wb2api/{wb2api,config.json,auths/,data/}，用户 wb2api(nologin)
#   systemd: cp ecs/wb2api.service /etc/systemd/system/ && systemctl enable --now wb2api
#   升级 = 替换二进制 + systemctl restart wb2api（凭证/状态在 auths/ data/ 不受影响）
```

## 五、已踩过的坑（勿重复）

1. **无头浏览器登录必触发风控**：本机/远端无头 Chrome/Edge 均被「登录环境异常」拦（错误码 b3433666，安全验证第二关必败）。**必须用有头真实浏览器**（`channel="chrome", headless=False` + `--disable-blink-features=AutomationControlled`）——实测一次过。
2. **验证码有时效**：短信码约 1-2 分钟内有效，拿到立即写入 `sms_code.txt`；过期页面会提示「验证码过期，请重新获取」，脚本需重发逻辑。
3. **登录共两条短信**：第一条登录码 + 可能的「安全验证」第二条（完成验证并登录）。别当失败处理。
4. **Edge Cookies 是 v20 app-bound 加密**：DPAPI 主密钥解不开（app-bound），别再尝试本地解密；CDP `Storage.getCookies` 只能看到当前 profile 会话，**登录态在网页 cookie 里不持久，token 在桌面端是 `$wbEncrypted` 私有加密**——都拿不到可用 token，唯一可行路径就是 OAuth 设备授权流程重新登录。
5. **Edge 无头模式在无桌面会话下反复崩溃**（SSH 会话进程树随会话终止）——远端需 `Invoke-WmiMethod Win32_Process Create` 启动才能脱离会话存活；但既然有头登录是必经之路，此路已弃用。
6. **models.dev 国内直连超时**：opencode 卡在 build 阶段的根因。`HTTPS_PROXY` 指向 Clash（7897）+ `NO_PROXY` 排除内网地址；改代理后必须 `opencode service stop` 重启后台 server（旧 server 不继承新环境变量）。
7. **GitHub API 匿名限额**在共享出口 IP 上极易耗尽；`git ls-remote` / push 走 `git config http.proxy http://127.0.0.1:7897`。
8. **bash grep 管道**：Windows GBK 控制台下 Python 输出中文会 UnicodeEncodeError，一律 `PYTHONIOENCODING=utf-8`；后台任务的 grep 管道会缓冲吞输出，落盘文件再 grep。

## 六、opencode 接入

`~/.config/opencode/opencode.json` 的 provider 段：

```json
"wb2api": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "WorkBuddy2API (ECS)",
  "options": { "baseURL": "http://115.29.231.25:7863/v1", "apiKey": "<your-key>" },
  "models": {
    "hy4flash-free": { "name": "HY4 Flash Free", "limit": {"context": 960000, "output": 64000}, "capabilities": {"tools": true}, "cost": {"input": 0, "output": 0} },
    "glm-5.3-flash": { "...": "同上结构" }
  }
}
```

- 模型 id 透传网关 `/v1/models` 的裸名（`hy4flash-free` / `glm-5.3-flash` / `deepseek-v4-pro` …）；带 realm 前缀 `cn:` 也可。
- 模型条目**必须内联 `limit/capabilities/cost`**，否则 opencode 会去 models.dev 查元数据（国内超时卡 build）。
- opencode 后台 server 拉取 models.dev 需代理：`HTTPS_PROXY=http://127.0.0.1:7897` + `NO_PROXY=127.0.0.1,localhost,<ecs-ip>`，改完 `opencode service stop` 让下次 run 重启 server。

## 五、验证清单

```bash
curl -s http://<ecs>:7863/healthz                                  # {"healthy":N,...}
curl -s http://<ecs>:7863/status -H "Authorization: Bearer $KEY"   # 每号积分/状态
curl -s http://<ecs>:7863/v1/models -H "Authorization: Bearer $KEY"
curl -s http://<ecs>:7863/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy4flash-free","messages":[{"role":"user","content":"Reply OK"}],"stream":false,"metadata":{"conversation_id":"t1"}}'
journalctl -u wb2api -n 5 --no-pager    # 请求日志行含 uid（粘性验证：同 conversation_id 同 uid）
```

## 六、安全红线

- `auths/*.json` 是明文 accessToken/refreshToken——**永不入库、永不外传**；`.gitignore` 已覆盖
- `config.json`（含 api_key）不入库，仓库内只保留脱敏模板 `ecs/config.json`
- 公网部署必须改掉示例 api_key；面板 `http://<ecs>:7863/panel/` 同 api_key 鉴权
- 桌面端 `$wbEncrypted` token（CodeBuddyExtension auth 文件）密钥在客户端内部，**不可导出**——不要在这条路上浪费时间，直接走设备授权登录
