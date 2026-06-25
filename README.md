# daily_scores.py 说明文档（调用者参考）

> 单日就绪度评分模块。从单日睡眠 / 心率 / 步数等输入,计算三个分数:
> **SleepPerformance(0–100)**、**Recovery(0–100)**、**Strain(0–21)**。
>
> 设计原则:**无个人基线、仅用当日输入、全显式分段公式、跨运行时确定性舍入、零三方依赖**。Python 3.10+。

---

## 目录
1. [快速开始](#1-快速开始)
2. [输入:DailyScoreInput](#2-输入dailyscoreinput)
3. [输出结构](#3-输出结构)
4. [状态码与告警码](#4-状态码与告警码)
5. [年龄分桶](#5-年龄分桶)
6. [计算流程:SleepPerformance](#6-计算流程sleepperformance)
7. [计算流程:Recovery](#7-计算流程recovery)
8. [计算流程:Strain](#8-计算流程strain)
9. [顶层编排](#9-顶层编排)
10. [常量速查](#10-常量速查)
11. [关键不变量与注意事项](#11-关键不变量与注意事项)

---

## 1. 快速开始

模块暴露 5 个公共函数 + 7 个公共类型(见文件 `__all__`)。两种调用入口:

**A. 对象入口(推荐,类型安全):**
```python
from daily_scores import DailyScoreInput, compute_daily_score

inp = DailyScoreInput(
    age_years=35,
    total_sleep_min=450.0,        # 必填, >0
    deep_sleep_min=80.0,
    rem_sleep_min=90.0,
    sleep_stage_valid=True,
    hrv_night_ms=45.0,
    rhr_sleep_bpm=58.0,
    day_hr_minute_bpm=[78.0, 82.0, ...],   # 白天逐分钟心率
    rhr_valid=True,
    hrv_valid=True,
    day_hr_coverage=0.92,
    day_step_minute_count=[0, 0, 95, ...], # 可选, 与 HR 数组对齐
    waso_min=20.0,                          # 可选
)
out = compute_daily_score(inp)
print(out.status_code, out.sleep_performance.score, out.recovery.score, out.strain.score)
```

**B. 字典入口(JSON 友好,永不抛异常):**
```python
from daily_scores import compute_daily_score_from_dict
result = compute_daily_score_from_dict({
    "age_years": 35, "total_sleep_min": 450.0, "deep_sleep_min": 80.0,
    "rem_sleep_min": 90.0, "sleep_stage_valid": True, "hrv_night_ms": 45.0,
    "rhr_sleep_bpm": 58.0, "day_hr_minute_bpm": [...], "waso_min": 20.0,
})   # 返回纯 dict
```

**单独调用某个分数**也可以:`compute_sleep_performance(inp)`、`compute_recovery(inp, sleep_score)`、`compute_strain(inp)`。注意 `compute_recovery` 需要先传入睡眠分数。

---

## 2. 输入:DailyScoreInput

所有字段集中在一个 `@dataclass(frozen=True)` 里。**不可变**,构造后不能改。

| 字段 | 类型 | 单位 | 必填 | 默认 | 被哪个分数用 | 说明 |
|---|---|---|---|---|---|---|
| `age_years` | `Optional[int]` | 岁 | 否(强烈建议) | — | 三者 | 年龄分桶依据(HRmax、HRV/RHR 参考、RHR 回退)。缺失/非法 → **桶 1**;有效范围 [0,120] |
| `total_sleep_min` | `float` | 分钟 | **是** | — | Sleep | 必须 **> 0**,否则报错。`< 180` → 低置信 |
| `deep_sleep_min` | `float` | 分钟 | 是¹ | — | Sleep | 绝对深睡时长 |
| `rem_sleep_min` | `float` | 分钟 | 是¹ | — | Sleep | 绝对 REM 时长 |
| `sleep_stage_valid` | `bool` | — | **是** | — | Sleep | 分期是否可信;为假 → 恢复性分量缺席、走回退 |
| `hrv_night_ms` | `Optional[float]` | ms | 否 | — | Recovery | 夜间 HRV(RMSSD)。有效区间 **[5, 250]** |
| `rhr_sleep_bpm` | `Optional[float]` | bpm | 否 | — | Recovery, Strain | 睡眠静息心率。有效区间 **[30, 100]** |
| `day_hr_minute_bpm` | `Sequence[Optional[float]]` | bpm | **是** | — | Strain | 白天逐分钟心率序列。每个元素有效区间 **[35, 220]**,否则该分钟跳过 |
| `hrv_valid` | `Optional[bool]` | — | 否 | `None` | Recovery | 显式有效标志。`True` 只有在数值本身合法时才确认有效;`False` 强制无效;`None` → 看数值 |
| `rhr_valid` | `Optional[bool]` | — | 否 | `None` | Recovery, Strain | 同上,针对 RHR |
| `day_hr_coverage` | `Optional[float]` | 比例 [0,1] | 否 | `None` | Strain | 白天心率覆盖率。`< 0.5` → Strain 低置信(分数照出,仅标记) |
| `day_step_minute_count` | `Optional[Sequence[Optional[float]]]` | 步/分钟 | 否 | `None` | Strain | **逐分钟步数,与 HR 数组按下标对齐**。用于"低心率走路放行"。某分钟步频在 **[40, 250]** spm 时判为走路 |
| `waso_min` | `Optional[float]` | 分钟 | 否 | `None` | Sleep | 入睡后清醒时长(Wake After Sleep Onset)。用于连续性分量;无此输入则该分量缺席并重分配权重 |

¹ 深睡/REM 是"分期恢复性"所需;若缺失或 `deep+rem > total`,恢复性分量被丢弃,只按充足度(+连续性)计分。

**`DailyScoreInput.from_dict(data)`**:从 dict 构造。缺 `day_hr_minute_bpm` / `total_sleep_min` / `deep_sleep_min` / `rem_sleep_min` / `sleep_stage_valid` 会抛 `ValueError`;类型错误抛 `TypeError`。数值字段经 `_ensure_number` 清洗(拒绝 `bool`、`NaN`、`Inf`)。

---

## 3. 输出结构

### 3.1 顶层 `DailyScoreOutput`(`compute_daily_score` 返回)

| 字段 | 类型 | 说明 |
|---|---|---|
| `sleep_performance` | `Optional[SleepPerformanceOutput]` | 失败时为 `None` |
| `recovery` | `Optional[RecoveryOutput]` | 失败时为 `None` |
| `strain` | `Optional[StrainOutput]` | 失败时为 `None` |
| `status_code` | `StatusCode` | 见 §4 |
| `warning_codes` | `tuple[WarningCode, ...]` | 三个分数的告警**去重并按码值排序**后的合集 |

`.to_dict()` 产出 JSON 友好结构(`status_code` 与 `warning_codes` 转为 int)。

### 3.2 `SleepPerformanceOutput`

| 字段 | 类型 | 说明 |
|---|---|---|
| `score` | `int` | 0–100 |
| `level` | `str` | `poor`(≤59)/ `fair`(60–79)/ `good`(≥80) |
| `sleep_stage_fallback` | `bool` | 分期不可用 → `True` |
| `low_confidence` | `bool` | `total_sleep_min < 180` → `True` |
| `components` | `SleepComponents` | 见下 |

**`SleepComponents`**:`sleep_suff_score`(充足度子分)、`restorative_score`(恢复性子分,缺席为 `None`)、`restorative_ratio`((深+REM)/总,诊断用)、`total/deep/rem_sleep_min`(回显)、`deep_score`/`rem_score`(绝对深睡/REM 子分)、`continuity_score`(连续性子分,缺席为 `None`)。

### 3.3 `RecoveryOutput`

| 字段 | 类型 | 说明 |
|---|---|---|
| `score` | `int` | 0–100 |
| `level` | `str` | `low`(≤33)/ `medium`(34–66)/ `high`(≥67) |
| `fallback_level` | `int` | `0`=全量(HRV+RHR+Sleep)、`1`=部分(缺 HRV 或缺 RHR)、`2`=全回退(仅 Sleep) |
| `components` | `RecoveryComponents` | `hrv_score?`、`rhr_score?`、`sleep_score`、`hrv_night_ms?`、`rhr_sleep_bpm?` |

### 3.4 `StrainOutput`

| 字段 | 类型 | 说明 |
|---|---|---|
| `score` | `float` | 0–21,一位小数 |
| `level` | `str` | `very_low`(≤5.9)/ `low`(≤9.9)/ `moderate`(≤14.9)/ `high`(≤17.9)/ `very_high`(≥18) |
| `valid` | `bool` | 无任何有效心率分钟 → `False`(分数 0） |
| `fallback_rhr` | `bool` | RHR 不可用、用了人群回退值 → `True` |
| `low_confidence` | `bool` | `day_hr_coverage < 0.5` → `True` |
| `components` | `StrainComponents` | `trimp_day`(全天 TRIMP)、`hrmax_est`(估计最大心率)、`rhr_for_strain_bpm`(实际用的静息心率)、`zone_minutes`(各 HR 区分钟数,**诊断字段,不参与打分**) |

---

## 4. 状态码与告警码

**`StatusCode`(致命,决定是否产出分数):**

| 码 | 名称 | 含义 |
|---|---|---|
| 0 | `SUCCESS` | 成功 |
| 1001 | `INVALID_TOTAL_SLEEP` | `total_sleep_min ≤ 0` |
| 1002 | `INVALID_REQUIRED_FIELD` | 必填字段缺失/类型错误,或计算中异常 |
| 1003 | `INVALID_DAY_HR_ARRAY` | `day_hr_minute_bpm` 缺失或不是序列 |

**`WarningCode`(非致命,分数照出,仅标记):**

| 码 | 名称 | 触发 |
|---|---|---|
| 2001 | `SLEEP_STAGE_FALLBACK` | 睡眠分期不可用 |
| 2002 | `SLEEP_LOW_CONFIDENCE` | 总睡眠 < 180 分钟 |
| 2003 | `HRV_INVALID_FALLBACK` | HRV 无效 |
| 2004 | `RHR_INVALID_FALLBACK` | RHR 无效 |
| 2005 | `RECOVERY_PARTIAL_FALLBACK` | Recovery 缺 HRV 或缺 RHR |
| 2006 | `RECOVERY_FULL_FALLBACK` | Recovery 仅剩 Sleep |
| 2007 | `STRAIN_RHR_FALLBACK` | Strain 用了 RHR 人群回退值 |
| 2008 | `STRAIN_LOW_CONFIDENCE` | Strain 心率覆盖率 < 0.5 |
| 2009 | `NO_VALID_DAY_HR` | 白天无任何有效心率分钟 |

---

## 5. 年龄分桶

多个参考值按年龄桶取(`age_bucket`):

| 年龄 | 桶 | 缺失/非法年龄 |
|---|---|---|
| < 30 | 0 | |
| 30–39 | 1 | ← **默认落桶 1** |
| 40–49 | 2 | |
| 50–59 | 3 | |
| ≥ 60 | 4 | |

按桶索引的数组(桶 0→4):

| 数组 | 值 | 用途 |
|---|---|---|
| `HRV_LOW` | [25, 22, 18, 15, 12] | Recovery：HRV 0 分下界 |
| `HRV_HIGH` | [80, 70, 60, 50, 45] | Recovery：HRV 100 分上界 |
| `RHR_GOOD` | [50, 52, 54, 56, 58] | Recovery：RHR 100 分线;Strain 回退中点的一端 |
| `RHR_POOR` | [72, 74, 76, 78, 80] | Recovery：RHR 0 分线;Strain 回退中点的另一端 |

---

## 6. 计算流程:SleepPerformance

**三分量加权,缺分量按比例归一。** 名义权重:充足度 0.45 / 恢复性 0.25 / 连续性 0.10(和为 0.80,归一后充足度占 56%)。

```
0. 守卫: total_sleep_min ≤ 0 → 抛 ValueError

1. 充足度(总在场)
   sleep_suff_score = _sleep_shaped(total, TARGET_SLEEP_MIN=480)

2. 恢复性(分期可用才在场)
   stage_usable = sleep_stage_valid 且 deep≥0 且 rem≥0 且 (deep+rem)≤total
   若可用:
     deep_score  = _sleep_shaped(deep, DEEP_TARGET_MIN=100)
     rem_score   = _sleep_shaped(rem,  REM_TARGET_MIN=110)
     restorative = 0.5·deep_score + 0.5·rem_score      # W_REST_DEEP=0.5
   否则:恢复性缺席, sleep_stage_fallback=True, 告警 SLEEP_STAGE_FALLBACK

3. 连续性(有 WASO 才在场)
   若 waso_min 有效且 ≥0:
     continuity = clamp(100·(1 − waso/total), 0, 100)

4. 归一加权
   有效权重_i = 名义权重_i / Σ(在场分量名义权重)
   raw = Σ(有效权重_i · 子分_i)

5. score = clamp(round_int(raw), 0, 100); level 由 _sleep_level
   low_confidence = total < 180 → 告警 SLEEP_LOW_CONFIDENCE
```

**整形函数 `_sleep_shaped(value, target)`** —— 让"拿满分变难"的核心:
```
r = value / target
r < 1 :  90 · r^1.3                       # 凸惩罚:不足目标掉得比线性快
r ≥ 1 :  90 + 10·(1 − e^(−3·(r−1)))        # 饱和:超目标才缓慢逼近 100
value≤0 或 target≤0 → 0;结果钳制 [0,100]
```
即:达到目标只得 **90**,逼近 100 需显著超出目标(近乎不可达);低于目标惩罚更陡。参数 `SLEEP_SHAPE_ANCHOR/P/K = 90/1.3/3` 可调。

> 缺分量"重分配权重"而非"静默丢弃",这也保证了分期缺失不会反而抬高总分。

---

## 7. 计算流程:Recovery

**HRV + RHR + Sleep 三者加权,缺哪个降级到对应回退公式。** `compute_recovery(inp, sleep_score)` 的 `sleep_score` 由睡眠分数传入。

```
hrv_valid / rhr_valid 解析(显式标志 + 数值区间双重确认)
  无效则各自告警 HRV_INVALID_FALLBACK / RHR_INVALID_FALLBACK

hrv_score(有效且有值): _compute_hrv_score —— 对数映射
   h ≤ HRV_LOW[桶] → 0;h ≥ HRV_HIGH[桶] → 100
   否则 100 · ln(h/HRV_LOW) / ln(HRV_HIGH/HRV_LOW)

rhr_score(有效且有值): _compute_rhr_score —— 线性映射
   r ≤ RHR_GOOD[桶] → 100;r ≥ RHR_POOR[桶] → 0
   否则 100 · (RHR_POOR − r) / (RHR_POOR − RHR_GOOD)

组合:
   HRV 与 RHR 都在 → 0.45·hrv + 0.25·rhr + 0.30·sleep      fallback_level=0
   仅 RHR        → 0.40·rhr + 0.60·sleep, level=1, 告警 RECOVERY_PARTIAL_FALLBACK
   仅 HRV        → 0.60·hrv + 0.40·sleep, level=1, 告警 RECOVERY_PARTIAL_FALLBACK
   都无          → sleep,               level=2, 告警 RECOVERY_FULL_FALLBACK

score = clamp(round_int(raw), 0, 100); level 由 _recovery_level
```

> HRV 用**对数**映射(HRV 分布右偏,对数更线性);RHR 用线性映射。两者方向相反:HRV 越高越好,RHR 越低越好。

---

## 8. 计算流程:Strain

**白天逐分钟心率 → 心率储备(HRR)→ 指数 TRIMP 负荷 → 全天累加 → 饱和映射到 0–21。**

```
1. HRmax_est = 208 − 0.7·age(年龄缺失 → 固定 190)

2. RHR 解析:
   rhr_valid → 用 rhr_sleep_bpm
   否则      → 0.5·(RHR_GOOD[桶]+RHR_POOR[桶]) 人群中点, fallback_rhr=True, 告警 STRAIN_RHR_FALLBACK

3. 逐分钟(遍历 day_hr_minute_bpm,带下标):
   a) 有效性:HR 不在 [35,220] → 跳过(不计入 valid_minutes)
   b) valid_minutes += 1
   c) HRR(Karvonen,钳制[0,1]):
        HRmax≤RHR 或 HR≤RHR → 0;HR≥HRmax → 1
        否则 (HR − RHR)/(HRmax − RHR)
   d) 分区(诊断,不参与打分):z1 [0.30,0.50) / z2 [0.50,0.60) / z3 [0.60,0.70)
                              / z4 [0.70,0.80) / z5 [0.80,1.00]
   e) 步数门控:若 day_step_minute_count[该下标] 步频 ∈ [40,250] → step_active=True
   f) 每分钟负荷:
        HRR < 0.30 且 非 step_active → load = 0       # 久坐拒绝
        否则 → load = HRR · 0.64 · e^(1.92·HRR)         # TRIMP_A, TRIMP_B
      （步数门控的意义:HR 低于 0.30 但确在走路的分钟,放行其真实低负荷,而非归零）
   g) trimp_day += load

4. 若 valid_minutes == 0 → score 0.0, valid=False, low_confidence=True, 告警 NO_VALID_DAY_HR

5. strain_raw = 21·(1 − e^(−trimp_day/100))      # STRAIN_TAU=100
   score = round_1(clamp(strain_raw, 0, 21)); level 由 _strain_level
   low_confidence = day_hr_coverage < 0.5 → 告警 STRAIN_LOW_CONFIDENCE
```

> 标度感:饱和映射使分数前段灵敏、越高越难涨(trimp=100 → ≈13.3;trimp=200 → ≈18.2;21 近乎不可达),对齐 0–21 标度。`zone_minutes` 只供诊断,**不进分数**——分数完全由 `trimp_day` 决定。

---

## 9. 顶层编排

**`compute_daily_score(inp)`** 校验顺序与行为:
```
total_sleep_min ≤ 0           → INVALID_TOTAL_SLEEP(不产出任何分数)
day_hr_minute_bpm 为 None     → INVALID_REQUIRED_FIELD
day_hr_minute_bpm 非序列      → INVALID_DAY_HR_ARRAY
否则:
  sleep = compute_sleep_performance(inp)
  recovery = compute_recovery(inp, sleep.score)   # recovery 依赖 sleep 分数
  strain = compute_strain(inp)
  任一抛异常 → 捕获并记日志, 返回 INVALID_REQUIRED_FIELD
  合并三者告警(去重 + 按码排序), status=SUCCESS
```

**`compute_daily_score_from_dict(data)`**:`from_dict` 出错时映射为状态码而非抛异常——`ValueError` 含 `day_hr_minute_bpm` → `INVALID_DAY_HR_ARRAY`,其余 `ValueError`/`TypeError` → `INVALID_REQUIRED_FIELD`。成功则返回 `compute_daily_score(...).to_dict()`。

> 依赖关系:**Recovery 需要 Sleep 的分数**(占其 30%~60% 权重)。Strain 与 Sleep 相互独立。

---

## 10. 常量速查

**有效性区间**:HRV `[5,250]` ms;RHR `[30,100]` bpm;白天 HR `[35,220]` bpm。

**Sleep**:
| 常量 | 值 | 含义 |
|---|---|---|
| `TARGET_SLEEP_MIN` | 480 | 充足度目标(8h) |
| `DEEP_TARGET_MIN` | 100 | 深睡目标(分钟) |
| `REM_TARGET_MIN` | 110 | REM 目标(分钟) |
| `W_SLEEP_SUFF / REST / CONT` | 0.45 / 0.25 / 0.10 | 三分量名义权重 |
| `W_REST_DEEP` | 0.5 | 恢复性中深睡占比 |
| `SLEEP_SHAPE_ANCHOR / P / K` | 90 / 1.3 / 3 | 整形曲线参数 |

**Recovery**:`W_REC_HRV/RHR/SLEEP = 0.45/0.25/0.30`;`HRV_LOW/HIGH`、`RHR_GOOD/POOR`(见 §5)。

**Strain**:
| 常量 | 值 | 含义 |
|---|---|---|
| `LOW_INTENSITY_HRR_THRESHOLD` | 0.30 | 计入负荷的 HRR 下界(久坐拒绝) |
| `TRIMP_A / TRIMP_B` | 0.64 / 1.92 | Banister TRIMP 系数 |
| `STRAIN_TAU` | 100 | 饱和映射时间常数 |
| `WALK_CADENCE_MIN/MAX_SPM` | 40 / 250 | 判定"走路分钟"的步频区间 |

**舍入**:Sleep/Recovery 用 `_round_int`(整数,.5 进位);Strain 用 `_round_1`(一位小数,.05 进位)。均跨平台确定性。

---

## 11. 关键不变量与注意事项

- **无基线**:所有分数仅用当日输入 + 人群级参考(年龄桶),不依赖历史。
- **确定性**:整数 / 一位小数的舍入采用 `floor(x+0.5)` 形式,跨运行时一致。
- **缺数据降级**:Sleep 缺分量→权重归一;Recovery 缺 HRV/RHR→降级公式 + `fallback_level`;Strain 缺 RHR→人群中点。均产出分数并打告警,不静默失败。
- **`zone_minutes` 是诊断字段**,不参与 Strain 打分(分数只由 `trimp_day` 决定)。
- **步数门控只补不减**:`day_step_minute_count` 仅用于"放行"低 HR 的走路分钟,不会降低任何分钟的负荷,因此骑行等无位移高心率运动不受影响,也不会双计走路(位移已经过心率进入)。
- **显式有效标志**:`hrv_valid`/`rhr_valid` 为 `True` 时仅在数值本身落在有效区间内才确认有效——脏数据不会被一个 `True` 强行放行。
- **顶层不抛异常**:`compute_daily_score` / `..._from_dict` 把校验与计算异常编码为 `status_code`,适合服务端直接调用。
- **可调参数**:目标值(480/100/110)、权重、整形参数、TRIMP 系数、`τ`、步频区间等均为模块级常量,便于按真实数据标定。

---

### 已知局限(便于后续迭代)
- Sleep 的深睡/REM 目标与充足度目标为**固定值,未按年龄调整**(深睡量随年龄下降,固定目标会低估老年用户)。
- 连续性依赖 `waso_min` 输入,缺失则该维度不参与。
- 未纳入"个体化睡眠需求 / 睡眠债 / 规律性"等需历史基线的维度(与无基线设定冲突,属产品取舍)。