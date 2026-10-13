#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交互式配置入口：python configure.py
主菜单：修改兴趣方向（画像编译器）/ 导入论文（ingest）/ 推送与筛选设置 /
追踪作者管理 / 查看当前画像 / 试跑。配置统一读写 config/settings.json。
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402  复用 llm_chat / load_settings / load_dotenv / S2
import profile_compiler as pc  # noqa: E402
import workspace as ws  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "daily.yml"
CRON_TAG = "# paper-digest-agent"
PAPER_TYPES = ["empirical", "review", "intervention", "theoretical", "methods"]
TYPE_ZH = {"empirical": "实证研究", "review": "综述", "intervention": "干预方案",
           "theoretical": "理论", "methods": "方法学"}


def pick_account():
    """多账号模式：选择要操作的账号并切换工作区；单用户模式直接返回"""
    import accounts
    if not accounts.has_users_dir():
        ws.use_root()
        return "(单用户)"
    accts = accounts.load_registry()
    if not accts:
        print("users/ 下还没有账号，请先运行 python setup.py 创建（或 python setup.py --migrate 迁移现有实例）。")
        sys.exit(1)
    if len(accts) == 1:
        a = accts[0]
    else:
        print("\n选择要配置的账号：")
        for i, a in enumerate(accts, 1):
            print(f"  {i}. {a['email']}{'' if a.get('active', True) else '（已停用）'}")
        v = input("编号：").strip()
        if not v.isdigit() or not (1 <= int(v) <= len(accts)):
            sys.exit("无效选择")
        a = accts[int(v) - 1]
    ws.use_account(ws.USERS_DIR / a["dir"])
    return a["email"]


def load_settings():
    return json.loads(ws.settings_path().read_text(encoding="utf-8"))


def save_settings(s):
    profile = pc.load_profile() or {}
    if profile.get("version"):
        s["profile_version"] = profile["version"]
    ws.settings_path().write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("✅ 已保存")


def llm(settings, msgs, **kw):
    return m.llm_chat(settings, msgs, **kw)


# ---------- 追踪作者 ----------

def search_authors(api_key, preset_name=""):
    name = preset_name or input("输入学者姓名（英文拼写更准，如 Mark Weist）：").strip()
    if not name:
        return
    resp = requests.get(f"{m.S2}/author/search",
                        params={"query": name, "fields": "name,affiliations,paperCount,citationCount,hIndex", "limit": 5},
                        headers={"x-api-key": api_key} if api_key else {}, timeout=30)
    if resp.status_code != 200:
        print(f"搜索失败：{resp.status_code} {resp.text[:200]}")
        return
    results = resp.json().get("data", [])
    if not results:
        print("没有找到匹配的学者")
        return
    print("\n候选学者：")
    for i, a in enumerate(results, 1):
        aff = (a.get("affiliations") or ["未知机构"])[0]
        print(f"  {i}. {a['name']} · {aff} · h-index {a.get('hIndex')} · 论文 {a.get('paperCount')} · 被引 {a.get('citationCount')}")
    choice = input("选择编号添加（回车取消）：").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(results)):
        return
    a = results[int(choice) - 1]
    s = load_settings()
    authors = s.setdefault("tracked_authors", [])
    if any(x["id"] == a["authorId"] for x in authors):
        print("该作者已在追踪列表中")
        return
    authors.append({"name": a["name"], "id": a["authorId"]})
    save_settings(s)
    print(f"已添加：{a['name']}（ID {a['authorId']}）——TA 的新论文将以相关度≥{s.get('author_pool', {}).get('relevance_min', 5)} 的门槛直推")


def manage_authors(api_key):
    while True:
        s = load_settings()
        authors = s.get("tracked_authors") or []
        print("\n追踪作者列表：")
        if authors:
            for i, a in enumerate(authors, 1):
                print(f"  {i}. {a['name']} (ID {a['id']})")
        else:
            print("  （空）")
        op = input("[a]添加  [d]删除  [回车]返回：").strip().lower()
        if op == "a":
            search_authors(api_key)
            time.sleep(1)
        elif op == "d" and authors:
            idx = input("删除编号：").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(authors):
                removed = authors.pop(int(idx) - 1)
                save_settings(s)
                print(f"已删除 {removed['name']}")
        else:
            return


# ---------- 修改兴趣方向（画像编译器） ----------

def edit_interests(settings):
    print("\n用自然语言描述你的兴趣调整，例如：")
    print("  「最近想多看看出院计划和家庭参与这块，成人相关的别推了」")
    print("  「少推纯精神医学的临床研究」")
    mode = input("模式：[回车]=增量合并  [r]=整体替换：").strip().lower()
    mode = "replace" if mode == "r" else "increment"
    print("请输入（可多行，空行结束）：")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        print("未输入，取消")
        return
    new_profile = pc.apply_update_interactive(lambda msgs, **kw: llm(settings, msgs, **kw),
                                              text, mode=mode, source_tag="user_text")
    if new_profile is not None:
        save_settings(load_settings())  # 同步 profile_version 到 settings.json
        s = load_settings()
        if s["taste"].get("strictness") == "wide":
            print("⚠️ 当前视野宽度为「拓宽」，画像已更新，建议回设置菜单把视野宽度调回「平衡」或「严格」，避免兴趣漂移")


# ---------- 推送与筛选设置 ----------

def cron_line_for(time_str):
    hh, mm = time_str.split(":")
    return f"{int(mm)} {int(hh)} * * * cd {ROOT} && /usr/bin/env python3 main.py >> /tmp/paper_digest_cron.log 2>&1 {CRON_TAG}"


def update_crontab(time_str):
    line = cron_line_for(time_str)
    print(f"\n将写入 crontab 的行：\n  {line}")
    if input("确认写入？[y/N]：").strip().lower() != "y":
        print("未写入 crontab（设置已保存，仅定时未改）")
        return
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in (cur.stdout or "").splitlines() if CRON_TAG not in l]
    lines.append(line)
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
    print("✅ crontab 已更新")


def update_workflow_cron(time_str):
    hh, mm = int(time_str.split(":")[0]), int(time_str.split(":")[1])
    utc_h = (hh - 8) % 24  # Asia/Shanghai → UTC
    wf = WORKFLOW.read_text(encoding="utf-8")
    wf_new = re.sub(r'cron: "\d+ \d+ \* \* \*"', f'cron: "{mm} {utc_h} * * *"', wf)
    if wf_new != wf:
        WORKFLOW.write_text(wf_new, encoding="utf-8")
        print(f"✅ 已同步 .github/workflows/daily.yml 的 cron 为 UTC {utc_h}:{mm:02d}（需 git push 生效）")


def push_time_setting(s):
    cur = s["push"]["time"]
    v = input(f"推送时间（当前 {cur}，格式 HH:MM，北京时间）：").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", v):
        print("未改动")
        return
    s["push"]["time"] = v
    save_settings(s)
    update_crontab(v)
    if input("GitHub Actions 定时也要同步吗？[y/N]：").strip().lower() == "y":
        update_workflow_cron(v)


def settings_menu():
    while True:
        s = load_settings()
        types = s["taste"]["paper_types"]
        type_marks = " ".join(f"{'☑' if t in types else '☐'}{TYPE_ZH[t]}" for t in PAPER_TYPES)
        strict_zh = {"strict": "严格对口", "balanced": "平衡", "wide": "拓宽"}.get(s["taste"]["strictness"], "?")
        extra = "额外直推" if s["mix"]["tracked_author_extra"] else "占配额"
        print(f"""
⚙️ 推送与筛选设置
  1. 推送时间      {s['push']['time']}（改完可重写 crontab / 同步 Actions）
  2. 每日篇数      {s['push']['count']}
  3. 新旧配比      新近 {s['mix']['recent']} / 经典 {s['mix']['classic']}（追踪作者{extra}）
  4. 内容偏好      {type_marks}
  5. 视野宽度      [{strict_zh}]
  6. 分数线        相关度≥{s['taste']['relevance_min']} · 新颖度≥{s['taste']['novelty_min']}
  7. h-index 门槛  {s['filters']['h_index_min'] or '关'}
  8. Pages 地址    {s.get('pages_base') or '(未配置）'}
  0. 返回""")
        op = input("选择：").strip()
        if op == "1":
            push_time_setting(s)
        elif op == "2":
            v = input(f"每日篇数（当前 {s['push']['count']}）：").strip()
            if v.isdigit() and int(v) > 0:
                s["push"]["count"] = int(v)
                save_settings(s)
        elif op == "3":
            print("直接回车保持原值")
            r = input(f"新近论文篇数（当前 {s['mix']['recent']}）：").strip()
            c = input(f"经典精读篇数（当前 {s['mix']['classic']}）：").strip()
            if r.isdigit():
                s["mix"]["recent"] = int(r)
            if c.isdigit():
                s["mix"]["classic"] = int(c)
            e = input(f"追踪作者论文额外直推、不占配额？[y/n]（当前 {'y' if s['mix']['tracked_author_extra'] else 'n'}）：").strip().lower()
            if e in ("y", "n"):
                s["mix"]["tracked_author_extra"] = (e == "y")
            save_settings(s)
        elif op == "4":
            t = input(f"输入要切换的类型编号 {','.join(f'{i+1}={TYPE_ZH[x]}' for i, x in enumerate(PAPER_TYPES))}，逗号分隔：").strip()
            for part in t.split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(PAPER_TYPES):
                    name = PAPER_TYPES[int(part) - 1]
                    if name in types:
                        types.remove(name)
                    else:
                        types.append(name)
            if types:
                save_settings(s)
            else:
                print("至少要保留一种类型，未保存")
        elif op == "5":
            v = input("视野宽度 [1]严格对口 [2]平衡 [3]拓宽：").strip()
            mapping = {"1": "strict", "2": "balanced", "3": "wide"}
            if v in mapping:
                s["taste"]["strictness"] = mapping[v]
                save_settings(s)
                if mapping[v] == "wide":
                    print("⚠️ 已切到「拓宽」：邻近领域更低分也可补位。下次修改兴趣方向时会提示你调回，避免兴趣漂移")
        elif op == "6":
            print("直接回车保持原值")
            r = input(f"相关度分数线(0-10)（当前 {s['taste']['relevance_min']}）：").strip()
            n = input(f"新颖度分数线(0-10)（当前 {s['taste']['novelty_min']}）：").strip()
            if r.isdigit():
                s["taste"]["relevance_min"] = int(r)
            if n.isdigit():
                s["taste"]["novelty_min"] = int(n)
            save_settings(s)
        elif op == "7":
            v = input(f"新论文池作者 h-index 门槛（当前 {s['filters']['h_index_min']}，0=不过滤）：").strip()
            if v.isdigit():
                s["filters"]["h_index_min"] = int(v)
                save_settings(s)
        elif op == "8":
            v = input(f"Pages 地址（当前 {s.get('pages_base') or '未配置'}，形如 https://<用户名>.github.io/<仓库名>）：").strip()
            if v:
                s["pages_base"] = v.rstrip("/")
                save_settings(s)
        else:
            return


# ---------- 查看当前画像 ----------

def show_profile():
    p = pc.load_profile()
    if not p:
        print("画像不存在")
        return
    print(f"\n===== 兴趣画像 v{p['version']} =====")
    print("核心研究问题：")
    for i, q in enumerate(p.get("core_questions") or [], 1):
        print(f"  {i}. {q}")
    print("主题词（权重 / 更新时间）：")
    for t in sorted(p.get("topics") or [], key=lambda x: -x.get("weight", 0)):
        print(f"  - {t['name']}（{t.get('weight', 0.8)} / {t.get('updated_at', '')}）")
    print(f"排除方向：{', '.join(p.get('negative') or ['（无）'])}")
    print(f"检索词 {len(p.get('query_templates') or [])} 条：")
    for q in p.get("query_templates") or []:
        print(f"  · {q}")


# ---------- 主菜单 ----------

def show_summary(s):
    authors = s.get("tracked_authors") or []
    profile = pc.load_profile() or {}
    print(f"""
当前状态：画像 v{profile.get('version', '?')} · 每天 {s['push']['time']} 推 {s['push']['count']} 篇（新近 {s['mix']['recent']}/经典 {s['mix']['classic']}）
分数线 相关≥{s['taste']['relevance_min']} 新颖≥{s['taste']['novelty_min']} · 视野 {s['taste']['strictness']} · h-index {s['filters']['h_index_min'] or '关'} · 追踪作者 {len(authors)} 人""")


def main():
    m.load_dotenv()
    who = pick_account()
    api_key = os.environ.get("S2_KEY", "")
    print(f"当前配置账号：{who}")
    while True:
        show_summary(load_settings())
        print("""
[1] 修改兴趣方向（自然语言 → 画像变更预览）
[2] 导入论文（DOI/S2 ID/摘要 → 提取兴趣信号 + 作者候选）
[3] 推送与筛选设置
[4] 追踪作者管理
[5] 查看当前画像
[6] 本地试跑预览（--dry-run）
[q] 退出""")
        op = input("选择：").strip().lower()
        if op == "1":
            edit_interests(load_settings())
        elif op == "2":
            import ingest
            ingest.run_ingest(load_settings(), api_key, search_authors)
        elif op == "3":
            settings_menu()
        elif op == "4":
            manage_authors(api_key)
        elif op == "5":
            show_profile()
        elif op == "6":
            subprocess.run([sys.executable, str(ROOT / "main.py"), "--dry-run"])
        elif op == "q":
            return


if __name__ == "__main__":
    main()
