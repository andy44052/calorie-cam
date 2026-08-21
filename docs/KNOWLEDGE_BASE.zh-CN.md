# CalorieCam — 项目知识库

> [English version](KNOWLEDGE_BASE.md)

本文面向从未接触过本项目、但需要维护它的人。建议先读 `ARCHITECTURE.zh-CN.md`
了解整体流程图。

## 模块与职责

| 模块 | 职责 | 输入 → 输出 |
|---|---|---|
| `caloriecam/vision.py` | 视觉系统提示词、图片预处理，以及所有模型调用统一经过的封装 (`structured_call`) | 照片字节 → `FoodAnalysis` |
| `caloriecam/debate.py` | 质疑者与复核者的提示词及对抗式评审流程 | 草稿 → 修订后的草稿 + 评审记录 |
| `caloriecam/lookup.py` | 将食物名称匹配到数据库 | `FoodItem` → `Resolution` 或 `None` |
| `caloriecam/units.py` | 单件重量区间（一颗葡萄、一块寿司） | 食物名 → `UnitBand` 或 `None` |
| `caloriecam/sanity.py` | 分量钳制、不确定区间、逐项来源标记、汇总 | 分析 + 匹配结果 → `MealEstimate` |
| `caloriecam/history.py` | SQLite 餐次日记、个人分量先验、用户修正、金标准样本 | 餐次 → 数据行；名称 → 历史分量 |
| `caloriecam/calibration.py` | 应用已拟合的分来源乘性系数 | 来源 → 系数（未拟合则为 1.0） |
| `caloriecam/pipeline.py` | 粘合层：编排上述全部环节，持有 `needs_debate` | 照片 → `(MealEstimate, FoodAnalysis)` |
| `caloriecam/report.py` | 将结果格式化为命令行文本与 Web JSON | `MealEstimate` → str / dict |
| `caloriecam/usage.py` | 每次调用的 token / 耗时 / 成本台账 | API 响应 → 成本记录 |
| `caloriecam/text.py` | 共享的分词与词干处理（匹配器、单位、历史键均使用） | — |
| `app.py` | FastAPI 服务、PIN 校验、上传大小限制 | HTTP → JSON |
| `estimate.py` | 命令行入口 | argv → stdout |
| `benchmark.py` | 付费基准扫描与评分报告 | 照片目录 → JSONL → 报告 |
| `calibrate.py` | 从金标准餐次拟合校准系数 | 日记 → `calibration.json` |
| `audit_db_misses.py` | 归类食物未命中数据库的原因 | 扫描 JSONL → CSV |

**数据文件**（可直接编辑，无需改代码）：`generic.json`（132 种食物，每 100g
千卡）、`fastfood.json`（26 个连锁餐品，总千卡）、`units.json`（69 条单件重量
区间）。

## 模型与提示词依赖

- **模型：** 三次调用均使用 `claude-opus-5`（`config.DEFAULT_MODEL`）。实测最优
  ——见下方基线。`--skeptic-model` / `--model` 可切换模型，`--critic-count N`
  可启用质疑者集成；两者默认关闭。
- **提示词：** `vision.SYSTEM_PROMPT`（识别、画面边界、整盘 vs 单份规则、
  优先计数的分量估算、散装食物几何推算、能量密度指引）、
  `debate.CRITIC_SYSTEM`、`debate.REVISER_SYSTEM`，以及
  `debate.EAGER_CRITIC_SUPPLEMENT`（仅当质疑者运行在比主模型更便宜的模型上时
  自动追加）。
- **结构化输出：** 所有调用均使用 `client.messages.parse` 配合 Pydantic schema。
  Pydantic 的 `Field` 约束（ge/le）在强制 schema 中会被**剥离**——必须在 Python
  中校验，绝不能依赖 schema。

## 降级与回退逻辑

| 故障 | 行为 |
|---|---|
| 复核者输出被截断（`TruncatedError`） | 返回**草稿**结果并记录 `reviser_truncated`。绝不丢失用户已付费的估算。 |
| 质疑者未提出任何问题 | 直接跳过复核者（省一次调用） |
| 照片中没有食物 | 跳过评审、跳过写入日记——一张键盘照片不该变成 0 千卡的"一餐" |
| 数据库无匹配 | 保留模型自身数值，标记为 `model_estimate`，并放宽区间 |
| 日记写入失败（数据库被锁、磁盘满） | 仍返回估算，只是不带 `meal_id` / `today` |
| API 拒绝响应（`stop_reason: refusal`） | 抛出 `RefusalError` → HTTP 422 |
| PIN 请求头含非 ASCII 字节 | 返回 401，而非 500 |

## 数据库匹配规则

**根本原则：错误的匹配比不匹配更糟。** 模型自身的估算是可接受的兜底；一个
自信但错误的数据库数值不是。

- 匹配 = 条目的词元是待匹配项词元的**子集**，按 Jaccard 方式打分；阈值为
  0.80（连锁餐品）/ 0.78（通用食物）。
- 多余的词必须是**无害修饰词**（`_MODIFIERS`：烹饪方式、尺寸、数量、颜色、
  vine/cluster）。否则单一食材就会冒领复合菜品——"banana bread"（香蕉面包）
  不是 banana（香蕉）。
- `loose_match: true` 标记那些理应吸收食材词的条目（仅限复合菜品）。
- `exclude_tokens` 直接否决匹配。**只能是单个词——多词排除项会静默失效。**
  且必须对照该条目自身的别名检查：给 taco 条目加 "shells" 会把它自己的
  "hard shell taco" 别名一并杀死。
- 直接匹配失败后有两种重试归一化：**剥离括号内容**，以及**取配菜从句的主干**
  （如 "french fries with seasoning"）。两者都会对**原始名称**重新校验排除项，
  因此剥离操作绝不可能绕过否决。当被丢弃的从句本身指向一种真实食物时，主干
  重试会拒绝执行。
- **已被否决的方案——请勿重新提出：** 当热量密度大致吻合时就接受子集匹配。
  经红队攻击与实测：复合菜品的密度往往落在其自身食材的 1.6 倍以内
  （三明治 vs 面包 1.16 倍、黄油饼干 vs 黄油 1.43 倍），因此密度无法证明身份。

## 分量估算规则

1. 离散食物**优先计数**：`unit_count x per_unit_grams`，并钳制到 `units.json`
   中的 USDA 区间。计数的可重复性远高于目测一堆食物。
2. 只有当区间衡量的**正是被计数的那个单位**时才可套用——12 片牛油果**切片**
   不能按 12 个整颗牛油果计价（`_PIECE_WORDS`）。
3. 只有当 `count x serving` 落在模型自身克数估算的 45% 以内时，才采信其给出的
   计数（平底锅披萨防护规则）。
4. **散装食物**走几何推算链：占盘比例 x 厚度 → 体积 → 克数，提示词中提供了
   常见熟食密度。
5. 克数钳制在 1–2000 g；低置信度或模型估算来源会自动放宽区间。

## 个人分量与校准

- **分量收缩：** 重复出现的食物（历史出现 ≥2 次，取最近 10 次）以
  `n/(n+2)` 的权重向用户中位数收缩。跳过可计数项与品牌餐品。当历史密度与当前
  条目相差 >1.6 倍时拒绝收缩（名称键冲突防护："grilled cheese" 不能借用普通
  cheese 的分量历史）。
- **用户修正**在**读取时**生效，绝不改写已存储的原始估算。比值钳制在
  0.2–5 倍，避免一次输入错误污染先验。
- **校准**按来源对千卡乘以拟合系数。在 `calibration.json` 存在之前不生效。
  `calibrate.py fit` 需要 ≥10 次**实测**餐次，系数钳制在 ±15%，小样本时向 1.0
  收缩，出现次数 <3 的来源直接跳过。
- **两者共同的铁律：系统绝不用自己的输出训练自己。** 日记存储的是收缩前的
  克数；每个条目都记录了所应用的校准系数，拟合时会先把它除回去。

## 当前基准基线（Run A：23 张照片 x 3 轮）

| 指标 | 数值 |
|---|---|
| 相对已核实餐次的平均误差 | **8%** |
| 偏差 | **+2%** |
| 真值照片落在容差内 | **6 / 6** |
| 由数据库支撑的卡路里占比 | **74%** |
| 食物错配次数 | **0** |
| 每张照片成本 | **18.8 美分** |
| 多轮波动（中位数） | 11%（≥300 千卡的餐次为 12%→11%） |

同一批照片上的更廉价配置：Haiku 质疑者 6.6 美分，但误差 14% / 偏差 −10%；
全 Sonnet 11.5 美分，18% / +8%；Sonnet 质疑者 13 美分，16% / −11%。
**高价质疑者的强硬质疑正是准确度的来源**——在 68 轮中有 64 轮改变了最终答案。

## 回归用例（这些测试为何存在）

| 测试文件 | 所防止的缺陷 |
|---|---|
| `test_collision_audit.py` | 任何别名匹配到热量相差 >2 倍的食物；失效别名；多词排除项。替代了一次付费的 515 探针红队攻击，运行约 2 秒 |
| `test_coverage_growth.py` | 牛排冒领奶油牛排意面；黄油冒领黄油饼干；12 片被当作 12 整个 |
| `test_history.py` | 修正一次已收缩的餐次污染先验；品牌餐品被收缩；数字导致历史键被拆分 |
| `test_calibration.py` | 校准复合自身输出；样本不足 10 次仍拟合；旧版数据库迁移 |
| `test_debate.py` | 复核者被截断导致付费估算丢失；集成去重 |
| `test_skip_debate.py` | 颜色变体被误判为未匹配 |

## 已知限制

- **无定形堆状食物**（如一堆意面）是最薄弱的一类——既无可计数单位，也无标准
  重量。唯一真正的解法是多次取中位数采样，而这会按张产生费用。
- **系统性的向上压力尚未解决。** 92% 的质疑都在把估算往上推；单位区间钳制把
  15 个条目调高、仅 3 个调低。当应用整体低估 6% 时这是正确的；如今它作用在
  已经准确的数据库数值之上。校准是既定的解法，等待金标准餐次积累。
- **葡萄粒数漂移** —— 两次扫描之间计数从 90–100 涨到 110–164，相对真值 +55%。
  刻意未修补；需要独立的测量。
- **真值集只有 6 张照片。** 所有准确度数字都建立在它们之上。"已实测"复选框
  正是为扩充它而设。
- **Render 免费套餐磁盘是临时的** —— 每次部署日记都会清空。在接入持久化磁盘
  之前，日记类功能以本地服务器为主。
- **方法论警示：** 曾有一条提示词规则是在一次"同时也更换了模型"的扫描结果上
  调优的。信号被混淆，结论是错的。**每次扫描只改一个变量。**

## 运行、测试与部署

```powershell
# 初始化
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # 然后填入 ANTHROPIC_API_KEY

# 运行
start-web.cmd                                   # Web 服务，手机可访问
.venv\Scripts\python.exe estimate.py photo.jpg  # 命令行

# 测试 —— 全部离线，不产生 API 费用
.venv\Scripts\python.exe -m pytest -q           # 312 个测试，约 15 秒

# 基准扫描 —— 会产生费用，务必设置 --max-cost
.venv\Scripts\python.exe benchmark.py run photos\ --runs 3 --out r.jsonl --max-cost 15
.venv\Scripts\python.exe benchmark.py report r.jsonl --truth truth.json --compare runA.jsonl

# 校准
.venv\Scripts\python.exe calibrate.py show      # 查看金标准餐次数量
.venv\Scripts\python.exe calibrate.py fit       # 需要 >= 10 次实测餐次
```

**部署：** 推送到 GitHub，Render 会自动读取 `render.yaml`。在控制台中设置
`ANTHROPIC_API_KEY` 与 `CALORIECAM_PIN`。

**环境变量：** `CALORIECAM_PIN`、`CALORIECAM_DEBATE=off`、
`CALORIECAM_HISTORY=off|路径`、`CALORIECAM_SKEPTIC_MODEL`、
`CALORIECAM_CRITIC_COUNT`、`CALORIECAM_CALIBRATION`、`PORT`。

**曾造成真实调试成本的坑：** 数据文件必须以 `utf-8-sig` 读取（Windows 编辑器
会加 BOM）；`secrets.compare_digest` 遇到非 ASCII 字符串会抛异常（应比较
bytes）；SDK 会在 `stop_reason` 可见**之前**先校验结构化输出的 JSON，因此截断
表现为 `ValidationError`，而不是一个清晰的信号。
