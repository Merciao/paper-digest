# 论文每日推送 Agent · 详细设计文档

> 版本：2026-10-13 · 状态：本地功能全部建成并实测通过，待 GitHub 上线
> 项目目录：`~/paper-digest-agent/`

## 一、需求与定位

用户是社会学/社会工作方向研究生，研究领域：**精神科住院儿童青少年出院后的复学/返校过渡（school reentry / reintegration），以及社会工作在其中的干预路径**——一个小而高度跨学科的领域。

核心诉求（逐步演进确认）：
1. 每天定时推送**有价值**的论文到邮箱（163），**不一定是最新的，但一定是值得读的**
2. 兴趣方向用**自然语言**描述，可随时调整
3. 每次推送**三篇**
4. 标题附中文翻译；保留 gpt_paper_assistant 的两个特色：**追踪权威作者**（Semantic Scholar ID）与 **h-index 门槛过滤**
5. 每篇附可点入的**概览页**（字段结构对齐用户的文献整理 Excel《复学文献-国内国外.xlsx》），邮件卡片简洁美观
6. 有**交互式设置入口**；有**结构化兴趣画像系统**（编译器 + 论文摄入两个配套 prompt）

## 二、技术选型与理由

| 决策 | 选择 | 理由 |
|---|---|---|
| 主数据源 | Semantic Scholar Graph API（免费，可申请 key） | 覆盖 2 亿+ 论文含社会学期刊，带 citationCount / influentialCitationCount；**arXiv 对社会学覆盖极差，不适用** |
| 摘要补全 | OpenAlex API（免费无需 key） | S2 缺摘要时按 DOI 补拉真实摘要/期刊名/开放获取 PDF（T&F、Sage 等出版社论文 S2 常缺摘要） |
| LLM | Kimi（k3-256k，OpenAI 兼容端点 `https://api.kimi.com/coding/v1`） | 用户已有 Kimi Code 会员 key；注意：**该模型只允许 temperature=1**，支持 `reasoning_effort: low` 省额度 |
| 打分口径 | 移植 tatsu-lab/gpt_paper_assistant 的 相关度+新颖度 双分制 | 用户指定；其「不轻信作者自称创新」的新颖度定义 |
| 运行机制 | 借鉴 TideDra/zotero-arxiv-daily 的 GitHub Actions + SMTP 邮件 | 免费、无需开机；备选本机 crontab |
| 语言/环境 | Python（兼容 3.9），依赖仅 requests | 本机系统 Python 3.9；GitHub Actions 用 3.11 |

## 三、系统架构

```
┌─ 拉取 ──────────────────────────────────────────────┐
│ 新论文池(近30天)  经典池(被引≥40,每关键词配额40)  追踪作者池 │
│ Semantic Scholar bulk search + author/papers         │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─ 富化 ── OpenAlex 补缺失摘要/期刊/PDF ──────────────┐
└──────────────┬──────────────────────────────────────┘
               ▼
┌─ 打分 ── Kimi，锚定「兴趣画像精简版」──────────────┐
│ 相关度0-10 + 新颖度0-10 + paper_type + matched_entries │
│ + negative_hit + reason + overview_seed（批量5篇/次，  │
│ 解析不全自动重试3次）                                  │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─ 过滤与凑满 ────────────────────────────────────────┐
│ ① 内容偏好过滤(paper_type∉偏好→rejected留痕)          │
│ ② 分数线(相关≥7 新颖≥4;追踪作者池相关≥5)               │
│ ③ 按 mix 配额凑满3篇: 作者(额外不占配额)→经典1→新近2    │
│    →剩余过线补位→视野宽度兜底(wide: 相关≥5经典补位)     │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─ 生成与推送 ─────────────────────────────────────────┐
│ 每篇生成概览页(docs/papers/<id>.html) + 存档索引页      │
│ HTML 邮件(163 SMTP SSL) · 页脚带「画像 vN」            │
└──────────────┬──────────────────────────────────────┘
               ▼
┌─ 账本 ── pushed.json(推过不再推) + rejected.json ────┐
│        (评过未过线不再评，保证队列每日推进、LLM不浪费)   │
└──────────────────────────────────────────────────────┘

定时：GitHub Actions cron（当前设北京时间 09:07）或本机 crontab
```

## 四、兴趣画像系统（用户自研 spec，已实现）

### 4.1 画像文件 `config/interest_profile.json`

当前 v1：3 条核心问题（中文）、9 个英文主题词（weight 0.8-0.9、source、updated_at）、1 条排除（无实证评论类文章）、14 条英文检索串（高权重主题 2-3 条同义变体）、tracked_authors_suggested（空）。

### 4.2 画像编译器 `profile_compiler.py`

- 输入：自由文本 / ingest 产出的兴趣声明 + current_profile + mode(increment/replace) + source_tag
- LLM 按 spec prompt 输出严格 JSON（diff / new_profile / preview_text / clarifying_questions / detected_reset_intent）
- 交互：展示 preview_text → [y] 确认写回 / [r] 改整体替换 / [n] 放弃；replace 后提示并可一键重置 rejected.json；检测到重置语义但 mode=increment 时提示选 [r]
- 合并规则（spec）：新增默认 0.8、强烈语气 0.9-1.0、paper 来源 0.9；再提 +0.1 封顶 1.0；「少推/不推 X」-0.3，低于 0.3 移入 negative

### 4.3 论文摄入 `ingest.py`

- 输入 DOI/S2 ID（或直接粘贴摘要文本）→ S2 拉 meta+abstract+references（前 100 条）
- LLM 按 spec prompt 产出：一句话概括、core_question、**第一人称兴趣声明**（「我在关注 X 方向的 Y 问题」）、topics_hint、cited_author_candidates（参考文献中出现≥2 次的作者，只报名字线索不编造数字）
- 声明文本以 `source_tag=paper:<DOI>` 喂编译器走同一预览确认 → 确认后作者候选逐个问「要追踪吗」→ 接 S2 按名搜索 → 选真实 author ID 加入追踪

### 4.4 打分 prompt（spec②，main.py 内）

锚定 profile_digest（core_questions + topics 带权重 + negative）；校准锚点：
- 直接命中核心问题（如《Elements of Successful School Reentry》类）= 10
- 泛泛青少年抑郁/精神科、与复学过渡和社工干预均无直接关联 ≤ 5
- 邻近领域（普通返校、成人出院、纯校园社工无精神科背景）= 4-6
- **命中 negative 任一条目 → 相关度 ≤5**（且 negative_hit=true）
- 经典池论文按「发表当年」的新颖度评判
- matched_entries 必须引用画像原文条目（推荐理由可溯源）
- 摘要不足时降 1-2 分并在 reason 注明「基于摘要判断」

## 五、关键工程细节

### 5.1 小领域适配（实测踩坑后的设计）
- **经典池按关键词配额**（每词 top 40）而非全局合并排序——否则被引 700+ 的大领域论文挤占全部席位
- **每日经典候选按关键词轮换**（每词 4 篇打分）——复学经典凭相关度胜出而非拼被引
- **被引门槛 40**（原 50）——领域奠基论文《School Reintegration Following Psychiatric Hospitalization》被引正好 49
- **新论文窗口 30 天**——小领域 14 天候选太少
- **rejected.json 账本**——未过线论文记档不再重复评审，否则同一批失败经典每天重复打分、队列永不推进

### 5.2 凑满 3 篇的优先级
追踪作者过线者（额外不占配额）→ 经典配额 1 → 新近配额 2 → 剩余过线经典/新近补位 → 视野宽度兜底（strict 不兜底 / balanced 相关≥6 / **wide 相关≥5（当前档位）**）。拓宽档会在下次画像变更时提示回落，防兴趣漂移。

### 5.3 健壮性
- S2 无 key 限流：指数退避重试（无 key 45s 起步）+ 单关键词失败容错不拖垮整轮
- LLM 输出截断曾致整批打分丢失 → 批次解析不全自动重试 3 次、跨次合并、仍失败告警；打分用摘要截 800 字符降截断概率
- 当日候选缓存 `data/candidates_cache_<date>.json` 带检索串指纹，画像检索串变更自动作废重拉
- h-index 门槛：取论文作者最高 h-index，未知保留，仅作用新论文池（默认关）

### 5.4 呈现
- **邮件卡片**：中文标题 + 英文原题 + 池别/分数/paper_type 小字条 + 极简摘要 + 💡推荐理由（引用画像条目）+「📖 概览」按钮
- **概览页**（学术杂志风，2026-10-13 改版）：信息 kicker 行、衬线中文大标题、01-05 编号章节（摘要/研究问题/研究方法/结论/创新点·启发）、细分隔线、移动端适配；内容用 overview_seed + 摘要生成
- **存档索引页** `docs/index.html`：倒序列表，Pages 托管后可公开访问

## 六、配置体系 `config/settings.json`

```json
{
  "push": {"time": "09:07", "timezone": "Asia/Shanghai", "count": 3},
  "mix": {"recent": 2, "classic": 1, "tracked_author_extra": true},
  "taste": {"paper_types": ["empirical", "review", "intervention"],
            "strictness": "wide", "relevance_min": 7, "novelty_min": 4},
  "filters": {"h_index_min": 0},
  "profile_version": 1,
  "llm": {"base_url": "https://api.kimi.com/coding/v1", "model": "k3-256k"},
  "email": {"smtp_server": "smtp.163.com", "smtp_port": 465},
  "sources": {"new_pool_days": 30, "classic_min_citations": 40,
              "classic_per_keyword": 40, "classic_candidates_per_keyword": 4},
  "tracked_authors": [], "author_pool": {"relevance_min": 5},
  "pages_base": "", "send_empty": true
}
```

`configure.py` 六入口菜单：**修改兴趣方向**（编译器）/ **导入论文**（ingest）/ **推送与筛选设置**（推送时间含 crontab 改写+Actions cron 同步询问、篇数、新旧配比、内容偏好复选、视野宽度三档、分数线、h-index 门槛、Pages 地址）/ **追踪作者管理**（按名搜 S2 → 候选含真实 h-index → 选号添加）/ **查看当前画像** / **试跑**。保存设置自动同步 profile_version。

## 七、验证记录（全部实测）

| 日期 | 验证项 | 结果 |
|---|---|---|
| 10-12 | S2 拉取（无 key 限流重试） | ✅ 新 2 篇 + 经典 150 篇 |
| 10-12 | Kimi 端点 | ✅ k3-256k；发现 temperature 仅允许 1（已修） |
| 10-12 | 双分制筛选 | ✅ 复学奠基论文相关 10 被选中；772 引泛抑郁研究相关 2 被拦截 |
| 10-13 | OpenAlex 补摘要 | ✅ 13 篇补到真实摘要，概览告别「据标题推断」 |
| 10-13 | 凑满 3 篇 + 打分重试 | ✅ 22 篇全打分，3 篇入选 |
| 10-13 | 真实发信 ×2 | ✅ 均送达 15176213201@163.com |
| 10-13 | 画像编译器/ingest/settings.json 菜单（子代理实施） | ✅ 非交互验证全绿 |
| 10-13 | 拓宽档+门槛40 | ✅ Tough Transitions(被引44)、School Reintegration(43) 等入围 |

## 八、待办与上线计划

1. **GitHub 上线**（用户配合建 Public 仓库）→ 推代码 → 仓库 Secrets 配 5 个（MOONSHOT_API_KEY / EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECEIVER / S2_KEY）→ Settings → Pages 选 main 分支 /docs 目录 → `pages_base` 填 `https://<用户名>.github.io/<仓库名>`
2. **上线时清空 pushed.json + rejected.json**——用户明确要求：历史论文按新画像全部重推
3. 待申请：Semantic Scholar API key（用户已提交申请，下来填 .env 的 S2_KEY，拉取不再磨限流）
4. 可选：configure.py 添加追踪作者（如 Mark Weist，h-index 53）

## 九、已知取舍

- **拓宽档 vs 推送纯净度**：wide 档会有相关度 5 的邻近论文进入推送（用户已选择，宁可杂一点不断更）
- **打分波动**：LLM 打分存在 ±1 正常波动，分数线 7/4 留有安全边距
- **rejected 永久记档**：边界论文（6 分）一旦记档不再重评，换取队列推进与成本控制；整体替换画像时会提示重置
- **概览链接**：pages_base 未配置前邮件里概览是本地文件链接（手机点不开），上线 Pages 后变公开网址

## 十、文件清单

```
paper-digest-agent/
├── main.py                  # 主流程：拉取/富化/打分/选择/概览页/邮件/账本
├── configure.py             # 交互设置六入口菜单
├── profile_compiler.py      # 兴趣画像编译器（spec prompt 内嵌）
├── ingest.py                # 论文摄入：兴趣声明提取 + 作者候选
├── config/
│   ├── interest_profile.json  # 兴趣画像 v1
│   └── settings.json          # 统一配置
├── data/                    # pushed.json / rejected.json / 当日缓存 / preview.html
├── docs/                    # 概览页（Pages 托管）papers/*.html + index.html
├── .github/workflows/daily.yml  # 定时 workflow（北京时间 09:07 + 手动触发）
├── .env(.example)           # 密钥（gitignore）
├── requirements.txt         # requests
└── README.md
```

---

## 十一、通用化 + 多账号改造（2026-10-13 实施）

### 11.1 目标与架构变化

- **领域通用化**：精神科复学社工相关内容全部从代码/prompt 抽离，改为运行时注入的 `domain_context`（core_questions 拼接 + 权重前 5 topics；冷启动用用户原文，`profile_compiler.domain_context_of()` 三处共用）。任何领域用户经 `python setup.py` 向导生成画像/锚点/池参数。
- **多账号**：邮箱即账号，一套部署服务多用户。`main.py` 入口自动判模式：存在 `users/` → 逐账号顺序跑完整流水线（单账号故障 try/except 隔离、发信间隔 5-10s、末尾汇总打印）；不存在 → 单用户模式（根目录 config/、data/，既有实例零迁移）。

### 11.2 账号存储与隐私

```
users/
├── registry.json.enc        # Fernet 加密集登记表 [{dir,email,created_at,active}]
└── <sha256(email.lower().strip())[:12]>/
    ├── config/  (interest_profile.json / settings.json)
    └── data/    (pushed.json / rejected.json / candidates_cache_*.json)
```

- 目录名不可逆 hash，邮箱明文只在加密体内；`FERNET_KEY` 存 .env 与 GitHub Secrets。
- 无 `FERNET_KEY` 自动回落明文 `users/registry.json` + 启动警告（Private 仓库适用）；加密模式下明文副本自动删除。
- 概览页 `docs/` 全账号共享（论文本身是公开信息，不含账号数据）。

### 11.3 画像 schema 新增 calibration_anchors

`[{"paper_title","s2id","score","note","source":seed|feedback}]`——编译器只透传不修改（LLM 丢失时代码补回）；
向导种子论文与日常 ingest 确认后由代码追加（score 9）。打分 prompt（Prompt C）分两层锚点：
**用户锚点最高优先级**（与 score≥9 锚点研究问题同类 → 相关度 8-10，攀附禁止）+ 通用锚点
（命中核心问题 9-10 / 高权重主题 7-8 / 邻近 4-6 / 仅沾边 ≤3 / negative 命中压 ≤5）。
打分输出含 `matched_anchor`（日志中以 ⚓ 标记）。首个账号已补 3 篇种子锚点
（Elements of Successful School Reentry / Tough Transitions / School Reintegration and Perceived Needs）。

### 11.4 setup.py 向导（屏 0-7）

账号邮箱（校验/查重/建 hash 目录）→ 研究方向自由文本（编译器冷启动，[y]/[e]补充/[q]）→
种子论文（每行一篇走 ingest，声明喂编译器、生成 seed 锚点；作者候选末尾统一问，S2 解析真实 h-index）→
反向示例（可选，进 negative）→ **领域体量探测**（top5 检索串 × 近30天新增量 + 被引≥40 经典量估算 →
小/中/大三档参数包 PACK_SMALL/MEDIUM/LARGE，失败回落 MEDIUM 并明示）→ 推送偏好（篇数/时间/内容偏好）→
试跑（dry_run 不写账本）→ 完成摘要。`--migrate` 把根目录 config/+data/ 迁入 users/<hash>/ 并登记。

### 11.5 其他变更

- provider 接口抽象（`PaperProvider` 基类 + `S2Provider`；roadmap：arXiv / PubMed 源）。
- `workspace.py` 统一单/多账号路径；`accounts.py` 登记与加密。
- settings.json 增 `domain.setup_completed`；`configure.py` 多账号先选账号再进菜单。
- daily.yml：单 job 顺序跑多账号（不用 matrix，防 S2 限流互撞），提交路径覆盖 users/ 账本与加密登记表；新增 FERNET_KEY secret。
- requirements.txt 增 cryptography。

### 11.6 回归验证（全部实测）

单用户模式 dry-run（通用打分报文，3 篇凑满）→ --migrate（config/data 迁入 users/74a02eb66867/）→
多账号 dry-run（3 篇凑满、账本未动、加密登记正常解密）→ 明文回落（警告+明文读写正常）→
领域探测实跑（见验收报告）→ 全部通过。
