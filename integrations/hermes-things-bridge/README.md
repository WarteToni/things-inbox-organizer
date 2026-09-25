# Hermes → Things 桥接（上游投递端）

本目录是 [things-inbox-organizer](../) 的**上游组件**：让 Telegram 上的
AI agent（[Hermes](https://github.com/NousResearch/hermes-agent) 或任何支持
tool calling 的 chat bot）能把对话里的"提醒我…"一键变成 Things 待办。

## 整条流水线

```
你（在 Telegram 上，语音或文字）
  「提醒我周五给老板发周报」
        │
        ▼  Hermes（服务器端，常驻）
   LLM 识别提醒意图 + 解析「周五」→ 2026-09-25
   调用工具 add_things_todo(title, when, notes)
        │
        ▼  本目录 bridge.py（作为 Hermes 插件运行）
   1. 关键词规则预分类区域（可热加载的外部规则文件）
   2. 拼 things:///add?title=...&when=2026-09-25&list=...
   3. 持久化到队列文件（fcntl 锁 + 原子写，失败重试，不丢任务）
   4. POST Bark 推送，Things URL 放在 url 字段
        │
        ▼  iPhone 收到推送 → 点一下
   Things 创建待办 → Things Cloud 秒级同步
        │
        ▼  所有设备的 Things Inbox 里出现这条待办
        │
        ▼  下游：Mac 上的 things-inbox-organizer（父仓库）
   每 2 小时归档：移入区域、修正/补设 When、清理标题
```

桥接端已设置 `when` 的条目，下游不会覆盖（下游仅在抽到日期时才写 When，
其余只做区域路由）；桥接端没带日期的条目，由下游按
[INBOX_SPEC.md](../INBOX_SPEC.md) 的语义补排期。

## 为什么是"Bark 推送 + 点一下"

iOS 沙盒不允许第三方后台无感执行，任何服务器 → iPhone 的自动化路径
天花板就是"点一下通知"。Bark 的 `url` 字段携带 `things:///add` URL Scheme，
点击即建待办，是投入产出比最高的方案（决策记录见 PROJECT 原始文档，
含 HomeKit/快捷指令等被否决的备选）。

## 安装

前置：一台常驻服务器跑 Hermes（或同类 agent）、iPhone 装
[Bark](https://apps.apple.com/app/bark/id1403753865)。

```bash
# 1. Bark key 进环境（或 Hermes 的 .env）
echo 'THINGS_BARK_KEY=你的device_key' >> ~/.hermes/.env
# 可选自建 bark-server: THINGS_BARK_HOST=https://你的域名

# 2. 部署插件（路径按你的服务器实际情况改 deploy.sh 里的 SRC/PYTHON）
bash deploy.sh

# 3. 对 agent 说「提醒我明天下午3点开会」→ iPhone 收推送 → 点一下验证
```

## 组件

| 文件 | 作用 |
|---|---|
| `plugin/bridge.py` | 核心：URL 构造、区域预分类（关键词规则，热加载）、持久队列、Bark 重试 |
| `plugin/plugin.yaml` `plugin/__init__.py` | Hermes 插件注册（注册 `add_things_todo` / `things_bridge_status` 两个工具） |
| `test_bridge.py` | 行为测试（URL 编码、分类规则、队列重试、显式 list 优先） |
| `deploy.sh` | 服务器部署脚本（编译检查 → 拷贝 → mock 注册验证 → 写入 config） |

### Things URL Scheme 参数速查

| 参数 | 说明 |
|---|---|
| `title` | 标题（必需，需 URL 编码） |
| `when` | `today/tomorrow/evening/anytime/someday` 或 ISO `YYYY-MM-DD` |
| `deadline` `reminder` | 截止日 / 精确提醒时刻（ISO） |
| `list` | 项目或区域名（按 title 匹配） |
| `notes` `tags` `checklist-items` | 备注 / 标签 / 清单（均需编码） |

注意：非法 `when` 会被 Things **静默忽略**（条目照建但无日期）——这正是下游
organizer 兜底重排日期的价值所在。

## 与下游的契约

上游写标题时遵守 [INBOX_SPEC.md](../INBOX_SPEC.md)（可直接加进 Hermes 的
system prompt）：一条一事一日期、截止写「绝对日期+前」、模糊词=故意不排期。
上游写得越规范，下游归档越准；上游写得随性，下游也有保守兜底（拿不准留
Inbox，绝不猜日期）。

## License

MIT（随父仓库）
