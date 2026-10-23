# douyin-comment-miner

> 挖掘抖音爆款视频评论区的选题与用户痛点 —— 一个可复用的 WorkBuddy Skill + 本地采集脚本。
> 一句话触发："现在扫一下 XX 的评论区"。

## 它能做什么

- 🔍 **按关键词搜近一周爆款**：自动过滤"X天/周/月前"，按点赞排序选 Top N
- 💬 **抓高赞评论**：评论区高赞置顶，默认前 40 条即覆盖主要痛点（约 25 秒/视频）
- 🏷️ **零 token 预分类**：本地正则规则把评论分为 求教程/许愿/质疑/对比/共鸣 五类，AI 只对压缩报告做一次分析
- 📊 **输出 Markdown 报告**：按点赞排序的评论表 + 分类分布，可直接喂给任意 LLM 做选题分析

## 工作原理

```
用户一句话 → WorkBuddy 调用 skill
  → Python 脚本（Playwright + 本机 Chrome，用户自己的登录态）自动滚动评论区提取文本
  → 本地规则预分类（不消耗任何大模型 token）
  → LLM 只读压缩后的报告，产出选题/痛点分析
```

**设计哲学**：采集和粗分类全部本地完成，token 只花在真正需要理解的部分。实测单视频 80 条评论 25 秒抓完，整个分析流程 LLM 只需读一张 ~80 行的表。

## 安装

```bash
pip install playwright   # 无需下载 Chromium，直接使用本机 Chrome
```

## 首次使用（登录一次，永久有效）

```bash
python douyin-comment-miner/scripts/douyin_comments.py --login
```

弹出浏览器 → 用抖音 APP 扫码登录。登录态保存在脚本目录 `.chrome-profile/`。

## 用法

```bash
# 抓指定视频（可多个）
python douyin-comment-miner/scripts/douyin_comments.py --url "https://www.douyin.com/video/xxx" "https://www.douyin.com/video/yyy"

# 按关键词搜近 7 天爆款 Top3
python douyin-comment-miner/scripts/douyin_comments.py --keyword "AI工具" --days 7 --max-videos 3
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--url` | - | 直接抓视频链接（可空格分隔多个） |
| `--keyword` | - | 搜索关键词（与 --url 二选一） |
| `--days` | 7 | 只保留近 N 天发布的视频 |
| `--max-videos` | 3 | 关键词模式抓取视频数 |
| `--max-comments` | 40 | 单视频评论上限（高赞已覆盖） |
| `--out` | 脚本目录/output | 输出目录 |

输出：`<时间戳>-comments.json`（原始数据）+ `<时间戳>-report.md`（预分类报告，LLM 直接读这份）。

## 定时自动化（可选）

配合 WorkBuddy 自动化，每周一自动扫近一周爆款：

```
每周一 09:00 运行：
python douyin_comments.py --keyword "<你的赛道>" --days 7 --max-videos 3 --max-comments 40
→ 读取 OUTPUT_MD 报告 → 五分类分析 → 选题报告写入知识库
```

## ⚠️ 合规与边界

- 抖音**没有公开的评论 API**。本工具通过驱动**用户自己登录的浏览器**访问，模拟正常浏览行为，仅限个人研究/选题用途
- **禁止**高频使用：单视频 ≤100 条、单批 ≤5 个视频，避免触发风控
- 请遵守抖音用户协议与当地法律法规，勿用于抓取他人隐私信息或商业数据转售
- 不得使用 `--headless`：无头模式会被风控（实测只能抓到 ~10 条）
- 页面结构变化可能导致选择器失效，脚本内置多候选选择器 + 兜底逻辑；失效时看 `raw` 字段调试

## License

MIT
