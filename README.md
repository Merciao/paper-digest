# 每日论文推送 Agent（Paper Digest Agent）

每天自动筛选**你的研究领域**里值得读的论文，推送到你的邮箱——不一定是最新的，但一定是有价值的。
任何学科均可：首次运行向导 `python setup.py` 用自然语言（+ 可选种子论文）生成你的专属兴趣画像与检索策略。

- 数据源：Semantic Scholar（主体）+ OpenAlex（补缺失摘要），学术覆盖远超 arXiv
- 打分：LLM 依据**结构化兴趣画像 + 你亲自给的校准锚点**打 相关度/新颖度 双分，推荐理由可溯源
- 三池设计：新近论文 + 高引经典（每天精读）+ 追踪作者（大牛一发新文就推）
- 每篇附**中文概览页**（摘要/研究问题/方法/结论/创新点启发），GitHub Pages 托管，手机可读
- 多账号：一套部署服务多个用户，邮箱即账号，画像/设置/账本隔离，登记表 Fernet 加密
- 定时：GitHub Actions 每日自动跑（免费），或本机 crontab

## 快速开始（新用户）

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入 MOONSHOT_API_KEY（Kimi）、EMAIL_*（163 SMTP）、可选 S2_KEY、FERNET_KEY
python setup.py        # 5 分钟向导：邮箱 → 研究方向 → 种子论文 → 体量探测 → 推送偏好 → 试跑
```

向导会探测你的领域体量（近 30 天新增量 + 经典池规模），自动推荐参数：
小领域（宽视野/低被引门槛/30 天窗口）、中等、大领域（严筛选/高门槛/14 天窗口）。

之后每天到点自动推送。想调整随时：

```bash
python configure.py    # 修改兴趣方向 / 导入论文 / 推送与筛选设置 / 追踪作者 / 查看画像 / 试跑
```

## 多账号

- 每个账号 = 一个邮箱；账号目录 `users/<sha256(email)[:12]>/`，邮箱明文不进仓库
- 登记表 `users/registry.json.enc` 用 `FERNET_KEY` 加密（Public 仓库必须配置；Private 仓库可不配，自动回落明文并警告）
- 主程序 `python main.py` 自动检测：有 `users/` → 逐账号跑完整流水线（单账号故障隔离、发信间隔 5-10s）；无 → 单用户模式（根目录 config/、data/）
- 既有单用户实例迁移：`python setup.py --migrate`

## 部署到 GitHub Actions

1. 建 **Public** 仓库（Pages 需要；密钥都在 Secrets 不泄露）并 push 代码
2. Settings → Secrets and variables → Actions 添加：
   `MOONSHOT_API_KEY`（Kimi Code key）、`EMAIL_SENDER` / `EMAIL_PASSWORD`（163 SMTP 授权码）、
   `EMAIL_RECEIVER`（单用户模式收件人；多账号模式可留空，逐账号取登记邮箱）、
   `S2_KEY`（可选，[免费申请](https://www.semanticscholar.org/product/api)）、`FERNET_KEY`
3. Settings → Pages → Deploy from a branch → main /docs —— 概览页即公开可访问，
   把 `https://<用户名>.github.io/<仓库名>` 填到 configure.py 的 Pages 地址
4. Actions 手动触发一次验证；默认定时北京时间 09:07（`.github/workflows/daily.yml` 可改）

## 本地运行

```bash
python main.py            # 真实推送（按 users/ 或根目录配置自动判模式）
python main.py --dry-run  # 试跑：不发邮件不记账，预览写 data/preview.html
```

本机定时：`python configure.py` → 推送与筛选设置 → 推送时间，可重写 crontab（带标记行）。

## 工作原理（简）

```
拉取（S2 三池）→ OpenAlex 补摘要 → Kimi 画像打分（含你的校准锚点）
→ 类型/分数线过滤 → 按新旧配比凑满每日篇数（视野宽度三档兜底）
→ 生成概览页 → 163 SMTP 发信 → 双账本（推过的不再推、评过未过的不再评）
```

画像编译器与论文摄入（`profile_compiler.py` / `ingest.py`）让你用自然语言或扔一篇范文就能调整研究方向，
每次变更先出 diff 预览、你确认才落盘。详细设计与演进记录见 `DESIGN.md`。

## 文件导览

```
main.py               主流程（含 provider 接口；roadmap：arXiv / PubMed 源）
setup.py              新用户向导 + 既有实例迁移
configure.py          交互设置菜单
profile_compiler.py   兴趣画像编译器（spec：严格 JSON + 预览确认）
ingest.py             论文摄入：兴趣声明提取 + 作者候选 + 校准锚点
accounts.py           多账号登记（Fernet 加密 / 明文回落）
workspace.py          单/多账号路径抽象
docs/                 概览页（Pages 托管）
users/                多账号数据（hash 目录，登记表加密）
```
