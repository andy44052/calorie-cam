# CalorieCam — 系统架构

> [English version](ARCHITECTURE.md)

输入一张照片，输出逐项的卡路里估算。贯穿整个设计的核心原则是：
**模型只负责"看"，不负责"算"。** Claude 负责识别食物、估算分量；此后每一个
卡路里数字都来自数据库和确定性的 Python 代码，因此可以离线、零成本地测试。

## 主流程

```mermaid
flowchart TD
    Photo["照片 + 可选备注<br/>(手机 / 命令行 / 基准测试)"] --> Prep

    Prep["prepare_image<br/>EXIF 旋转校正、缩放至 1568px、JPEG q85"] --> Vision

    Vision["<b>模型调用 1 — 视觉识别</b><br/>analyze_prepared<br/>-> FoodAnalysis JSON"] --> Gate

    Gate{"needs_debate?<br/>存在无数据库锚定的条目、<br/>条目数 >= 6、或区间宽度 > 35%"}
    Gate -->|否| Lookup
    Gate -->|是| Critic

    Critic["<b>模型调用 2 — 质疑者</b><br/>找出草稿中的具体问题"] --> HasCh
    HasCh{"是否提出<br/>质疑?"}
    HasCh -->|否| Lookup
    HasCh -->|是| Reviser["<b>模型调用 3 — 复核者</b><br/>逐条裁决并重新输出分析"]
    Reviser -->|输出被截断| Fallback["回退到草稿<br/>并记录 reviser_truncated"]
    Reviser --> Lookup
    Fallback --> Lookup

    Lookup["<b>数据库匹配</b> — lookup.py<br/>先匹配连锁餐品，再匹配通用food<br/>+ 2 种重试归一化策略"] --> Sanity

    Sanity["<b>分量与合理性校验</b> — sanity.py<br/>单位重量区间钳制、克数钳制、<br/>不确定区间、逐项来源标记"] --> Blend

    Blend["<b>个人分量学习</b> — history<br/>重复出现的食物向<br/>你的历史中位数收缩<br/>(跳过可计数项与品牌餐品)"] --> Cal

    Cal["<b>校准</b> — calibration.py<br/>按来源乘以系数<br/>(未拟合前恒为 1.0)"] --> Total

    Total["汇总与报告<br/>低 / 中 / 高"] --> Out

    Out["Web JSON、命令行文本、<br/>或基准测试记录"] --> Diary
    Diary[("history.db<br/>混合前的原始估算、<br/>用户修正、金标准样本")]

    Diary -.->|"历史分量"| Blend
    Diary -.->|"已核实餐次"| Fit["calibrate.py fit<br/>岭回归最小二乘"]
    Fit -.->|"写入"| CalFile[("calibration.json")]
    CalFile -.-> Cal

    DB[("generic.json 132 条<br/>fastfood.json 26 条<br/>units.json 69 条")]
    DB -.-> Lookup
    DB -.-> Sanity

    style Vision fill:#1e56d6,color:#fff
    style Critic fill:#1e56d6,color:#fff
    style Reviser fill:#1e56d6,color:#fff
    style Lookup fill:#e8edf5
    style Sanity fill:#e8edf5
    style Cal fill:#e8edf5
```

蓝色 = 产生费用的环节（API 调用）。其余均为确定性 Python 代码，运行免费——
这也是绝大部分测试都在离线完成的原因。

## 成本构成

一张照片 = 1～3 次模型调用。视觉调用始终执行；当 `needs_debate` 判定草稿存在
不确定性时才调用质疑者（实际约 95% 的照片会触发）；只有质疑者确实提出问题时
才会调用复核者。实测均值：**每张 18.8 美分**，成本主要由输出 token 决定，因此
随画面复杂度变化（两个苹果 4.9 美分，一整块冷盘拼盘 45.6 美分）。

## 验证闭环

```mermaid
flowchart LR
    Change["代码或数据变更"] --> Offline

    Offline["<b>免费，每次提交</b><br/>312 个 pytest 测试<br/>含覆盖全部数据库别名的<br/>冲突审计"] --> Replay

    Replay["<b>免费，按需</b><br/>用新匹配器重放<br/>历史扫描的模型输出"] --> Sweep

    Sweep["<b>付费</b><br/>benchmark.py run<br/>N 张照片 x M 轮<br/>--max-cost 限制开销"] --> Report

    Report["benchmark.py report<br/>对比真值 + 对比基线"] --> Gates

    Gates{"发布门槛<br/>覆盖率、平均误差、偏差、<br/>波动、错配数、成本"}
    Gates -->|通过| Ship["部署上线"]
    Gates -->|未通过| Revert["回退到上一个<br/>已实测良好的状态"]

    style Sweep fill:#1e56d6,color:#fff
```

最关键的一条规则：**付费扫描用于"确认"，而不是用于"发现"。** 离线测试和免费
重放能以零成本回答绝大多数问题；付费扫描的存在意义是验证。以及
**每次扫描只改一个变量**——曾经有一次在同时更换了模型的扫描结果上去调提示词，
直接得出了错误结论（详见 `docs/KNOWLEDGE_BASE.zh-CN.md` → 已知限制）。
