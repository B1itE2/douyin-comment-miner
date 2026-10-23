#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_comments.py - 抖音爆款视频评论区采集器（配合 douyin-comment-miner skill）

用法:
  # 首次使用：登录（弹出 Chrome，扫码后自动检测并继续）
  python douyin_comments.py --login

  # 按关键词搜爆款并抓评论区
  python douyin_comments.py --keyword "AI工具" --max-videos 3 --max-comments 80

  # 直接抓指定视频
  python douyin_comments.py --url "https://www.douyin.com/video/7683888221113093416"

输出: --out 目录下的 comments.json（原始数据）+ report.md（预分类报告）
"""
import argparse, asyncio, json, os, re, sys, time
from datetime import datetime

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome-profile")

# ---------------- 规则预分类（零 token 本地处理） ----------------
RULES = [
    ("求教程型", r"求|怎么|如何|教程|教教|在哪|哪.*(弄|搞|学|用)|什么.*(工具|软件|方法|教程)|请问|入门|小白|一步步|跟着做|提示词|prompt|资源|文档|怎么用|怎么弄"),
    ("许愿型",   r"要是能|有没有|希望能|能不能|什么时候出|跪求|求推荐|想要|期待|如果有|坐等|蹲.*(功能|更新|出)|希望出"),
    ("质疑型",   r"不好用|根本|收费|太贵|贵|坑|智商税|假的|不行|垃圾|难用|卡|封号|会员|付费|套路|割韭菜|忽悠|踩坑|避雷|劝退|翻车|吹"),
    ("对比型",   r"不如|还是.*(强|好|香)|比.*(好|强)|哪个.*(好|强|推荐)|和.*比|对比|更.*(好|强|推荐)|其实.*更好|感觉.*强"),
    ("共鸣型",   r"真实|我也是|确实|太对了|太能|哈哈|笑死|同感|学到了|收藏|牛|绝了|破防|泪目|人间清醒|一针见血|说得太对|终于|太对了"),
]

def classify(text):
    for name, pat in RULES:
        if re.search(pat, text):
            return name
    return "其他"

def parse_likes(s):
    s = (s or "").strip()
    if not s:
        return 0
    m = re.match(r"^([\d.]+)\s*万$", s)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.match(r"^(\d+)$", s)
    return int(m.group(1)) if m else 0

def parse_age_days(text):
    """从搜索结果卡片文本解析发布距今天数，解析失败返回 None"""
    if re.search(r"刚刚|分钟前|小时前|昨天", text):
        m = re.search(r"(\d+)\s*小时前", text)
        return 0 if not m else (1 if int(m.group(1)) >= 24 else 0)
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*周前", text)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*月前", text)
    if m:
        return int(m.group(1)) * 30
    m = re.search(r"(\d+)\s*年前", text)
    if m:
        return int(m.group(1)) * 365
    return None

# ---------------- 搜索相关性过滤（减少串入无关视频） ----------------
def _relevance_tokens(keyword):
    """生成关键词相关性 token：整词 + 中文二元组 + 长度≥2 的拉丁片段。
    用于过滤抖音搜索混排带来的离题视频（如「AI智能体」→ A股 内容）。"""
    kw = (keyword or "").strip()
    toks = set()
    if kw:
        toks.add(kw)
    # 中文二元组（滑窗），覆盖「智能/模型/编程」等子串
    chars = [c for c in kw if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(chars) - 1):
        toks.add(chars[i] + chars[i + 1])
    # 拉丁片段（长度≥2），如 AI / Agent
    for m in re.findall(r"[A-Za-z]{2,}", kw):
        toks.add(m)
    return {t for t in toks if len(t) >= 2}

def _is_relevant(title_hint, tokens):
    if not tokens:
        return True
    t = title_hint or ""
    return any(tok in t for tok in tokens)

# ---------------- 页面内提取 JS ----------------
# 抖音评论项选择器候选（页面结构会变，按优先级尝试）
EXTRACT_JS = r"""
() => {
  // 优先用 data-e2e（最精确），命中即采用；否则退到 class 模糊匹配。
  // 注意：不要用裸 [class*="CommentItem"]（任意元素），它会把评论操作栏（赞/分享/回复）
  // 也当成评论项，导致正文全变成「分享」。
  const itemSels = [
    '[data-e2e="comment-item"]',
    '[data-e2e="comment-list-item"]',
    'div[class*="CommentItem"]',
    'div[class*="commentItem"]',
    'li[class*="comment-item"]',
  ];
  let items = [];
  for (const sel of itemSels) {
    const found = Array.from(document.querySelectorAll(sel));
    if (found.length) {
      items = found;
      if (sel.startsWith('[data-e2e')) break;  // 精确选择器优先，不再比数量
    }
  }
  if (!items.length) {
    // 最终兜底：任意含 comment 的 div，且自身有实质文本
    items = Array.from(document.querySelectorAll('div[class*="comment"]'))
      .filter(el => (el.innerText || '').trim().length > 5);
  }
  return items.map(el => {
    const text = (el.innerText || '').trim();
    // 昵称：第一个账户链接或加粗 span
    let nick = '';
    const nickEl = el.querySelector('a[href*="/user/"] span, [class*="nickname"], [class*="Nickname"], [class*="name"]');
    if (nickEl) nick = nickEl.innerText.trim();
    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
    if (!nick && lines.length && lines[0].length <= 20) nick = lines[0];
    // 点赞数：找纯数字/万 的小元素（通常在右侧心形图标旁）
    let likes = '';
    const likeEl = el.querySelector('[class*="like"] span, [class*="Like"] span, [class*="digg"]');
    if (likeEl) likes = likeEl.innerText.trim();
    if (!likes) {
      const cands = Array.from(el.querySelectorAll('span,p,div'))
        .map(e => (e.innerText||'').trim())
        .filter(t => /^[\d.]+万?$/.test(t));
      if (cands.length) likes = cands[cands.length - 1];
    }
    // 时间/地点行
    const timeLine = lines.find(l => /\d+\s*(小时|天|周|月)前|昨天|刚刚|20\d\d-/.test(l)) || '';
    // 正文：排除昵称/时间/点赞/回复按钮后最长的一行
    const body = lines
      .filter(l => l !== nick && l !== timeLine && l !== likes && !/^(回复|展开|收起|查看回复)/.test(l))
      .sort((a, b) => b.length - a.length)[0] || '';
    // 防御：跳过纯操作栏（赞/分享/回复等），避免把操作行当评论
    if (!body || /^(分享|回复|赞|点赞|收藏|转发|评论|展开|收起|查看回复)$/.test(body.trim())) return null;
    return { nick, body, likes, time: timeLine, raw: text.slice(0, 300) };
  }).filter(c => c && c.body && c.body.length >= 2);
}
"""

SCROLL_JS = r"""
() => {
  // 找评论区滚动容器：评论项的可滚动祖先
  const item = document.querySelector('[data-e2e="comment-item"], div[class*="CommentItem"]');
  if (!item) return { scrolled: false, reason: 'no-comment-item' };
  let el = item.parentElement;
  while (el) {
    if (el.scrollHeight > el.clientHeight + 50 &&
        /auto|scroll/.test(getComputedStyle(el).overflowY)) {
      const before = el.scrollTop;
      el.scrollTop = el.scrollHeight;
      return { scrolled: true, before, after: el.scrollTop, max: el.scrollHeight };
    }
    el = el.parentElement;
  }
  window.scrollTo(0, document.body.scrollHeight);
  return { scrolled: true, mode: 'window' };
}
"""

# ---------------- 主流程 ----------------
async def ensure_login(page):
    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    login_sel = 'div[id*="login"], [class*="login-panel"], [class*="LoginPanel"], [class*="loginGuide"]'
    for _ in range(120):  # 最长等 6 分钟
        modal = await page.query_selector(login_sel)
        qrcode = await page.query_selector('img[class*="qrcode"], [class*="QRcode"], [class*="qr-code"]')
        if not modal and not qrcode:
            # 再看是否有登录按钮（未登录态）
            btn = await page.query_selector('button:has-text("登录"), div[class*="login-btn"]')
            if not btn:
                return True
        await page.wait_for_timeout(3000)
    return False

async def search_videos(page, keyword, max_videos, max_age_days=7):
    from urllib.parse import quote
    await page.goto(f"https://www.douyin.com/search/{quote(keyword)}?type=video",
                    wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    seen, results = set(), []
    for _ in range(12):
        links = await page.query_selector_all('a[href*="/video/"]')
        for a in links:
            href = await a.get_attribute('href') or ''
            m = re.search(r"/video/(\d+)", href)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            txt = (await a.inner_text() or '').strip().replace('\n', ' ')
            age = parse_age_days(txt)
            if age is not None and age > max_age_days:
                continue  # 只保留近 N 天
            like_m = re.search(r"([\d.]+万?)\s", txt)
            results.append({
                "url": f"https://www.douyin.com/video/{m.group(1)}",
                "likes_hint": parse_likes(like_m.group(1)) if like_m else 0,
                "age_days": age,
                "title_hint": txt[:80],
            })
        if len(results) >= max_videos * 3:  # 多收一些候选再按赞排序
            break
        await page.evaluate("window.scrollBy(0, 1200)")
        await page.wait_for_timeout(2000)
    # 相关性过滤：只保留标题命中关键词的候选，避免串入无关视频（如「AI智能体」→ A股）
    tokens = _relevance_tokens(keyword)
    filtered = [r for r in results if _is_relevant(r["title_hint"], tokens)]
    if filtered:
        results = filtered
    print(f"  search: {len(results)} relevant candidates (keyword={keyword})", file=sys.stderr)
    results.sort(key=lambda x: -x["likes_hint"])
    return results[:max_videos]

async def _navigate_to_comments(page):
    """把评论面板带到视口并触发渲染，返回是否检测到评论项。
    替代原来的 page.mouse.click(400,590)+End 硬编码坐标跳转（易失效导致 0 条）。"""
    sel = ('[data-e2e="comment-item"], [data-e2e="comment-list-item"], '
           'div[class*="CommentItem"], li[class*="comment-item"]')
    try:
        await page.wait_for_selector(sel, timeout=8000)
        return True
    except Exception:
        pass
    # 没直接出现：逐步把页面滚到底，触发评论面板懒加载渲染
    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(1200)
        try:
            await page.wait_for_selector(sel, timeout=3000)
            return True
        except Exception:
            continue
    return False

async def scrape_comments(page, url, max_comments):
    best, title = [], ""
    for attempt in range(2):  # 首次失败/过少则 reload 重试一次
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        # 视频标题
        for sel in ['h1', '[data-e2e="video-desc"]', '[class*="video-info"] [class*="title"]']:
            el = await page.query_selector(sel)
            if el:
                title = ((await el.inner_text()) or '').strip()[:120]
                if title:
                    break
        # 稳健定位评论面板（替代硬编码坐标跳转）
        await _navigate_to_comments(page)

        comments, stale = {}, 0
        for rnd in range(60):
            batch = await page.evaluate(EXTRACT_JS)
            for c in batch:
                # 去重 key 加入昵称，避免 time 为空时不同评论被误删（修复漏抓）
                key = (c["body"][:50] + "|" + c["time"] + "|" + c["nick"])
                if key not in comments:
                    c["likes_num"] = parse_likes(c["likes"])
                    c["category"] = classify(c["body"])
                    comments[key] = c
            if len(comments) >= max_comments:
                break
            r = await page.evaluate(SCROLL_JS)
            await page.wait_for_timeout(1800)
            if not r.get("scrolled") or (r.get("before") == r.get("after") == r.get("max")):
                stale += 1
                if stale >= 4:
                    break
            else:
                stale = 0
        lst = sorted(comments.values(), key=lambda x: -x["likes_num"])[:max_comments]
        if len(lst) >= min(5, max_comments) or attempt == 1:
            best = lst
            break
        print(f"  retry: only {len(lst)} comments, reloading (attempt {attempt+2})",
              file=sys.stderr)
    return {"url": url, "title": title, "comments": best}

def write_outputs(outdir, keyword, videos):
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    data = {"keyword": keyword, "fetched_at": ts, "videos": videos}
    jp = os.path.join(outdir, f"{ts}-comments.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    mp = os.path.join(outdir, f"{ts}-report.md")
    L = [f"# 评论区采集报告 {ts}", f"\n关键词：{keyword or '（指定视频）'}\n"]
    for v in videos:
        cs = v["comments"]
        L.append(f"\n---\n## 视频：{v['title'] or v['url']}\n{v['url']} ｜ 采集评论 {len(cs)} 条\n")
        # 分类汇总
        from collections import Counter
        cnt = Counter(c["category"] for c in cs)
        L.append("分类分布：" + "，".join(f"{k} {v2}" for k, v2 in cnt.most_common()) + "\n")
        L.append("| 赞 | 分类 | 评论 | 用户 | 时间 |")
        L.append("|---|---|---|---|---|")
        for c in cs:
            body = c["body"].replace("|", "｜").replace("\n", " ")[:60]
            L.append(f"| {c['likes_num']} | {c['category']} | {body} | {c['nick']} | {c['time']} |")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return jp, mp

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", nargs="+", help="抖音视频链接（可多个，空格分隔）")
    ap.add_argument("--keyword", help="搜索关键词")
    ap.add_argument("--days", type=int, default=7, help="关键词模式只保留近 N 天发布的视频（默认 7）")
    ap.add_argument("--max-videos", type=int, default=3)
    ap.add_argument("--max-comments", type=int, default=40,
                    help="单视频评论上限（评论区高赞置顶，默认 40 已覆盖高赞痛点）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    ap.add_argument("--login", action="store_true", help="只做登录（扫码后自动退出）")
    ap.add_argument("--headless", action="store_true", help="无头模式（登录态已存在时才可用）")
    args = ap.parse_args()

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=args.headless,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        ok = await ensure_login(page)
        if not ok:
            print("ERROR: 登录超时，请重试", file=sys.stderr)
            await ctx.close(); sys.exit(1)
        print("LOGIN_OK", flush=True)
        if args.login:
            await ctx.close(); return

        videos = []
        targets = args.url if args.url else None
        if not targets:
            found = await search_videos(page, args.keyword, args.max_videos, args.days)
            print(f"SEARCH_OK {len(found)} videos (近{args.days}天)", flush=True)
            targets = [v["url"] for v in found]
        for u in targets:
            print(f"SCRAPE {u}", flush=True)
            try:
                videos.append(await scrape_comments(page, u, args.max_comments))
                print(f"DONE {len(videos[-1]['comments'])} comments", flush=True)
            except Exception as e:
                print(f"WARN scrape failed: {e}", file=sys.stderr)
        jp, mp = write_outputs(args.out, args.keyword or "", videos)
        print(f"OUTPUT_JSON {jp}")
        print(f"OUTPUT_MD {mp}")
        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
