#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""论文摄入：把一篇论文转成「兴趣声明」喂给画像编译器，并提作者追踪候选（通用版，不限学科）。
实现「研究兴趣信号提取器」spec（Prompt B）：领域上下文运行时注入、anchor_note 供代码组装校准锚点。
用法：python ingest.py            （交互输入 DOI / S2 ID / 粘贴摘要文本）
也可由 configure.py 主菜单「导入论文」或 setup.py 向导调 run_ingest()。
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402  复用 llm_chat / s2_tolerant / load_dotenv
import profile_compiler as pc  # noqa: E402

EXTRACTOR_PROMPT = """你是「研究兴趣信号提取器」。用户给了一篇论文的元数据与正文（或摘要），你要回答的问题不是「这篇论文讲了什么」，而是「用户把这篇论文给我，说明他想看什么」。你的输出会被另一个模块当作兴趣画像的更新输入。

# 领域上下文（运行时注入）
{domain_context}
仅用于理解语义，不复述进输出。

# 输出（严格 JSON，此外不输出任何文字）
{{
  "one_line_summary": "一句话中文概括这篇论文",
  "core_question": "这篇论文在回答的研究问题，中文表述",
  "statement_text": "以用户第一人称写的兴趣声明，供画像编译器消费。写法：「我在关注 X 方向的 Y 问题，特别是 Z 角度」，2-4 句，聚焦研究问题与视角，不罗列论文细节",
  "topics_hint": [{{"name": "英文主题词", "weight_hint": 0.9}}],
  "anchor_note": "一句话说明这篇论文为何可作为「非常对口」的校准样例，例如「直接命中核心问题：……」",
  "cited_author_candidates": [
    {{"name": "作者姓名", "cited_count": 在参考文献中出现的次数, "representative_work": "被引代表作标题", "note": "为何值得追踪（一句话）"}}
  ]
}}

# 规则
1. statement_text 必须是第一人称兴趣声明，不是论文摘要。判断标准：换一个没读过这篇论文的人读这段话，能明白用户想看什么方向的论文。
2. topics_hint 提取 2-5 个，weight_hint 默认 0.9（用户亲手给的论文是强信号）；方法论类主题（某量表、某统计方法、某框架名）除非显然是论文核心贡献，否则不提取。
3. anchor_note 只写「命中了什么问题」，不评价论文质量。
4. cited_author_candidates：统计参考文献中反复出现的作者（≥2 次），最多 5 个；只报名字和线索，h-index 等由代码去 Semantic Scholar 解析，不编造任何数字。
5. 论文中用户可能「不想看」的方向不输出——给论文是纯正向信号，负向信号只来自用户明确表达。
6. 若 paper_text 只有摘要，在 statement_text 末尾标注「（基于摘要判断，置信度有限）」。
7. 不假设任何具体学科。"""


def fetch_paper_bundle(api_key, ref):
    """按 DOI 或 S2 paperId 拉 标题/摘要/参考文献（前 100 条）"""
    paper_id = f"DOI:{ref}" if ref.lower().startswith("10.") else ref
    data = m.s2_tolerant("GET", f"{m.S2}/paper/{paper_id}", api_key, params={
        "fields": "title,abstract,authors,venue,year,externalIds,references.title,references.authors",
    })
    if not data or not data.get("title"):
        return None
    refs = (data.get("references") or [])[:100]
    refs_text = "\n".join(
        f"- {r.get('title') or '?'} ({', '.join(a.get('name', '') for a in (r.get('authors') or [])[:3])})"
        for r in refs
    )
    meta = {
        "title": data["title"],
        "authors": ", ".join(a.get("name", "") for a in (data.get("authors") or [])[:6]),
        "year": data.get("year"),
        "venue": data.get("venue") or "",
        "doi": (data.get("externalIds") or {}).get("DOI", ""),
        "s2_id": data.get("paperId", ""),
    }
    return meta, (data.get("abstract") or ""), refs_text


def extract_signals(settings, meta, paper_text, references_text, domain_context=""):
    payload = {
        "paper_meta": meta,
        "paper_text": paper_text[:8000] if paper_text else "(缺失)",
        "references_text": references_text[:6000] if references_text else "(缺失)",
    }
    text = m.llm_chat(settings, [
        {"role": "system", "content": EXTRACTOR_PROMPT.format(domain_context=domain_context or "（暂无）")},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], max_tokens=4096)
    return m.parse_json_object(text)


def add_calibration_anchor(meta, anchor_note, source):
    """代码侧组装校准锚点：score=9，写入画像 calibration_anchors（去重按 s2id）"""
    if not meta.get("s2_id") and not meta.get("title"):
        return
    profile = pc.load_profile()
    if not profile:
        return
    anchors = profile.setdefault("calibration_anchors", [])
    s2id = meta.get("s2_id") or ""
    if s2id and any(a.get("s2id") == s2id for a in anchors):
        return
    anchors.append({
        "paper_title": meta.get("title", ""),
        "s2id": s2id,
        "score": 9,
        "note": anchor_note or "用户提供的对口论文",
        "source": source,
    })
    pc.save_profile(profile)
    print(f"  ⚓ 已加入校准锚点：《{meta.get('title', '')[:50]}》（score 9 / {source}）")


def run_ingest(settings, api_key, add_author_fn, ref=None, anchor_source="feedback",
               ask_authors=True):
    """摄入一篇论文。ref 为空时交互输入；anchor_source=seed（向导种子论文）或 feedback（日常反馈）；
    ask_authors=False 时不在此问作者候选（由调用方汇总统一问，如 setup 向导）"""
    if ref is None:
        ref = input("输入论文 DOI / S2 paperId，或直接粘贴摘要文本：").strip()
    if not ref:
        return None
    if ref.lower().startswith("10.") or (ref.isalnum() and len(ref) >= 20):
        print("拉取论文信息…")
        bundle = fetch_paper_bundle(api_key, ref)
        if not bundle:
            print("没找到这篇论文，检查 DOI/ID 或改为粘贴摘要")
            return None
        meta, paper_text, refs_text = bundle
        print(f"  《{meta['title']}》({meta['venue']} {meta['year'] or ''})")
    else:
        meta, paper_text, refs_text = {"title": "(用户粘贴文本)", "doi": "", "s2_id": ""}, ref, ""

    domain_context = pc.domain_context_of(pc.load_profile())
    print("提取兴趣信号…")
    signals = extract_signals(settings, meta, paper_text, refs_text, domain_context)
    if not signals:
        print("信号提取失败（LLM 输出异常），请重试")
        return None
    print(f"\n一句话：{signals.get('one_line_summary', '')}")
    print(f"兴趣声明：{signals.get('statement_text', '')}\n")

    statement = signals.get("statement_text") or signals.get("one_line_summary") or ""
    if not statement:
        print("没有可用的兴趣声明，未改动画像")
        return None
    source_tag = f"paper:{meta.get('doi') or meta.get('s2_id') or 'manual'}"
    new_profile = pc.apply_update_interactive(
        lambda msgs, **kw: m.llm_chat(settings, msgs, **kw),
        statement, mode="increment", source_tag=source_tag)

    if new_profile is None:
        return None
    add_calibration_anchor(meta, signals.get("anchor_note", ""), anchor_source)
    if ask_authors:
        for cand in (signals.get("cited_author_candidates") or [])[:5]:
            ans = input(f"\n作者候选：{cand.get('name')}（被引 {cand.get('cited_count')} 次，{cand.get('note', '')}）——要追踪吗？[y/N]：").strip().lower()
            if ans == "y":
                add_author_fn(api_key, preset_name=cand.get("name", ""))
                time.sleep(1)
    return signals


def main():
    m.load_dotenv()
    settings = m.load_settings()
    api_key = os.environ.get("S2_KEY", "")

    def _lazy_add_author(key, preset_name=""):
        import configure
        return configure.search_authors(key, preset_name=preset_name)

    run_ingest(settings, api_key, _lazy_add_author)


if __name__ == "__main__":
    main()
