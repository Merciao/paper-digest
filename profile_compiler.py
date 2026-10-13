#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研究兴趣画像编译器：自由文本/论文声明 → 结构化画像更新（通用版，不限学科）。
实现「文献推送兴趣画像管理器」spec（Prompt A）：领域上下文运行时注入、
calibration_anchors 透传（由代码在向导/反馈流程维护）、严格 JSON 输出 → 预览 → 确认才写回。
configure.py 与 ingest.py 共用本模块；路径经 workspace 抽象（单/多账号）。
"""
import json
from datetime import date

import workspace as ws

COMPILER_PROMPT = """你是「文献推送兴趣画像管理器」。你的唯一职责：根据用户的新输入，维护一份结构化的研究兴趣画像（interest_profile.json），输出变更预览供用户确认。你不推荐论文、不打分、不闲聊。

# 领域上下文（运行时注入）
{domain_context}
你用它理解语义、生成检索词；不要把它复述进输出。

# 画像结构（严格遵循）
{{
  "version": <int>,
  "core_questions": ["用户关心的研究问题，中文表述"],
  "topics": [{{"name": "英文主题词", "weight": 0.0-1.0, "source": "来源标记", "updated_at": "YYYY-MM-DD"}}],
  "negative": ["明确排除的方向，英文"],
  "tracked_authors_suggested": [],
  "query_templates": ["英文检索串"],
  "calibration_anchors": [{{"paper_title": "...", "s2id": "...", "score": 9, "note": "...", "source": "seed|feedback"}}],
  "raw_description": "本次输入原文"
}}

# 合并规则
## 增量模式（increment）
- 新增 topics：默认 weight 0.8；语气强烈（「重点」「只关心」「最近主攻」）给 0.9-1.0；来源为 paper 时默认 0.9。
- 已有 topics：再次被提到 → weight +0.1（封顶 1.0），刷新 updated_at；用户说「少推/不推 X」且 X 在 topics 中 → weight -0.3，低于 0.3 时从 topics 移除并加入 negative。
- 不要删除用户没提到的旧条目。
- negative：只收用户明确表达排除的（「不推」「别来」「排除」），不擅自扩大。

## 替换模式（replace）
- topics 与 core_questions 以本次输入为准整体重建。
- negative：保留旧条目，合并本次新增。
- preview_text 中必须提示：「整体替换后，旧画像下被筛掉的论文可能符合新方向，建议重置 rejected.json」。

## 通用
- version = current_profile.version + 1（冷启动为 1）。
- raw_description 记录本次输入原文，不做润色。
- **calibration_anchors 原样透传**：从 current_profile 复制到 new_profile，不增删不改（该字段由代码在向导和反馈流程中维护）。冷启动时输出空数组。
- query_templates 每次变更后基于最新 topics 重新生成：每条是可直接提交 Semantic Scholar 的英文检索串；每个主题 2-3 条同义变体（覆盖英美拼写差异、上位/下位词）；总数控制在 8-15 条，高权重主题的变体数量优先。若用户领域明显以中文本土文献为主，在 preview_text 末尾提示一句「Semantic Scholar 对中文期刊覆盖有限」。

# 输出（严格 JSON，此外不输出任何文字）
{{
  "mode_used": "increment 或 replace",
  "detected_reset_intent": true 或 false,
  "diff": [
    {{"op": "add|update|remove|reweight", "path": "topics[xxx]", "before": null 或原值, "after": 新值, "reason": "一句话中文说明"}}
  ],
  "new_profile": {{完整画像}},
  "preview_text": "给用户看的中文变更说明（markdown 列表）：新增/调整/移除的条目，权重变化写成 0.6→0.8；替换模式须含 rejected.json 提示",
  "clarifying_questions": ["输入有歧义时的问题，无则为空数组"]
}}

# 铁律
1. 只输出 JSON。
2. 不编造用户没表达的兴趣；拿不准的写入 clarifying_questions，diff 中不做对应改动。
3. 若输入含整体重置语义（「以后只推 X」「换个方向」「重新开始」）但 mode=increment：detected_reset_intent 置 true，preview_text 开头加「检测到你可能想整体替换，确认时请选 [r]」。
4. preview_text 简洁，给人看，不解释规则本身。
5. 不假设任何具体学科——所有判断只依据领域上下文与画像内容。"""


def domain_context_of(profile, fallback_text=""):
    """领域上下文：core_questions 拼接 + 权重前 5 topics；冷启动用用户原文（三处共用）"""
    if not profile:
        return fallback_text or "（用户尚未描述研究领域）"
    lines = ["用户研究领域："]
    qs = profile.get("core_questions") or []
    if qs:
        lines.append("核心研究问题：" + "；".join(qs))
    tops = sorted(profile.get("topics") or [], key=lambda x: -x.get("weight", 0))[:5]
    if tops:
        lines.append("重点主题：" + "、".join(t["name"] for t in tops))
    raw = profile.get("raw_description")
    if raw:
        lines.append(f"用户原始描述：{raw[:300]}")
    return "\n".join(lines)


def load_profile():
    p = ws.profile_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_profile(profile):
    p = ws.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def compile_update(llm_chat, current_profile, new_input, mode, source_tag, domain_context=None):
    """调 LLM 跑一次编译，返回解析后的结果 dict"""
    if domain_context is None:
        domain_context = domain_context_of(current_profile, fallback_text=new_input)
    payload = {
        "current_profile": current_profile,
        "new_input": new_input,
        "mode": mode,
        "source_tag": source_tag,
        "today": date.today().isoformat(),
    }
    text = llm_chat([
        {"role": "system", "content": COMPILER_PROMPT.format(domain_context=domain_context)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], max_tokens=8192)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"编译器输出不是合法 JSON：{text[:200]}")
    result = json.loads(text[start:end + 1])
    # calibration_anchors 由代码维护：LLM 若丢失则从当前画像补回
    anchors = (current_profile or {}).get("calibration_anchors") or []
    if anchors and not (result.get("new_profile") or {}).get("calibration_anchors"):
        result["new_profile"]["calibration_anchors"] = anchors
    return result


def apply_update_interactive(llm_chat, new_input, mode="increment", source_tag="user_text",
                             preview_only=False):
    """编译 → 预览 → 确认 → 写回。preview_only=True 时只展示不写盘（供向导草稿预演）。
    返回新画像（确认时）或 None（放弃时）。"""
    current = load_profile()
    draft = current
    while True:
        result = compile_update(llm_chat, draft, new_input, mode, source_tag)
        print("\n" + result.get("preview_text", "(无预览)"))
        for q in result.get("clarifying_questions") or []:
            print(f"  ❓ {q}")
        if preview_only:
            return result["new_profile"]
        hint = "\n[y]确认写入"
        if mode == "increment" and result.get("detected_reset_intent"):
            hint += "  [r]改用整体替换"
        hint += "  [n]放弃："
        op = input(hint).strip().lower()
        if op == "r" and mode == "increment":
            mode = "replace"
            draft = current
            continue
        if op != "y":
            print("已放弃，画像未改动。")
            return None
        new_profile = result["new_profile"]
        save_profile(new_profile)
        print(f"✅ 画像已更新到 v{new_profile['version']}")
        if result.get("mode_used") == "replace":
            ans = input("整体替换后，旧画像下被筛掉的论文可能符合新方向。要重置 rejected.json 吗？[y/N]：").strip().lower()
            if ans == "y":
                ws.rejected_path().write_text("[]", encoding="utf-8")
                print("已重置 rejected.json")
        return new_profile
