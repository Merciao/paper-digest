#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次设置向导：python setup.py
屏 0-7：账号邮箱 → 研究方向 → 种子论文 → 反向示例 → 领域体量探测 → 推送设置 → 试跑 → 完成。
迁移现有单用户实例：python setup.py --migrate（把根目录 config/+data/ 迁入 users/<hash>/ 并登记）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402
import accounts  # noqa: E402
import workspace as ws  # noqa: E402
import profile_compiler as pc  # noqa: E402
import ingest  # noqa: E402

PAPER_TYPES = ["empirical", "review", "intervention", "theoretical", "methods"]
TYPE_ZH = {"empirical": "实证研究", "review": "综述", "intervention": "干预方案",
           "theoretical": "理论", "methods": "方法学"}

# 领域体量三档参数包（spec 第六节）
PACK_SMALL = {"new_pool_days": 30, "classic_min_citations": 40, "strictness": "wide",
              "classic_candidates_per_keyword": 4, "relevance_min": 7}
PACK_MEDIUM = {"new_pool_days": 21, "classic_min_citations": 60, "strictness": "balanced",
               "classic_candidates_per_keyword": 4, "relevance_min": 7}
PACK_LARGE = {"new_pool_days": 14, "classic_min_citations": 100, "strictness": "strict",
              "classic_candidates_per_keyword": 6, "relevance_min": 8}


def default_settings():
    """新账号的默认配置（push/mix/taste/filters/sources 等；领域探测会覆盖 sources 与 taste 部分值）"""
    return {
        "push": {"time": "08:30", "timezone": "Asia/Shanghai", "count": 3},
        "mix": {"recent": 2, "classic": 1, "tracked_author_extra": True},
        "taste": {"paper_types": ["empirical", "review", "intervention"],
                  "strictness": "balanced", "relevance_min": 7, "novelty_min": 4},
        "filters": {"h_index_min": 0},
        "profile_version": 1,
        "domain": {"setup_completed": False},
        "llm": {"base_url": "https://api.kimi.com/coding/v1", "model": "k3-256k"},
        "email": {"smtp_server": "smtp.163.com", "smtp_port": 465},
        "pages_base": "",
        "send_empty": True,
        "sources": {"fields_of_study": ["Sociology", "Psychology", "Medicine"],
                    "new_pool_days": 21, "new_pool_max_candidates": 30,
                    "classic_min_citations": 60, "classic_per_keyword": 40,
                    "classic_candidates_per_keyword": 4},
        "tracked_authors": [],
        "author_pool": {"relevance_min": 5},
    }


def save_settings(s):
    profile = pc.load_profile() or {}
    if profile.get("version"):
        s["profile_version"] = profile["version"]
    ws.settings_path().write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def valid_email(email):
    return bool(re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", email or ""))


def read_multiline(prompt):
    print(prompt)
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines).strip()


# ---------- 领域体量探测 ----------

def detect_domain_scale(query_templates, api_key):
    """top5 检索串：近30天新增量 + 被引≥40 经典量估算 → 档位与参数包。失败回落 MEDIUM"""
    templates = (query_templates or [])[:5]
    if not templates:
        print("  无检索串，使用保守默认值（中等领域）")
        return "中等", PACK_MEDIUM, 0, 0
    new_ids, classic_ids = set(), set()
    try:
        for i, q in enumerate(templates, 1):
            print(f"  检索串 {i}/{len(templates)} ……")
            data = m.s2_tolerant("GET", f"{m.S2}/paper/search/bulk", api_key, params={
                "query": q, "fields": "paperId",
                "publicationDateOrYear": f"{date.today() - timedelta(days=30)}:{date.today()}",
                "limit": 100})
            for p in data.get("data", []):
                new_ids.add(p["paperId"])
            time.sleep(1)
            data = m.s2_tolerant("GET", f"{m.S2}/paper/search/bulk", api_key, params={
                "query": q, "fields": "paperId,citationCount",
                "sort": "citationCount:desc", "limit": 100})
            for p in data.get("data", []):
                if (p.get("citationCount") or 0) >= 40:
                    classic_ids.add(p["paperId"])
            time.sleep(1)
    except Exception as e:
        print(f"  探测未完成（{e}），已用保守默认值（中等领域）")
        return "中等", PACK_MEDIUM, 0, 0
    n_new, n_classic = len(new_ids), len(classic_ids)
    if n_new > 200:
        return "大领域", PACK_LARGE, n_new, n_classic
    if n_new < 10 or n_classic < 50:
        return "小领域", PACK_SMALL, n_new, n_classic
    return "中等", PACK_MEDIUM, n_new, n_classic


def apply_pack(settings, pack):
    settings["sources"]["new_pool_days"] = pack["new_pool_days"]
    settings["sources"]["classic_min_citations"] = pack["classic_min_citations"]
    settings["sources"]["classic_candidates_per_keyword"] = pack["classic_candidates_per_keyword"]
    settings["taste"]["strictness"] = pack["strictness"]
    settings["taste"]["relevance_min"] = pack["relevance_min"]


# ---------- 屏 2 作者候选统一处理 ----------

def resolve_author(api_key, name):
    """按名搜 S2 作者，返回 (name, hIndex, authorId) 或 None"""
    data = m.s2_tolerant("GET", f"{m.S2}/author/search", api_key,
                         params={"query": name, "fields": "name,hIndex", "limit": 1})
    hits = data.get("data", [])
    if hits:
        a = hits[0]
        return a.get("name", name), a.get("hIndex"), a.get("authorId")
    return None


def ask_tracked_authors(settings, api_key, candidates):
    if not candidates:
        return
    print("\n这些论文中反复出现的作者：")
    resolved = []
    for c in candidates[:5]:
        r = resolve_author(api_key, c.get("name", ""))
        h = f"h-index {r[1]}" if r and r[1] is not None else "h-index 未知"
        resolved.append((c, r))
        print(f"  {len(resolved)}. {c.get('name')}（出现 {c.get('cited_count')} 次，{h}）")
        time.sleep(1)
    v = input("输入编号添加追踪（逗号分隔，回车跳过）：").strip()
    for part in v.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(resolved):
            cand, r = resolved[int(part) - 1]
            if r and r[2]:
                authors = settings.setdefault("tracked_authors", [])
                if not any(a["id"] == r[2] for a in authors):
                    authors.append({"name": r[0], "id": r[2]})
                    print(f"  已添加追踪：{r[0]}（ID {r[2]}）")


# ---------- 向导主流程 ----------

def wizard():
    m.load_dotenv()
    api_key = os.environ.get("S2_KEY", "")
    print("""╔══════════════════════════════════════════════╗
║   论文每日推送 Agent · 首次设置向导（约 5 分钟） ║
╚══════════════════════════════════════════════╝

这个工具每天自动筛选你研究领域值得读的论文，推送到你的邮箱。""")

    # ---- 屏 0 · 账号 ----
    print("\n第 1 步：你的邮箱（推送目的地，也是你的账号标识）")
    while True:
        email = input("> ").strip().lower()
        if not valid_email(email):
            print("邮箱格式不对，请重输")
            continue
        if accounts.find_account(email):
            if input("该邮箱已有账号，进入修改模式？[y/N]：").strip().lower() == "y":
                ws.use_account(ws.USERS_DIR / accounts.hash_email(email))
                subprocess.run([sys.executable, str(ROOT / "configure.py")])
                return
            continue
        break
    account = accounts.create_account(email)
    ws.use_account(ws.USERS_DIR / account["dir"])
    print(f"账号已创建（目录 users/{account['dir']}/）")

    # ---- 屏 1 · 研究方向 ----
    text = read_multiline("""
第 2 步：你的研究方向

用几段自然语言描述，越像聊天越好。可以包括：
· 你关心的研究问题   · 想追踪的方法/理论   · 明确不想看的方向
（任何领域都行，中英文均可；输入完回车两次结束）""")
    if not text:
        print("未输入研究方向，向导中止。可重新运行 python setup.py")
        return
    draft = None
    while True:
        result = pc.compile_update(lambda msgs, **kw: m.llm_chat(default_settings(), msgs, **kw),
                                   draft, text, "increment", "user_text",
                                   domain_context=text)
        print(f"\n📋 这是我从你的描述里读出的画像：\n\n{result.get('preview_text', '(无预览)')}")
        for q in result.get("clarifying_questions") or []:
            print(f"  ❓ {q}")
        op = input("\n[y] 确认   [e] 补充修改   [q] 放弃：").strip().lower()
        if op == "y":
            pc.save_profile(result["new_profile"])
            print(f"✅ 画像 v{result['new_profile']['version']} 已保存")
            break
        if op == "e":
            extra = read_multiline("补充你的修改（回车两次结束）：")
            if extra:
                text = f"{text}\n{extra}"
                draft = result["new_profile"]
            continue
        print("已放弃，向导中止。可重新运行 python setup.py")
        return

    settings = default_settings()

    # ---- 屏 2 · 种子论文 ----
    print("""
第 3 步：种子论文（可选，但强烈建议给 1-3 篇）

给我你觉得「最对口」的论文——它们会成为筛选的校准标准，
比任何文字描述都准。可以输入 DOI、Semantic Scholar 链接，
或直接粘贴摘要。每行一篇，空行结束。""")
    seeds = read_multiline(">")
    all_candidates = []
    if seeds:
        for line in seeds.splitlines():
            ref = line.strip()
            if not ref:
                continue
            sig = ingest.run_ingest(settings, api_key, None, ref=ref,
                                    anchor_source="seed", ask_authors=False)
            if sig:
                all_candidates.extend(sig.get("cited_author_candidates") or [])
        # 作者候选汇总统一问（S2 解析真实 h-index）
        seen, uniq = set(), []
        for c in all_candidates:
            if c.get("name") and c["name"] not in seen:
                seen.add(c["name"])
                uniq.append(c)
        ask_tracked_authors(settings, api_key, uniq)

    # ---- 屏 3 · 反向示例 ----
    neg = input("""
第 4 步：不想看的（可选）

有没有典型「不要被推」的方向？一句话即可，回车跳过。
例如「纯药理的」「只要英文文献以外的别来」。
> """).strip()
    if neg:
        pc.apply_update_interactive(lambda msgs, **kw: m.llm_chat(settings, msgs, **kw),
                                    neg, mode="increment", source_tag="user_text")

    # ---- 屏 4 · 领域体量探测 ----
    print("\n第 5 步：正在探测你的领域体量（约 1-2 分钟）……")
    profile = pc.load_profile()
    scale, pack, n_new, n_classic = detect_domain_scale(profile.get("query_templates"), api_key)
    print(f"""
探测结果：
· 近 30 天新增候选：约 {n_new} 篇
· 经典池（被引≥40）候选：约 {n_classic} 篇
· 判定：{scale}

推荐参数：
  新论文窗口   {pack['new_pool_days']} 天
  经典被引门槛 {pack['classic_min_citations']}
  视野宽度     {pack['strictness']}
  相关度线     {pack['relevance_min']}""")
    if input("\n[y] 采纳推荐   [c] 逐项调整：").strip().lower() == "c":
        v = input(f"  新论文窗口天数（{pack['new_pool_days']}）：").strip()
        if v.isdigit():
            pack["new_pool_days"] = int(v)
        v = input(f"  经典被引门槛（{pack['classic_min_citations']}）：").strip()
        if v.isdigit():
            pack["classic_min_citations"] = int(v)
        v = input(f"  视野宽度 strict/balanced/wide（{pack['strictness']}）：").strip()
        if v in ("strict", "balanced", "wide"):
            pack["strictness"] = v
        v = input(f"  相关度线（{pack['relevance_min']}）：").strip()
        if v.isdigit():
            pack["relevance_min"] = int(v)
    apply_pack(settings, pack)

    # ---- 屏 5 · 推送设置 ----
    print("""
第 6 步：推送偏好（直接回车用默认值）""")
    v = input("  每日篇数 [默认 3]：").strip()
    if v.isdigit() and int(v) > 0:
        settings["push"]["count"] = int(v)
    v = input("  推送时间 [默认 08:30]：").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", v):
        settings["push"]["time"] = v
    v = input("""  内容偏好（逗号选择）[默认 1,2,3]：
    1 实证研究  2 综述  3 干预/方案  4 理论  5 方法学
  > """).strip()
    if v:
        chosen = [PAPER_TYPES[int(x) - 1] for x in v.split(",")
                  if x.strip().isdigit() and 1 <= int(x.strip()) <= len(PAPER_TYPES)]
        if chosen:
            settings["taste"]["paper_types"] = chosen
    settings["domain"]["setup_completed"] = True
    save_settings(settings)
    print("✅ 推送偏好已保存")
    print("提示：GitHub Actions 的定时是整个仓库统一的（.github/workflows/daily.yml），"
          "多账号共用同一 cron，无需单设；本机定时可用 python configure.py 的推送时间菜单改 crontab。")

    # ---- 屏 6 · 试跑 ----
    if input("""
第 7 步：立即试跑一次？

[y] 现在试跑（约 2-3 分钟，给你看今天会收到的内容，不发邮件不记账）
[n] 不了，明天直接收邮件
> """).strip().lower() == "y":
        m.run_pipeline(dry_run=True)
        print(f"\n预览文件：{ws.preview_path()}（浏览器打开查看）")

    # ---- 屏 7 · 完成 ----
    profile = pc.load_profile() or {}
    print(f"""
✅ 设置完成！

· 账号：{email}（目录 users/{account['dir']}/）
· 画像 v{profile.get('version', '?')}：{len(profile.get('topics') or [])} 个主题 · {len(profile.get('query_templates') or [])} 条检索串 · {len(profile.get('calibration_anchors') or [])} 个校准锚点
· 每天 {settings['push']['time']} 推送 {settings['push']['count']} 篇到邮箱
· 修改任何设置：python configure.py

注意：Semantic Scholar 对中文期刊覆盖有限，若你的领域以中文文献为主，
建议种子论文给足英文代表作，或后续通过反馈逐步校准。""")


# ---------- 迁移现有单用户实例 ----------

def migrate():
    m.load_dotenv()
    if not (ROOT / "config").exists() and not (ROOT / "data").exists():
        print("根目录没有 config/ 或 data/，无需迁移（或已迁移过）")
        return
    default_receiver = os.environ.get("EMAIL_RECEIVER", "")
    email = input(f"迁移账号邮箱（回车用 .env 中的 {default_receiver}）：").strip().lower() or default_receiver
    if not valid_email(email):
        print("邮箱格式不对，迁移中止")
        return
    if accounts.find_account(email):
        print("该邮箱已有账号，如需重复迁移请先删除 users/ 下对应目录与登记")
        return
    account = accounts.create_account(email)
    base = ws.USERS_DIR / account["dir"]
    for name in ("config", "data"):
        src = ROOT / name
        if src.exists():
            dst = base / name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
            print(f"已迁移 {name}/ → users/{account['dir']}/{name}/")
    ws.use_account(base)
    s = json.loads(ws.settings_path().read_text(encoding="utf-8"))
    s.setdefault("domain", {})["setup_completed"] = True
    save_settings(s)
    print(f"✅ 迁移完成：{email} 的配置与账本已进 users/{account['dir']}/，之后以多账号模式运行")


def main():
    if "--migrate" in sys.argv:
        migrate()
    else:
        wizard()


if __name__ == "__main__":
    main()
