# things-inbox-organizer

用 LLM 自动整理 [Things 3](https://culturedcode.com/things/) 的 Inbox：判断区域、
**从自然语言里抽取真实日期**排进 When、清理标题。你的收集箱从"堆放区"变回"进站口"。

```
你（或你的 AI 助理）随手投一条:   "9/25 14:52 接人：G1234 到深圳北站"
                                          │
                                   本工具每 2 小时运行
                                          │
Things 里自动出现:   区域「家人」中，When=9/25，标题「14:52 接人：G1234 到深圳北站」
```

## 解决什么问题

GTD 的规则是"收集要快，整理要定期"，但现实是：

- 待办从四面八方进来（手机速记、Siri、Telegram bot、邮件），Inbox 越堆越多
- 手动整理 = 判断区域 + 算日期 + 改标题，每条 10 秒，攒 20 条就懒得动了
- 已有的 AI 整理方案普遍把日期简化成 `今天/近期/ someday` 三档 ——
  **条目里写的真实日期（"9/25"、"下周三"）根本没被读**，排期系统性错位

本项目的核心就是第三点：让 LLM 输出 `YYYY-MM-DD`，由代码校验后用 AppleScript
`schedule` 到那一天。相对日期以**条目创建日**为基准换算（不是运行日），
所以网络故障、Mac 关机导致的延迟运行不会算错。

## 快速开始

```bash
git clone https://github.com/WarteToni/things-inbox-organizer.git
cd things-inbox-organizer

# 1. 授权终端控制 Things（首次会弹窗）
osascript -e 'tell application "Things3" to return name of list "Inbox"'

# 2. 填 API Key（智谱 GLM，免费额度即可跑）
cp /dev/null ~/.things-organizer/.env   # 或直接创建
echo 'THINGSORG_API_KEY=你的Key' >> ~/.things-organizer/.env

# 3. 试运行（不动数据，看日志）
python3 things_inbox_organizer.py --dry-run
tail -20 ~/Library/Logs/things-organizer.log

# 4. 满意后装成定时任务（8:00-22:00 每 2 小时）
bash install.sh
```

## 配置

所有私人配置都在 `~/.things-organizer/`，不进 git：

**`.env`** — API Key：

```
THINGSORG_API_KEY=sk-xxx
```

**`areas.json`** — 你的区域体系（key 是 LLM 用的代号，name 必须与 Things
里的区域名**完全一致**，desc 是给 LLM 的判断依据）：

```json
{
  "WORK":  {"name": "Work",    "desc": "工作/公司/项目相关"},
  "FAMILY":{"name": "Family",  "desc": "家庭/家人/生活杂事"},
  "IDEAS": {"name": "Ideas",   "desc": "想法/调研/将来可能做的事"}
}
```

## 日期语义

| 标题里写 | 结果 |
|---|---|
| `9/25`、`9月25日`、`2026-10-08` | When = 那天 |
| `明天`、`3天后`、`下周三` | 按条目创建日换算 |
| `10月初`、`下个月` | 该月 1 日 |
| `9/25-9/28 出差` | 开始日 |
| `9/30 前发出` | 不占 When，写进 notes `[截止]` 行 |
| `有空`、`这周找个时间` | 无日期，只归区域（**绝不猜**） |
| 日期已过去 | 不设 When，标题原样保留，日志记 WARN |

具体行为细节见 [INBOX_SPEC.md](INBOX_SPEC.md)。

## 与 AI 协作

这个项目本身就是一个三段式 AI 流水线的一环：

```
[上游] 任意 AI 助理（手机/Telegram/邮件 bot）
        按 INBOX_SPEC.md 的规范把想法投进 Things Inbox
        （INBOX_SPEC.md 可直接作为它的 system prompt）
          │
[本项目] GLM 抽取 area + date + is_deadline + 清理标题
          │
[下游] Things 3 —— 单一事实来源，人只在 Today 视图出现
```

**为什么需要上游规范**：LLM 日期抽取的准确率高度依赖写法。`9/30 前发出`
能被正确识别为 deadline，`国庆前发出` 就不行（节日不会被换算成日期）。
INBOX_SPEC.md 把这些实测边界固化成了 7 条投递规则，让上游 AI 产出
下游 AI 稳定可解析的格式。

**用 coding agent 维护本项目**：launchd 直接运行仓库里的源码（无副本、
无构建），所以 Claude Code / Cursor 等 agent 的工作流极简：

1. 看日志定位问题：`tail -50 ~/Library/Logs/things-organizer.log`
2. 改 `things_inbox_organizer.py`（主要是改 prompt）
3. `python3 things_inbox_organizer.py --dry-run` 实测
4. 满意即完成 —— 下次定时触发自动用新代码

## 已知限制

- **循环任务**（"每周六抢XX"）无法自动设重复 —— Things 的 AppleScript
  字典没有 `recurrence` 属性，条目会归入区域但不排期，需手动设一次重复
- Things 只有日期粒度，**时刻**（14:52）保留在标题里
- 需要 Things 3（Mac 版）+ macOS 自动化权限（首次运行授权一次）
- 经聊天工具（Telegram 等）传输本项目文件后，plist 可能带
  `com.apple.quarantine`，launchd 拒载报 `error 5`；
  `xattr -d com.apple.quarantine <plist>` 解决（install.sh 已内置）

## 设计笔记：为什么不让 LLM 输出"三档"

初版让 LLM 选 `today / soon / someday`，代码把三档映射成
"运行当天 / 明天 / Someday 列表"。上线两周后复盘日志：**12 条带日期的
归档几乎全错 1-3 天** —— 因为 LLM 选的"档位"和条目里的真实日期毫无关系，
`9/25 的任务在 9/22 运行时被设成 9/23`。

教训：**LLM 负责理解（抽取结构化日期），代码负责执行（校验 + 排期）**。
凡是有精确语义的字段，不要让 LLM 做离散近似；宁可让它输出空值走保守路径
（留在 Inbox / 不设日期），也不要接受一个"猜的"。

## License

[MIT](LICENSE)
