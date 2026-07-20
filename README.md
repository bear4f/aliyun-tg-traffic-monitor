# Aliyun Telegram Traffic Monitor

纯 Telegram Bot 的阿里云月度流量监控与自动关机工具。没有网页面板，不需要 Nginx、PHP、数据库或 Docker。

Monitor Aliyun CDT / SWAS monthly traffic from a Telegram bot, with automatic shutdown before the free quota runs out. No web panel, no database — one Python service plus a colored terminal management panel.

针对「多个独立阿里云账号，每个账号一台 ECS，各自使用 CDT 免费流量额度」的场景设计，机器数量不设上限。

```text
阿里云账号 A → ECS A（香港）  → CDT 200 GB
阿里云账号 B → ECS B（新加坡）→ CDT 200 GB
```

## 两个面板

**Telegram 面板**是一块会自我刷新的单条消息。每次操作都在原消息上重绘，不会刷屏。

```text
📊 阿里云流量监控
账期 2026-07 · 11 天后重置 · 更新于 2026-07-20 08:20

🟢 Aliyun HK-1 · 运行中
█████████░░░ 79.2%
158.32 GB / 200.00 GB · 剩余 41.68 GB
🛡️ 熔断 95% 开 · 🗓️ 下月开机 关
📈 日均 8.3 GB · 约 4 天后触线

⚫ Aliyun SG-1 · 已关机
███████████░ 91.3%
182.54 GB / 200.00 GB · 剩余 17.46 GB
🛡️ 熔断 90% 开（本账期已触发🛑） · 🗓️ 下月开机 开
📈 日均 9.6 GB · 已达熔断线

[🔄 刷新全部]
[⚙️ Aliyun HK-1] [⏹️ 关机]
[⚙️ Aliyun SG-1] [▶️ 开机]
[🛠️ 全局设置]    [ℹ️ 帮助]
```

开关机按钮会根据实例当前状态自动切换，运行中显示关机、已关机显示开机。所有层级都有 `🏠 返回主面板`。

**终端面板**（`sudo aliyun-monitor`）顶部是一条实时状态栏，读的是服务写入的 `state.json`，秒开不联网：

```text
── Aliyun Traffic Monitor 3.0.0 ────────────────────────────────
  服务 ● 运行中    账期 2026-07 · 11 天后重置    机器 2 台

  ● Aliyun HK-1   ███████████░░░  79.2%  158.32 GB / 200.00 GB  熔断 95% · 7 分钟前
  ○ Aliyun SG-1   █░░░░░░░░░░░░░   9.8%   19.56 GB / 200.00 GB  熔断 90% · 7 分钟前
────────────────────────────────────────────────────────────────
  1) 管理配置与机器
  2) 一键自检 / 诊断
  3) 查看实时日志
  4) 重启服务
  ...
```

## 3.0 的主要变化

- Telegram 面板改为单消息原地刷新，层级导航，全部带返回；
- 主面板直接展开每台机器的进度条与全部关键状态，两台机器无需再点进去；
- 阈值支持 ±1% / ±5% 快调；月度额度、提醒线、汇总时间、管理员都能在 Telegram 里直接改；
- **录入 AccessKey 后立即调用阿里云 API 验活**，填错当场知道，不用等 Telegram 报错；
- 新增 `aliyun-monitor doctor` 一键自检：配置结构、文件权限、systemd、时区账期、Telegram Token、每个账号的阿里云 API 连通性；
- 终端面板顶部彩色实时状态栏；
- 重启后从 `state.json` 恢复上次读数，面板不再显示「尚未查询」；
- 修复 `python-telegram-bot 22.8` 实际要求 Python 3.10+ 导致 Debian 11 安装失败；
- 修复刷新时 Telegram「message is not modified」报错、以及一次回调重复 answer 导致提示不显示。

## 计量口径：先读这一段

CDT 统计的是**整个阿里云账号**的公网流量池，**不是**单台 ECS 的网卡计量。同账号下的其他 ECS、EIP、NAT 网关都会计入同一个数。一个账号只跑一台主力机时最准确 —— 这正是你的结构。

CDT 免费额度分两个池：**非中国内地 200 GB/月**，**中国内地 20 GB/月**（合计 220 GB）。香港、新加坡、日本、美国等地域都计入「非中国内地」池，所以海外机器用 `traffic_scope: overseas` + `quota_gb: 200`。

### GB 还是 GiB

程序按 **1 GB = 1024³ 字节**换算额度，所以 `quota_gb: 200` 对应 214.7 个十进制 GB。如果阿里云的免费额度实际是十进制口径，95% 阈值会落在约 204 GB —— 已经超出免费线。

**因此默认建议阈值 90%**（约 193 十进制 GB），两种口径下都安全。向导在你填完阈值后会直接算出对应的十进制 GB 并在越线时告警。

## 部署位置

把 Bot 装在**不会被自动关机的第三台管理服务器**上。如果和受监控 ECS 同机，关机后面板和下月自动开机都会一起离线。

## 安装

一键安装（升级也用同一条命令，配置自动保留并备份）：

```bash
curl -fsSL https://raw.githubusercontent.com/bear4f/aliyun-tg-traffic-monitor/main/install.sh | sudo bash
```

没有 curl 的系统用 wget：

```bash
wget -qO- https://raw.githubusercontent.com/bear4f/aliyun-tg-traffic-monitor/main/install.sh | sudo bash
```

或者传统方式：

```bash
git clone https://github.com/bear4f/aliyun-tg-traffic-monitor.git
cd aliyun-tg-traffic-monitor
sudo ./install.sh
```

系统要求：Debian 11+ / Ubuntu / Alpine / Rocky / AlmaLinux，Python 3.9+，root 权限。

首次安装进入向导：Telegram Token → 管理员 User ID → 全局设置 → 逐台添加机器（每台录完自动验活）。管道安装时向导会自动接回终端（/dev/tty），交互不受影响。

## 日常使用

```bash
sudo aliyun-monitor          # 交互面板
sudo aliyun-monitor doctor   # 一键自检（联网）
sudo aliyun-monitor check    # 结构校验（不联网）
sudo aliyun-monitor status   # 状态栏 + systemd 状态
sudo aliyun-monitor logs     # 跟随日志
sudo aliyun-monitor restart
```

## 哪些能在 Telegram 改，哪些不能

| 操作 | Telegram | 终端 |
|---|---|---|
| 查看流量 / 状态 | ✅ | ✅ |
| 开机 / 关机 / 重启（二次确认） | ✅ | — |
| 自动熔断开关、关机阈值 | ✅ | ✅ |
| 月度额度、下月自动开机、启用/停用 | ✅ | ✅ |
| 提醒线、汇总时间、检查间隔、管理员 | ✅ | ✅ |
| **新增 / 删除机器** | ❌ | ✅ |
| **AccessKey / Region / Instance ID** | ❌ | ✅ |

密钥相关操作只在管理服务器本地完成，**不经过 Telegram 聊天传输**。

## Telegram 命令

```text
/menu    打开控制面板
/status  刷新并打开面板
/id      查看自己的 Telegram User ID 和 Chat ID
```

## EIP 与实例安全（3.0.1 起）

针对「公网 IP 珍贵，绝不能因脚本丢失」的场景做了三层防护：

1. **代码层**：全部代码只调用 7 个 API 动作 —— 读流量、读实例状态、开机、关机、重启。不存在任何删除实例、释放/解绑 EIP、变更计费方式的调用路径。
2. **关机模式层**：`StopCharging`（节省停机）已从代码中**彻底移除** —— 该模式会回收实例的固定公网 IP。关机硬编码为 `KeepCharging`，公网 IP 与全部资源保留，不可配置。旧配置中的 `StopCharging` 会在加载时被强制改写。注意：KeepCharging 意味着关机期间实例照常计费，这是保 IP 的代价。
3. **RAM 策略层**：`ram-policy-ecs-cdt.json` 在最小 Allow 之外增加了**显式 Deny**（`ecs:DeleteInstance`、`vpc:ReleaseEipAddress`、`vpc:UnassociateEipAddress` 等）。RAM 中 Deny 永远压过 Allow —— 即使这个 RAM 用户日后被误加了更宽的权限，删机和释放 EIP 依然被挡住。

`aliyun-monitor doctor` 会输出「实例 / EIP 安全」一节，逐条确认以上防线，并在发现旧配置残留 `StopCharging` 时提醒。

## RAM 权限

每个阿里云账号单独创建 RAM 用户，**不要用主账号 AccessKey**。策略见 `ram-policy-ecs-cdt.json`：

```text
Allow:
  cdt:ListCdtInternetTraffic
  ecs:DescribeInstances
  ecs:StartInstance / StopInstance / RebootInstance
Deny（防护层，压过一切 Allow）:
  ecs:DeleteInstance / DeleteInstances
  ecs:ModifyInstanceChargeType
  vpc:ReleaseEipAddress / UnassociateEipAddress / DeleteEipAddress
```

## 熔断安全规则

- 流量查询失败时**绝不**执行自动关机；
- 实例状态不是 `Running` 时不会重复发送关机指令；
- 同账期熔断一次后不再重复触发；
- 手动开机时若流量仍高于阈值且熔断开启，会被拦截并说明原因；
- 新账期自动开机必须先确认流量已低于确认线（默认 ≤10%），而不是仅凭日历翻月；
- 阿里云控制台仍应同时配置费用预算告警，作为第二道防线。

## 文件

```text
/opt/aliyun-traffic-bot/config.json   配置与密钥（600）
/opt/aliyun-traffic-bot/state.json    告警与账期状态（600）
/opt/aliyun-traffic-bot/monitor.log   程序日志
/opt/aliyun-traffic-bot/app.py        Telegram Bot 与监控循环
/opt/aliyun-traffic-bot/panel.py      终端面板
/opt/aliyun-traffic-bot/common.py     共享的阿里云 API 与格式化层
```

服务以独立系统用户 `aliyunmon` 运行。

## 升级与卸载

```bash
sudo ./install.sh    # 保留配置并自动备份
sudo ./uninstall.sh  # 询问是否删除含密钥的配置目录
```
