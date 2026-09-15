# st-rotator

**面向 OpenAI 兼容端点的多 Key 轮换 + 限流自愈网关。**

把多把 API Key 池化成一个稳定的本地端点，让上层应用在服务端限流下保持高成功率与低延迟。
适配商汤日日新（SenseNova）等 OpenAI 兼容服务，可直接接进 WorkBuddy 等上层应用。

> ⚠️ **使用前请先读 [合规与免责声明](#合规与免责声明)。**
> 本项目与任何服务商均无关联，仅供你使用**本人有权使用**的凭据。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](#)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#)

![控制台](docs/console.png)

---

## 它解决什么问题

大模型服务的限流策略通常是动态的、不透明的。上游一旦开始返回 429，
最直接的后果不是"慢"，而是**请求失败**和**尾延迟爆炸**。

本工具的目标是让上层应用在限流期间**保持可用**：

| 问题 | 现象 | 本工具的处理 |
|---|---|---|
| **动态限流不可预测** | 429 出现的时间点和频率无法预判 | AIMD 自适应限速：**主动降速**到服务端能接受的速率，再逐步试探恢复 |
| **错误分类不清晰** | 有的网关用 429 表达鉴权失败，简单的"遇 429 就重试"会在坏凭据上空转 | 解析响应体，区分"暂时限流"与"凭据失效"，分别处理 |
| **尾延迟爆炸** | 无限重试导致单个请求挂几分钟 | 单请求等待预算（`max_total_wait`），超预算立刻失败，把延迟上限钉死 |

**核心设计取向：主动限速优于加并发。**

撞到 429 时，本工具会**降低**发往上游的速率，而不是立刻换一把 Key 继续冲。
同样的吞吐下，成功率、延迟、429 次数都能显著改善，同时对上游更友好 ——
这既是工程上的更优解，也是更稳妥的用法。

## 特性

- **零第三方依赖** —— 纯 Python 标准库实现，`clone` 完就能跑，内网 / 容器 / 离线机器都不挑
- **本地 OpenAI 兼容网关** —— 上层只看到一个稳定端点，限流、冷却、Key 轮换全部在内部消化
- **图形控制台** —— 看池子状态、加 / 删 / 体检 Key、切换模型、复制接入片段、看实时日志
- **系统托盘（Windows）** —— 图标颜色即健康度，双击开控制台，右键可操作
- **配置热更新** —— 控制台里改完立即生效并落盘，不需要重启任何东西

## 快速开始

```bash
git clone https://github.com/Phoeky/st-rotator.git
cd st-rotator
cp config.example.json config.json     # 填入你自己的 Key（config.json 已在 .gitignore 里）
```

启动（三选一）：

```bash
# 常驻系统托盘，不弹窗口 —— 推荐日常使用
python -m st_rotator tray -c config.json --port 8899

# 直接打开控制台窗口
python -m st_rotator ui -c config.json --port 8899

# 只要网关，不要界面（适合部署到服务器）
python -m st_rotator serve -c config.json --port 8080 --token <本地口令>
```

Windows 用户可以直接生成桌面快捷方式，之后双击启动：

```bash
python install_shortcut.py --desktop
```

## 接入上层应用

网关启动后，把它当成一个普通的 OpenAI 端点用：

| 配置项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8899/v1` |
| API Key | 启动时 `--token` 指定的本地口令 |
| 模型名 | `deepseek-v4-flash`（或其他已支持的模型） |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8899/v1", api_key="<本地口令>")
resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "你好"}],
)
```

> **对上层完全透明**：限流、冷却、Key 轮换都在网关内部消化，上层只看到一个稳定端点。
> 网关会原样透传原始 chunk，`tool_calls` / `finish_reason` / `usage` 一个字段都不改，
> 所以 Agent 的工具调用不会被吃掉。

支持的端点：

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | 对话补全，支持 `stream` 与非流式 |
| `GET /v1/models` | 模型列表 |
| `POST /v1/*` | 其余端点（embeddings 等）原样透传，同样享受轮换 |
| `GET /healthz` | 健康检查（**无需 token**，可接监控探针） |
| `GET /stats` | 每把 Key 的成功 / 失败 / 429 次数、当前自适应速率 |

## 命令行

| 命令 | 作用 |
|---|---|
| `tray -c <cfg> [--port P] [--token T]` | 系统托盘 + 网关 + 控制台（推荐日常使用） |
| `ui -c <cfg> [--port P] [--token T] [--no-open]` | 图形控制台 + 网关 |
| `serve -c <cfg> [--port P] [--token T]` | 只启动网关，无界面 |
| `status -c <cfg>` | 打印 Key 池状态与限速模式 |
| `check -c <cfg>` | 逐把 Key 单独体检（区分"真失效"和"暂时无法确认"） |
| `chat -c <cfg> [-s] <prompt>` | 发一次对话，`-s` 流式 |
| `bench -c <cfg> -n N -p P` | 并发压测，验证轮换效果 |
| `demo` | 离线演示，不需要真实 Key |

## 配置

```jsonc
{
  "base_url": "https://token.sensenova.cn/v1",
  "default_model": "deepseek-v4-flash",

  "strategy": "round_robin",     // round_robin | least_inflight | least_recent | weighted
  "max_attempts": 8,             // 单个请求最多换几次 Key
  "max_total_wait": 120,         // 单请求总等待预算（秒），0 = 不限

  "rate_control": {
    "mode": "adaptive",          // off | fixed | adaptive
    "qps": 0.3,                  // fixed 的目标速率；adaptive 的初始速率
    "min_qps": 0.15,
    "max_qps": 1.5,
    "decrease": 0.85,            // 撞 429 时的乘性衰减系数
    "increase_step": 0.05,
    "recovery_seconds": 8        // 距离上次 429 多久才允许提速
  },

  "cooldown": {
    "base": 3, "factor": 2, "max": 60, "jitter": 0.5,
    "invalid_ttl": 600,          // 失效 Key 多久后允许再探一次
    "server_error": 2
  },

  "accounts": [
    {
      "name": "账号1",
      "api_keys": ["${SENSENOVA_KEY_1}"],   // 支持 ${ENV} 占位符
      "rpm_limit": 30,                      // 该账号每分钟最多几次
      "max_concurrency": 4,
      "weight": 1
    }
  ]
}
```

**同一账号下的多把 Key 共享账号配额**，所以 `rpm_limit` 建议填「账号总配额 ÷ Key 数量」。

> ⚠️ **不要改小 `rate_control.decrease`、也不要把 `recovery_seconds` 调大。**
> `0.85` / `8` 是实测调出来的：改成 `0.6` / `20` 会让速率触底爬不回来，吞吐掉到原来的三分之一。

### 配额估算

> **前提：只用你本人有权使用的凭据。**
> 请勿把他人账号的 Key 放进池子，也不要通过批量注册账号放大免费额度 ——
> 那既违反绝大多数服务商的条款，也不是本工具的设计用途。

在合规使用自有凭据的前提下，按下面的方式估算所需 Key 数：

```
所需 Key 数 ≈ 峰值请求数/分钟 ÷ 单把 Key 的实测可用速率 × 1.5（安全余量）
```

| 场景 | 峰值调用量 | 建议 Key 数 |
|---|---|---|
| 个人轻量 | ~2 req/min | 2 把 |
| 单人 Agent | ~5 req/min | 3~4 把 |
| 3~5 人小团队 | ~15 req/min | 8~10 把 |
| 20 人团队 | ~60 req/min | 25~30 把 |

> 接 Agent 类应用要按工具调用循环估：一个用户回合可能触发 5~20 次模型调用，
> 远高于聊天场景。

**如果加了 Key 吞吐仍然上不去**，通常说明瓶颈不在凭据数量，而在上游的整体服务容量。
这时应当**降低调用量或错峰使用**，而不是继续增加凭据 ——
本工具的自适应限速会自动把你收敛到上游能接受的速率。

## 注意事项

- **默认只监听 `127.0.0.1`。** 改成 `0.0.0.0` 等于同网段任何人都能用你的凭据，
  同时也会构成"许可他人使用"，可能违反你所使用的服务条款。
- **永远设 `--token`。** 不设口令等于本机任何进程都能白嫖。
- `config.json` 含明文密钥，已加入 `.gitignore`，**不要提交**。推荐用 `${ENV}` 占位符写法。
- 日志里的 Key 一律脱敏（`sk-J79...aZuN`），可以安全外发。
- **控制台页面会明文显示 token**（复制接入片段需要），截图外发前注意避开。
- `deepseek-v4-flash` 是推理模型，`reasoning_content` 与 `content` 共用 `max_tokens` 预算，
  **建议不低于 500**，否则 `content` 会返回空串。

## 合规与免责声明

### 凭据使用

- **只使用你本人有权使用的凭据。** 请勿把他人的 API Key 放进池子 ——
  多数服务条款都明确禁止账号借用、出租、转让或以其他方式许可他人使用。
- **不要通过批量注册账号来放大免费额度。** 这不属于本工具的设计用途。
- **不要把网关暴露给他人。** 默认只监听 `127.0.0.1`；对外提供接入等同于把你的凭据
  许可给他人使用。
- 你对自己如何使用本工具负全部责任，包括遵守你所使用服务的服务条款与当地法律法规。

### 免责

- 本项目是**独立的第三方开源工具**，与商汤科技及任何其他服务商
  **没有任何隶属、合作、赞助或背书关系**。
- 项目名称及文档中出现的服务名、模型名，仅为**说明兼容性**而作的描述性引用，
  相关商标归其各自权利人所有。
- 本工具**不破解、不绕过任何服务商的技术措施**，也不提供任何凭据。
  它只在你提供的凭据之上做客户端侧的**重试、限速与容错**。
- 软件按 MIT 协议「原样」提供，不附带任何明示或默示担保。作者不对使用本工具产生的
  任何后果负责，包括但不限于账号被限制或终止、服务中断、数据或业务损失。
- 商业场景使用前，请自行咨询法律意见。

## 项目结构

```
st_rotator/
├── keypool.py      # ★ 核心：Key 池调度、冷却、RPM 窗口、统计
├── client.py       # ★ 核心：错误分类、轮换重试、流式/原始 chunk 透传
├── proxy.py        # ★ 本地 OpenAI 兼容网关
├── ui.py           # ★ 控制台后端：状态快照、写操作、路由
├── tray.py         # ★ Windows 托盘（纯 ctypes 调 Shell_NotifyIcon）
├── trayicons.py    # 状态图标生成（手工拼 ICO 字节，不需要 Pillow）
├── dashboard.py    # 控制台前端（单文件 HTML，零 CDN 依赖）
├── transport.py    # 零依赖 HTTP 客户端（连接池 + 流式读取）
├── limiter.py      # 固定限速 + AIMD 自适应限速
├── config.py       # 配置加载、校验、${ENV} 展开、定点落盘
├── logs.py         # 日志环形缓冲 + 文件轮转
├── cli.py          # 命令行
└── demo.py         # 内置模拟上游
```

## 环境要求

- Python **3.10+**（用到了 `X | Y` 类型语法）
- 无任何第三方依赖，无需 `pip install`

## License

MIT
