#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日论文推送 Agent（通用版，不限学科；单用户 / 多账号）
数据源 Semantic Scholar（provider 接口抽象，arXiv 等更多源见 roadmap）；
三个池：新近论文 / 高引经典 / 追踪作者（h-index 过滤可选）。
Kimi 依据结构化兴趣画像（含用户校准锚点 calibration_anchors）打 相关度+新颖度 双分并输出
paper_type / matched_entries / matched_anchor / overview_seed；为入选论文生成中文概览页，
邮件推送 + GitHub Pages 托管概览页。配置统一走 settings.json（python configure.py 交互维护）。
路径经 workspace 抽象：单用户=根目录 config/+data/；多账号=users/<email_hash>/。
"""
import argparse
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from pathlib import Path
from urllib.parse import quote

import requests

import workspace as ws
from profile_compiler import domain_context_of

ROOT = ws.ROOT
DOCS_DIR = ws.DOCS_DIR

S2 = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,abstract,authors,venue,year,citationCount,influentialCitationCount,externalIds,openAccessPdf,publicationDate"

PAPER_TYPES = ["empirical", "review", "intervention", "theoretical", "methods"]


def load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()


def load_settings():
    return json.loads(ws.settings_path().read_text(encoding="utf-8"))


def load_profile():
    return json.loads(ws.profile_path().read_text(encoding="utf-8"))


def load_config():
    return load_settings(), load_profile()


def profile_digest(profile):
    """画像精简版：供打分/概览 prompt 锚定（含用户校准锚点）"""
    lines = ["【核心研究问题】"]
    for i, q in enumerate(profile.get("core_questions") or [], 1):
        lines.append(f"{i}. {q}")
    lines.append("【主题词（括号内为权重 0-1）】")
    for t in sorted(profile.get("topics") or [], key=lambda x: -x.get("weight", 0)):
        lines.append(f"- {t['name']}（{t.get('weight', 0.8)}）")
    neg = profile.get("negative") or []
    if neg:
        lines.append("【排除方向】")
        for n in neg:
            lines.append(f"- {n}")
    anchors = profile.get("calibration_anchors") or []
    if anchors:
        lines.append("【校准锚点（用户亲自提供/确认的对口样例）】")
        for a in anchors:
            lines.append(f"- 《{a.get('paper_title', '')}》（score {a.get('score', 9)}）{a.get('note', '')}")
    return "\n".join(lines)


def load_ledger():
    p = ws.ledger_path()
    if p.exists():
        return set(json.loads(p.read_text(encoding="utf-8")))
    return set()


def save_ledger(pushed):
    p = ws.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(pushed), ensure_ascii=False, indent=1), encoding="utf-8")


def load_rejected():
    p = ws.rejected_path()
    if p.exists():
        return set(json.loads(p.read_text(encoding="utf-8")))
    return set()


def save_rejected(rejected):
    p = ws.rejected_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(rejected), ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- Semantic Scholar ----------

def s2_request(method, url, api_key, retries=6, **kwargs):
    headers = {"x-api-key": api_key} if api_key else {}
    resp = None
    for attempt in range(retries):
        resp = requests.request(method, url, headers=headers, timeout=40, **kwargs)
        if resp.status_code == 429:
            wait = (30 if api_key else 45) * (attempt + 1) + random.uniform(0, 5)
            print(f"  Semantic Scholar 限流，{wait:.0f}s 后重试…")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def s2_tolerant(method, url, api_key, **kwargs):
    try:
        return s2_request(method, url, api_key, **kwargs)
    except requests.RequestException as e:
        print(f"  请求失败（{e}），跳过")
        return {}


def norm_paper(p):
    ext = p.get("externalIds") or {}
    pdf = (p.get("openAccessPdf") or {}).get("url") or ""
    doi = ext.get("DOI", "")
    url = f"https://doi.org/{doi}" if doi else f"https://www.semanticscholar.org/paper/{p['paperId']}"
    authors = p.get("authors") or []
    return {
        "id": p["paperId"],
        "title": p.get("title") or "(无标题)",
        "abstract": (p.get("abstract") or "")[:1200],
        "authors": ", ".join(a.get("name", "") for a in authors[:6]),
        "author_ids": [a["authorId"] for a in authors if a.get("authorId")],
        "venue": p.get("venue") or "",
        "year": p.get("year"),
        "citations": p.get("citationCount") or 0,
        "influential": p.get("influentialCitationCount") or 0,
        "doi": doi,
        "url": url,
        "pdf": pdf,
        "pubdate": p.get("publicationDate") or "",
        "pool": "new",
    }


def enrich_metadata(papers):
    """摘要/期刊缺失时，用 OpenAlex（免费）按 DOI 补拉真实摘要与来源"""
    for p in papers:
        if p["abstract"] or not p.get("doi"):
            continue
        try:
            resp = requests.get(f"https://api.openalex.org/works/doi:{p['doi']}",
                                params={"mailto": "paper-digest@local"}, timeout=20)
            if resp.status_code != 200:
                continue
            w = resp.json()
            inv = w.get("abstract_inverted_index") or {}
            if inv:
                pos = {i: w_ for w_, is_ in inv.items() for i in is_}
                p["abstract"] = " ".join(pos[i] for i in sorted(pos))[:1200]
            if not p["venue"]:
                src = ((w.get("primary_location") or {}).get("source") or {})
                p["venue"] = src.get("display_name") or ""
            if not p["pdf"]:
                p["pdf"] = (w.get("best_oa_location") or {}).get("pdf_url") or ""
            if p["abstract"]:
                print(f"  OpenAlex 补到摘要: {p['title'][:50]}")
        except requests.RequestException:
            pass
        time.sleep(0.3)


# ---------- 数据源 provider 接口 ----------
# 抽象目的：main 流程只依赖 provider 的三个方法。roadmap：ArxivProvider（arXiv RSS/API，
# 适合计算机/物理等领域）、PubmedProvider；新领域用户在 settings 里切换 provider 即可。

class PaperProvider:
    """数据源接口：实现三个论文池的拉取"""

    def fetch_new_pool(self, settings, profile, api_key):
        raise NotImplementedError

    def fetch_classic_pool(self, settings, profile, api_key):
        raise NotImplementedError

    def fetch_author_pool(self, settings, api_key):
        raise NotImplementedError


class S2Provider(PaperProvider):
    """Semantic Scholar 实现（社会学/跨学科覆盖好，带引用数）"""

    def fetch_new_pool(self, settings, profile, api_key):
        days = settings["sources"]["new_pool_days"]
        date_range = f"{date.today() - timedelta(days=days)}:{date.today()}"
        seen = {}
        for kw in profile.get("query_templates") or []:
            print(f"新论文池检索: {kw}")
            data = s2_tolerant("GET", f"{S2}/paper/search/bulk", api_key, params={
                "query": kw, "fields": S2_FIELDS,
                "fieldsOfStudy": ",".join(settings["sources"]["fields_of_study"]),
                "publicationDateOrYear": date_range, "limit": 50,
            })
            for p in data.get("data", []):
                seen[p["paperId"]] = p
            time.sleep(1)
        papers = sorted(seen.values(), key=lambda p: p.get("publicationDate") or "", reverse=True)
        return [norm_paper(p) for p in papers[: settings["sources"]["new_pool_max_candidates"]]]

    def fetch_classic_pool(self, settings, profile, api_key):
        """按关键词配额：每个关键词保留被引 top N，保证小领域关键词的经典也能入池"""
        per_kw = settings["sources"].get("classic_per_keyword", 40)
        out = []
        for kw in profile.get("query_templates") or []:
            print(f"经典池检索: {kw}")
            data = s2_tolerant("GET", f"{S2}/paper/search/bulk", api_key, params={
                "query": kw, "fields": S2_FIELDS,
                "fieldsOfStudy": ",".join(settings["sources"]["fields_of_study"]),
                "sort": "citationCount:desc", "limit": 50,
            })
            papers = sorted(data.get("data", []), key=lambda p: p.get("citationCount") or 0, reverse=True)
            for p in papers[:per_kw]:
                n = norm_paper(p)
                n["pool"] = "classic"
                n["kw"] = kw
                out.append(n)
            time.sleep(1)
        return out

    def fetch_author_pool(self, settings, api_key):
        """追踪作者近 N 天的新论文"""
        days = settings["sources"]["new_pool_days"]
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        out = []
        for au in settings.get("tracked_authors") or []:
            print(f"追踪作者检索: {au['name']}")
            data = s2_tolerant("GET", f"{S2}/author/{au['id']}/papers", api_key,
                               params={"fields": S2_FIELDS, "limit": 50})
            for p in data.get("data", []):
                pd = p.get("publicationDate")
                recent = (pd and pd >= cutoff) or (not pd and p.get("year") and p["year"] >= date.today().year)
                if recent:
                    n = norm_paper(p)
                    n["pool"] = "author"
                    n["tracked_author"] = au["name"]
                    out.append(n)
            time.sleep(1)
        return out


def get_provider(settings):
    """按配置取数据源 provider（当前仅 s2；roadmap：arxiv / pubmed）"""
    return S2Provider()


def apply_hindex_filter(settings, api_key, papers):
    """过滤新论文池：作者中最高 h-index 低于门槛则丢弃（h 未知则保留）"""
    threshold = settings.get("filters", {}).get("h_index_min") or 0
    if threshold <= 0:
        return papers
    targets = [p for p in papers if p["pool"] == "new" and p["author_ids"]]
    ids = sorted({aid for p in targets for aid in p["author_ids"]})
    hmap = {}
    for i in range(0, len(ids), 500):
        data = s2_tolerant("POST", f"{S2}/author/batch", api_key,
                           params={"fields": "name,hIndex"}, json={"ids": ids[i:i + 500]})
        for a in data if isinstance(data, list) else []:
            if a:
                hmap[a["authorId"]] = a.get("hIndex")
        time.sleep(1)
    kept, dropped = [], 0
    for p in papers:
        if p["pool"] != "new":
            kept.append(p)
            continue
        hs = [hmap[a] for a in p["author_ids"] if hmap.get(a) is not None]
        p["max_hindex"] = max(hs) if hs else None
        if p["max_hindex"] is None or p["max_hindex"] >= threshold:
            kept.append(p)
        else:
            dropped += 1
    print(f"h-index≥{threshold} 过滤：丢弃 {dropped} 篇")
    return kept


# ---------- Kimi ----------

def llm_chat(settings, messages, retries=3, max_tokens=8192):
    llm = settings["llm"]
    key = os.environ.get("MOONSHOT_API_KEY", "")
    if not key:
        sys.exit("缺少 MOONSHOT_API_KEY（见 .env.example）")
    resp = None
    for attempt in range(retries):
        resp = requests.post(
            f"{llm['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": llm["model"], "messages": messages,
                  "max_tokens": max_tokens, "reasoning_effort": "low"},
            timeout=180,
        )
        if resp.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    resp.raise_for_status()


def parse_json_array(text):
    start = text.find("[")
    try:
        return json.loads(text[start:]) if start >= 0 else []
    except json.JSONDecodeError:
        return []


def parse_json_object(text):
    start = text.find("{")
    end = text.rfind("}")
    try:
        return json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except json.JSONDecodeError:
        return {}


SCORING_PROMPT = """你是文献筛选评分器。依据用户的结构化兴趣画像，判断一篇论文是否值得推送。你只输出 JSON。

# 领域上下文
{domain_context}
仅用于理解语义。

# 输入
1. profile_digest：含 core_questions、topics（带权重）、negative、calibration_anchors（见下）
2. papers：本批论文列表（每篇：标题、摘要、期刊、年份、被引数、所属池 recent/classic/tracked_author）
3. thresholds：当前分数线（相关度、新颖度）

# 评分规则
## 相关度（0-10）
### 用户锚点（最高优先级）
calibration_anchors 是用户亲自提供或确认过的「标准像」。逐篇比对：
- 与 score≥9 锚点在**研究问题层面**同类（同一问题、或同一问题的不同方法/场景/人群）→ 相关度 8-10；
- 与锚点仅同大领域、问题不同 → 按通用锚点正常评；
- 若 reason 中确有关联，可写「与你提供的种子论文《X》研究问题一致」——但必须属实，不允许攀附。
### 通用锚点（无用户锚点命中时使用）
- 直接命中 core_questions 中任一条 → 9-10
- 命中高权重 topics 但不直接回答 core_questions → 7-8
- 邻近领域（同大领域、问题相邻但视角/人群/场景不同）→ 4-6
- 仅沾边大领域、与 core_questions 和 topics 均无实质关联 → ≤3
- **命中 negative 任一条目 → 相关度 ≤5 且 negative_hit=true**，无论其他维度多强
- 所属论文池不影响分数；tracked_author 池的降门槛由代码处理，你诚实打分
## 新颖度（0-10）
锚定该领域已有文献：重复验证已知结论 = 1-3；新场景/新人群/新要素组合 = 4-6；提出新框架、新方法或挑战既有共识 = 7-10。不轻信作者自称的创新，看摘要里实际呈现的差异。经典池论文按「发表当年」的新颖度评判。

# 输出（严格 JSON）
{{
  "scores": [
    {{
      "id": "输入中的论文 id",
      "relevance": <int>,
      "novelty": <int>,
      "paper_type": "empirical | review | intervention | theoretical | methods",
      "matched_entries": ["命中的画像 topics/core_questions 条目原文，按贡献排序"],
      "matched_anchor": "命中的 calibration_anchors 标题，无则 null",
      "negative_hit": false,
      "reason": "中文 1-2 句。达标：推荐理由，必须引用 matched_entries 说明对口在哪；未达标：拦截原因",
      "overview_seed": "若达标：研究问题一句话 + 方法一句话 + 最值得借鉴的一点；未达标为空字符串"
    }}
  ]
}}

# 铁律
1. 只输出 JSON；papers 中每篇都必须有对应 scores 条目。
2. matched_entries 必须逐字来自 profile_digest 原文，不允许概括改写。
3. 摘要不足以判断时相关度降 1-2 分，并在 reason 注明「基于摘要判断」。
4. 不假设任何具体学科，所有判断只依据领域上下文与 profile_digest。"""


def score_papers(settings, profile, digest, papers):
    """依据兴趣画像打 相关度+新颖度（用户锚点+通用锚点两层），批量每 5 篇"""
    th = settings["taste"]
    pool_name = {"new": "recent", "classic": "classic", "author": "tracked_author"}
    domain_context = domain_context_of(profile)
    results = {}
    for i in range(0, len(papers), 5):
        batch = papers[i : i + 5]
        items = [
            {"id": p["id"], "title": p["title"], "venue": p["venue"], "year": p["year"],
             "citations": p["citations"], "pool": pool_name.get(p["pool"], p["pool"]),
             "abstract": p["abstract"][:800] or "(无摘要)"}
            for p in batch
        ]
        user_content = f"""【profile_digest】
{digest}

【thresholds】相关度≥{th['relevance_min']}，新颖度≥{th['novelty_min']}

【papers】
{json.dumps(items, ensure_ascii=False, indent=1)}"""
        print(f"Kimi 打分中（{i + 1}-{i + len(batch)}/{len(papers)}）…")
        got = {}
        for attempt in range(3):
            text = llm_chat(settings, [
                {"role": "system", "content": SCORING_PROMPT.format(domain_context=domain_context)},
                {"role": "user", "content": user_content},
            ])
            arr = parse_json_object(text).get("scores") or parse_json_array(text)
            for r in arr:
                if isinstance(r, dict) and r.get("id"):
                    got[str(r["id"])] = r
            missing = [p["id"] for p in batch if p["id"] not in got]
            if not missing:
                break
            print(f"  本批 {len(missing)} 篇未拿到分数，第 {attempt + 1} 次重试…")
            time.sleep(2)
        if missing:
            print(f"  警告：{len(missing)} 篇三次均未拿到分数，按未通过处理")
        results.update(got)
        time.sleep(1)
    return results


def make_overview(settings, digest, paper):
    """为入选论文生成概览页内容；overview_seed 来自打分环节"""
    seed = paper.get("overview_seed") or ""
    prompt = f"""你是学术文献整理助手。请阅读下面这篇论文的信息，输出一个 JSON 对象（不要输出其他内容）：

{{
  "title_zh": "标题的中文翻译（学术、通顺）",
  "summary_zh": "论文摘要的中文概述，3-5句",
  "research_question": "研究问题：分点列出，格式如 1. ... 2. ...",
  "method": "研究方法：研究设计、抽样与样本、资料收集与分析方法，2-4句",
  "conclusion": "结论：分点列出核心发现，格式如 1. ... 2. ...",
  "insight": "创新点/可借鉴点/启发：结合研究者的兴趣（见下），2-4句"
}}

【研究者兴趣画像】
{digest}

【筛选环节的前期判断（供参考）】
{seed or '(无)'}

【论文】
标题: {paper['title']}
来源: {paper['venue']} ({paper['year']})
摘要: {paper['abstract'] or '(无摘要，请依据标题谨慎推断，并在各字段注明"据标题推断")'}"""
    return parse_json_object(llm_chat(settings, [{"role": "user", "content": prompt}], max_tokens=4096))


# ---------- 筛选 ----------

def select_papers(settings, profile, digest, pools, pushed, rejected):
    taste = settings["taste"]
    th_rel, th_nov = taste["relevance_min"], taste["novelty_min"]
    author_rel_min = (settings.get("author_pool") or {}).get("relevance_min", 5)
    count = settings["push"]["count"]
    mix = settings["mix"]
    strictness = taste.get("strictness", "strict")
    type_ok = set(taste.get("paper_types") or PAPER_TYPES)
    skip = pushed | rejected

    # 经典候选：按关键词轮换取未评过的论文，小领域关键词每天都有代表
    by_kw = {}
    for p in pools["classic"]:
        if p["id"] not in skip and p["citations"] >= settings["sources"]["classic_min_citations"]:
            by_kw.setdefault(p.get("kw", ""), []).append(p)
    per_kw = settings["sources"].get("classic_candidates_per_keyword", 4)
    classics = []
    for kw in profile.get("query_templates") or []:
        classics.extend(by_kw.get(kw, [])[:per_kw])
    authors, new = [], []
    seen_author_ids = set()
    for p in pools["author"]:
        if p["id"] not in skip and p["id"] not in seen_author_ids:
            authors.append(p)
            seen_author_ids.add(p["id"])
    for p in pools["new"]:
        if p["id"] not in skip and p["id"] not in seen_author_ids:
            new.append(p)

    candidates = classics + authors + new
    if not candidates:
        return [], []
    scores = score_papers(settings, profile, digest, candidates)
    for p in candidates:
        s = scores.get(p["id"], {})
        p.update({"relevance": s.get("relevance"), "novelty": s.get("novelty"),
                  "paper_type": (s.get("paper_type") or "").strip(),
                  "matched_entries": s.get("matched_entries") or [],
                  "matched_anchor": s.get("matched_anchor"),
                  "negative_hit": bool(s.get("negative_hit")),
                  "reason": s.get("reason", ""), "tldr": s.get("overview_seed", ""),
                  "overview_seed": s.get("overview_seed", "")})

    # 内容偏好过滤：paper_type 不在偏好列表的跳过（留痕 rejected）
    type_filtered_ids = set()
    typed = []
    for p in candidates:
        if p["paper_type"] and p["paper_type"] not in type_ok:
            type_filtered_ids.add(p["id"])
            print(f"  ✗ [{p['pool']}] 类型过滤（{p['paper_type']} 不在内容偏好内）| {p['title'][:50]}")
        else:
            typed.append(p)
    classics_t = [p for p in typed if p["pool"] == "classic"]
    authors_t = [p for p in typed if p["pool"] == "author"]
    new_t = [p for p in typed if p["pool"] == "new"]

    pass_th = lambda p: (p["relevance"] or 0) >= th_rel and (p["novelty"] or 0) >= th_nov
    by_score = lambda lst: sorted(lst, key=lambda p: (p["relevance"] or 0) + (p["novelty"] or 0), reverse=True)
    ok_classics = by_score([p for p in classics_t if pass_th(p)])
    ok_new = by_score([p for p in new_t if pass_th(p)])
    ok_authors = [p for p in authors_t if (p["relevance"] or 0) >= author_rel_min]

    # 凑满 count：追踪作者过线者额外直推（不占配额）→ 经典配额 → 新近配额 →
    # 剩余过线经典/新近补位 → 视野宽度允许时用邻近分数经典兜底
    picked, chosen = [], set()

    def add(lst, limit=None, cap=True):
        n = 0
        for p in lst:
            if cap and len(picked) >= count:
                return
            if limit is not None and n >= limit:
                return
            if p["id"] in chosen:
                continue
            chosen.add(p["id"])
            picked.append(p)
            n += 1

    if mix.get("tracked_author_extra", True):
        add(ok_authors, cap=False)
    else:
        add(ok_authors)
    add(ok_classics, mix.get("classic", 1))
    add(ok_new, mix.get("recent", 2))
    add(ok_classics)
    add(ok_new)
    if len(picked) < count:
        bar = {"balanced": 6, "wide": 5}.get(strictness)
        if bar:
            rest = by_score([p for p in classics_t
                             if p["id"] not in chosen and (p["relevance"] or 0) >= bar])
            if rest:
                print(f"  （视野宽度={strictness}：启用相关度≥{bar} 的经典补位）")
            add(rest)

    marks = {p["id"]: "✓" for p in picked}
    for p in candidates:
        if p["id"] in type_filtered_ids:
            continue
        anchor = " ⚓" if p.get("matched_anchor") else ""
        print(f"  {marks.get(p['id'], '✗')} [{p['pool']}] 相关{p['relevance']} 新颖{p['novelty']} "
              f"{p['paper_type'] or '?'} 被引{p['citations']}{anchor} | {p['title'][:55]}")
    newly_rejected = [p["id"] for p in candidates if p["id"] not in marks]
    return picked, newly_rejected


# ---------- 概览页 ----------

PAGE_CSS = """
*{box-sizing:border-box}
body{max-width:680px;margin:0 auto;padding:56px 26px 40px;font-family:-apple-system,'PingFang SC','Helvetica Neue',sans-serif;color:#1d1d1f;line-height:1.9;background:#fff;-webkit-font-smoothing:antialiased}
.kicker{font-size:11.5px;letter-spacing:.14em;color:#86868b;margin-bottom:16px}
h1.zh{font-family:'Songti SC','STSong',serif;font-size:27px;line-height:1.45;margin:0 0 6px;font-weight:700}
.en{color:#86868b;font-size:13.5px;line-height:1.6}
.meta{color:#86868b;font-size:13px;margin-top:14px;padding-bottom:22px;border-bottom:1px solid #e8e8ed}
section{margin-top:30px;font-size:15.5px}
section h2{font-size:12px;letter-spacing:.22em;color:#0a5396;font-weight:600;margin:0 0 10px}
section h2 .num{color:#b9b9be;margin-right:10px;letter-spacing:0;font-weight:400}
.links{margin-top:36px;padding-top:22px;border-top:1px solid #e8e8ed}
.links a{display:inline-block;margin:0 10px 8px 0;color:#0a5396;text-decoration:none;border:1px solid #0a5396;border-radius:999px;padding:6px 18px;font-size:13.5px}
footer{margin-top:34px;padding-top:16px;border-top:1px solid #e8e8ed;color:#aeaeb2;font-size:12px}
footer a{color:#0a5396;text-decoration:none}
ul.archive{list-style:none;padding:0;margin:18px 0 0}
ul.archive li{padding:14px 0;border-bottom:1px solid #f0f0f2}
ul.archive a{color:#1d1d1f;text-decoration:none;font-size:15.5px}
ul.archive a:active{color:#0a5396}
@media (max-width:480px){body{padding:40px 20px 32px}h1.zh{font-size:23px}}
"""


def nl2br(text):
    return "<br>".join(escape(line) for line in str(text or "").splitlines())


def write_overview_page(settings, paper):
    ov = paper.get("overview") or {}
    pool_label = {"author": f"关注作者 · {paper.get('tracked_author','')}",
                  "classic": "经典精读", "new": "新近论文"}[paper["pool"]]
    kicker = (f"{pool_label} ｜ 被引 {paper['citations']} ｜ 相关度 {paper['relevance']} · "
              f"新颖度 {paper['novelty']}" +
              (f" · {paper['paper_type']}" if paper.get("paper_type") else ""))
    html = f"""<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(ov.get('title_zh') or paper['title'])}</title><style>{PAGE_CSS}</style></head><body>
<div class="kicker">{escape(kicker)}</div>
<h1 class="zh">{escape(ov.get('title_zh') or '')}</h1>
<div class="en">{escape(paper['title'])}</div>
<div class="meta">{escape(paper['authors'])}<br>{escape(paper['venue'])} · {paper['year'] or ''}</div>
<section><h2><span class="num">01</span>论文摘要</h2>{nl2br(ov.get('summary_zh'))}</section>
<section><h2><span class="num">02</span>研究问题</h2>{nl2br(ov.get('research_question'))}</section>
<section><h2><span class="num">03</span>研究方法</h2>{nl2br(ov.get('method'))}</section>
<section><h2><span class="num">04</span>结论</h2>{nl2br(ov.get('conclusion'))}</section>
<section><h2><span class="num">05</span>创新点 / 可借鉴点 / 启发</h2>{nl2br(ov.get('insight'))}</section>
<div class="links">
{f'<a href="{paper["pdf"]}">原文 PDF</a>' if paper["pdf"] else ''}
<a href="{paper['url']}">原文页面（DOI）</a>
</div>
<footer>论文推送 Agent · {date.today().isoformat()} · 画像 v{settings.get('profile_version', '?')}<br><a href="../index.html">全部推送存档</a>{' · ' + settings_links_html(settings) if settings_links_html(settings) else ''}</footer>
</body></html>"""
    path = DOCS_DIR / "papers" / f"{paper['id']}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def rebuild_index(settings=None):
    pages = sorted((DOCS_DIR / "papers").glob("*.html"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for pg in pages:
        text = pg.read_text(encoding="utf-8")
        t0, t1 = text.find("<title>") + 7, text.find("</title>")
        title = text[t0:t1] if t0 > 6 and t1 > t0 else pg.stem
        items.append(f'<li><a href="papers/{pg.name}">{title}</a></li>')
    links = settings_links_html(settings) if settings else ""
    footer = f"<footer>{links}</footer>" if links else ""
    html = f"""<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>论文推送存档</title><style>{PAGE_CSS}</style></head><body>
<div class="kicker">DAILY PAPER DIGEST</div>
<h1 class="zh">论文推送存档</h1>
<div class="meta">按推送时间倒序 · 共 {len(items)} 篇</div>
<ul class="archive">{''.join(items)}</ul>
{footer}
</body></html>"""
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def overview_link(settings, paper):
    base = (settings.get("pages_base") or "").rstrip("/")
    if base:
        return f"{base}/papers/{paper['id']}.html"
    return f"../docs/papers/{paper['id']}.html"


def repo_base(settings):
    """GitHub 仓库地址：repo_url 显式配置优先，否则从 pages_base 推导"""
    url = (settings.get("repo_url") or "").rstrip("/")
    if url:
        return url
    base = (settings.get("pages_base") or "").rstrip("/")
    m = re.match(r"https://([A-Za-z0-9-]+)\.github\.io/([^/]+)", base)
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else ""


def _config_urls(settings):
    """本账号配置文件在 GitHub 上的编辑/反馈链接（repo 未配置时返回空）"""
    repo = repo_base(settings)
    if not repo:
        return None
    if ws.is_account_mode():
        rel = ws.base_dir().relative_to(ws.ROOT).as_posix() + "/config"
    else:
        rel = "config"
    return {
        "settings": f"{repo}/edit/main/{rel}/settings.json",
        "profile": f"{repo}/edit/main/{rel}/interest_profile.json",
        "issue": f"{repo}/issues/new?" + quote("title=推送反馈：", safe="=&"),
    }


def settings_links_html(settings):
    """网页页脚用：文字链接组"""
    urls = _config_urls(settings)
    if not urls:
        return ""
    return (f'<a href="{urls["settings"]}">⚙️ 推送设置</a> · '
            f'<a href="{urls["profile"]}">🧠 兴趣画像</a> · '
            f'<a href="{urls["issue"]}">✉️ 反馈提需求</a>')


def email_buttons_html(settings):
    """邮件底部用：醒目的按钮行（手机/电脑通用）"""
    urls = _config_urls(settings)
    if not urls:
        return ""
    btn = ("display:inline-block;margin:4px 6px;padding:8px 18px;border:1px solid #1a5fb4;"
           "border-radius:999px;color:#1a5fb4;text-decoration:none;font-size:13px;background:#fff;")
    return f"""<div style="text-align:center;margin:26px 0 10px;">
  <div style="font-size:12px;color:#999;margin-bottom:8px;">— 点这里，随时调整你的推送 —</div>
  <a href="{urls['settings']}" style="{btn}">⚙️ 推送设置</a>
  <a href="{urls['profile']}" style="{btn}">🧠 兴趣画像</a>
  <a href="{urls['issue']}" style="{btn}">✉️ 反馈提需求</a>
</div>"""


# ---------- 邮件 ----------

def render_email(picked, settings):
    today = date.today().isoformat()
    if not picked:
        body = '<p style="color:#666;">今日没有通过筛选的论文。Agent 运行正常。</p>'
    else:
        cards = []
        tag_map = {"author": "👤 关注作者", "classic": "📚 经典精读", "new": "🆕 新近论文"}
        for p in picked:
            ov = p.get("overview") or {}
            title_zh = escape(ov.get("title_zh") or "")
            tag = tag_map[p["pool"]] + (f" · {p['tracked_author']}" if p["pool"] == "author" else "")
            pdf_link = f' · <a href="{p["pdf"]}" style="color:#1a5fb4;text-decoration:none;">PDF</a>' if p["pdf"] else ""
            cards.append(f"""
<div style="background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:18px 20px;margin:14px 0;">
  <div style="font-size:12px;color:#999;">{escape(tag)} · 相关 {p['relevance']} · 新颖 {p['novelty']} · 被引 {p['citations']}</div>
  <div style="font-size:16px;font-weight:600;margin:8px 0 2px;line-height:1.5;">
    <a href="{overview_link(settings, p)}" style="color:#222;text-decoration:none;">{title_zh}</a></div>
  <div style="font-size:12.5px;color:#888;line-height:1.4;">{escape(p['title'])}</div>
  <div style="font-size:13px;color:#666;margin-top:6px;">{escape(p['authors'])} · {escape(p['venue'])} {p['year'] or ''}</div>
  <div style="font-size:14px;color:#333;margin-top:10px;line-height:1.7;">{escape(p['tldr'])}</div>
  <div style="font-size:13px;color:#1a5fb4;margin-top:6px;">💡 {escape(p['reason'])}</div>
  <div style="margin-top:12px;font-size:14px;">
    <a href="{overview_link(settings, p)}" style="color:#fff;background:#1a5fb4;border-radius:6px;padding:5px 14px;text-decoration:none;">📖 概览</a>
    <span style="font-size:13px;color:#999;margin-left:10px;"><a href="{p['url']}" style="color:#1a5fb4;text-decoration:none;">原文</a>{pdf_link}</span>
  </div>
</div>""")
        body = "".join(cards)
    return f"""<html><body style="max-width:680px;margin:0 auto;padding:20px;background:#f7f7f7;font-family:-apple-system,'PingFang SC',sans-serif;">
<div style="padding:6px 4px 14px;">
  <span style="font-size:19px;font-weight:700;">📖 每日论文推送</span>
  <span style="color:#999;font-size:13px;margin-left:10px;">{today} · 今日 {len(picked)} 篇</span>
</div>
{body}
{email_buttons_html(settings)}
<p style="color:#bbb;font-size:12px;text-align:center;margin-top:10px;">画像 v{settings.get('profile_version', '?')} · 由你的论文推送 Agent 生成</p>
</body></html>"""


def send_email(settings, subject, html, receiver=""):
    sender = os.environ.get("EMAIL_SENDER", "")
    password = os.environ.get("EMAIL_PASSWORD", "")
    receiver = receiver or os.environ.get("EMAIL_RECEIVER", "")
    if not all([sender, password, receiver]):
        sys.exit("缺少环境变量 EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECEIVER（多账号模式收件人取账号邮箱）")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("论文推送 Agent", sender))
    msg["To"] = receiver
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings["email"]["smtp_server"], settings["email"]["smtp_port"],
                          context=ctx, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, [receiver], msg.as_string())
    print(f"邮件已发送至 {receiver}")


# ---------- 主流程 ----------

def run_pipeline(dry_run, receiver=""):
    """单账号完整流水线（工作区已由调用方切好）。返回 (入选数, 是否已发信)"""
    settings, profile = load_config()
    digest = profile_digest(profile)
    pushed = load_ledger()
    rejected = load_rejected()
    api_key = os.environ.get("S2_KEY", "")
    provider = get_provider(settings)

    print("== 拉取论文 ==")
    cache_path = ws.cache_path(date.today().isoformat())
    pools = None
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("_meta", {}).get("query_templates") == (profile.get("query_templates") or []):
            pools = cached
            for k in ("new", "classic", "author"):
                pools.setdefault(k, [])
            print("（使用当日候选缓存）")
        else:
            print("（画像检索串已变更，当日缓存作废，重新拉取）")
    if pools is None:
        pools = {
            "new": provider.fetch_new_pool(settings, profile, api_key),
            "classic": provider.fetch_classic_pool(settings, profile, api_key),
            "author": provider.fetch_author_pool(settings, api_key),
        }
        pools["new"] = apply_hindex_filter(settings, api_key, pools["new"])
        pools["_meta"] = {"query_templates": profile.get("query_templates") or []}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(pools, ensure_ascii=False), encoding="utf-8")
    print(f"候选：新近 {len(pools['new'])} · 经典 {len(pools['classic'])} · 追踪作者 {len(pools['author'])}")

    print("== 补全缺失摘要（OpenAlex）==")
    enrich_metadata(pools["new"] + pools["classic"] + pools["author"])

    print("== 筛选打分 ==")
    picked, newly_rejected = select_papers(settings, profile, digest, pools, pushed, rejected)
    print(f"通过筛选 {len(picked)} 篇")

    print("== 生成概览页 ==")
    for p in picked:
        print(f"  {p['title'][:55]}")
        p["overview"] = make_overview(settings, digest, p)
        write_overview_page(settings, p)
        time.sleep(1)
    if picked:
        rebuild_index(settings)

    html = render_email(picked, settings)
    subject = f"📖 每日论文推送 {date.today().isoformat()}（{len(picked)} 篇）"
    if dry_run:
        out = ws.preview_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"[dry-run] 邮件预览已写入 {out}")
        return len(picked), False

    sent = False
    if picked or settings.get("send_empty", True):
        send_email(settings, subject, html, receiver=receiver)
        sent = True
    pushed.update(p["id"] for p in picked)
    save_ledger(pushed)
    rejected.update(newly_rejected)
    save_rejected(rejected)
    print("完成。")
    return len(picked), sent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="不发邮件、不记账，预览写入 data/preview.html")
    args = parser.parse_args()

    import accounts
    if accounts.has_users_dir():
        # 多账号模式：逐账号跑完整流水线，单账号故障不拖垮整轮
        accts = accounts.active_accounts()
        if not accts:
            print("users/ 下没有启用中的账号，请先运行 python setup.py 创建账号（或运行 python setup.py --migrate 迁移现有实例）。")
            sys.exit(1)
        print(f"多账号模式：共 {len(accts)} 个账号")
        summary = []
        for idx, acct in enumerate(accts, 1):
            print(f"\n========== 账号 {idx}/{len(accts)}：{acct['email']} ==========")
            ws.use_account(ws.USERS_DIR / acct["dir"])
            try:
                n, sent = run_pipeline(args.dry_run, receiver=acct["email"])
                summary.append((acct["email"], n, sent, ""))
            except Exception as e:  # 单账号故障隔离
                traceback.print_exc()
                summary.append((acct["email"], 0, False, str(e)[:80]))
            if not args.dry_run and idx < len(accts):
                time.sleep(random.uniform(5, 10))  # 发信间隔，防触发反垃圾
        print("\n===== 本轮汇总 =====")
        for email, n, sent, err in summary:
            mark = f"推送 {n} 篇" + ("（已发信）" if sent else "") if not err else f"失败：{err}"
            print(f"  {email}: {mark}")
    else:
        # 单用户模式（兼容既有实例）
        ws.use_root()
        run_pipeline(args.dry_run)


if __name__ == "__main__":
    main()
