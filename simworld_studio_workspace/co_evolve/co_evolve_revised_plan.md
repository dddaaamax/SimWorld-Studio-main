# Co-Evolution Module 设计方案 (Revised)

> 参考: Reflexion (2303.11366), SSP (2510.18821), SPICE (2510.24684), RLVE (2511.07317), EnvScaler (2601.05808)

---

## 一、核心设计原则（相比初版的关键变化）

| 维度 | 初版 | Revised |
|------|------|---------|
| Coding Agent 优化信号 | ELO（零和，概念错误） | Calibration Score（ZPD 命中率） |
| Grounding 机制 | 语言描述 failure pattern | 失败轨迹坐标 + 语言描述双通道 |
| Scene Pool 规模 | max_size=50 | max_size=200，按难度分层 |
| Nav Agent memory | 无 context 管理 | Reflexion-style sliding window + distill |
| 场景质量评估 | 纯几何验证（navmesh） | 几何验证 + 功能性验证（训练价值评分） |
| RL 接口 | 复杂抽象层（BasePolicy 等） | 轻量数据接口，不做架构侵入 |

---

## 二、整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                     CoEvolutionRunner                         │
│                                                              │
│  for gen in range(N):                                        │
│                                                              │
│    ┌──────────────────────────────────────┐                  │
│    │ Phase 1: Scene Generation            │                  │
│    │                                      │                  │
│    │  curriculum.get_constraints()        │                  │
│    │  feedback.compose_for_coding_agent(  │                  │
│    │    prev_metrics,                     │                  │
│    │    failure_trajectories,  ← NEW      │                  │
│    │    nav_strategies                    │                  │
│    │  )                                   │                  │
│    │  CodingAgent.generate_scene(...)     │ ← Claude CLI     │
│    │  SceneValidator.validate()           │   (subprocess)   │
│    │    ├─ geometric: navmesh             │                  │
│    │    └─ functional: learnability score │ ← NEW            │
│    │  ScenePool.add(scene)                │                  │
│    └──────────────┬───────────────────────┘                  │
│                   ▼                                          │
│    ┌──────────────────────────────────────┐                  │
│    │ Phase 2: Navigation Evaluation       │                  │
│    │                                      │                  │
│    │  fresh_episodes + pool_replay        │                  │
│    │  batch_runner.run_wave(...)          │ ← 复用现有        │
│    │  memory.reflect() per episode        │                  │
│    │  memory.maybe_distill()              │ ← NEW            │
│    │  trajectory_store.save(...)          │ ← NEW            │
│    └──────────────┬───────────────────────┘                  │
│                   ▼                                          │
│    ┌──────────────────────────────────────┐                  │
│    │ Phase 3: Signal Update               │                  │
│    │                                      │                  │
│    │  calibration.update(sr, target_zpd)  │ ← NEW            │
│    │  curriculum.update(sr_ema)           │                  │
│    │  scene_pool.update_scores(results)   │                  │
│    │  checkpoint.save()                   │                  │
│    └──────────────────────────────────────┘                  │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、模块文件结构

```
simworld_studio_workspace/co_evolve/
├── __init__.py               # 公开 API: CoEvolutionRunner
├── __main__.py               # CLI 入口
├── config.py                 # CoEvolveConfig dataclass
├── loop.py                   # 主循环编排器
├── coding_agent.py           # Claude CLI 场景生成
├── scene_pool.py             # 场景经验池（含 PLR-compatible 接口）
├── scene_validator.py        # 几何验证 + 功能性验证
├── curriculum.py             # ZPD 课程控制器
├── feedback.py               # 双向 verbalized feedback
├── calibration.py            # Coding Agent 优化信号（替代 ELO）★ NEW
├── trajectory_store.py       # 失败轨迹存储（grounding 信号源）★ NEW
├── memory_manager.py         # Nav memory context 管理（Reflexion-style）★ NEW
└── checkpoint.py             # 全状态序列化 / 断点恢复
```

---

## 四、核心组件详细设计

### 4.1 `calibration.py` — Coding Agent 优化信号 ★

**动机**：ELO 假设零和博弈，但两个 agent 的共同目标是找到 ZPD sweet spot，不是零和。参考 SSP 的 proposer reward 设计（proposer 生成有 ground-truth 且难度递增的任务），转译到我们的场景：Coding Agent 的奖励是"场景是否让 nav agent 落在学习区间内"。

```python
@dataclass
class CalibrationRecord:
    scene_id: str
    generation: int
    target_sr_range: Tuple[float, float]   # curriculum 期望 SR 区间
    actual_sr: float                        # nav agent 实际 SR
    score: float                            # CalibrationScore

class CalibrationTracker:
    """
    Coding Agent 的唯一优化目标：
    精准生成落在 ZPD 区间内的场景（target_sr_range = (0.25, 0.75)）
    
    score = 1.0  → 完美命中 ZPD
    score ∈ (0,1) → 偏离 ZPD 但有部分价值
    score = 0.0  → 完全无效（SR=0 或 SR=1）
    """
    def compute(self, actual_sr: float, target_range: Tuple[float, float]) -> float:
        lo, hi = target_range
        if lo <= actual_sr <= hi:
            return 1.0
        elif actual_sr < lo:
            # 太难：线性衰减到 0
            return actual_sr / lo
        else:
            # 太简单：线性衰减到 0
            return (1.0 - actual_sr) / (1.0 - hi)
    
    def update(self, scene_id: str, actual_sr: float, target_range: Tuple[float, float]):
        score = self.compute(actual_sr, target_range)
        self.history.append(CalibrationRecord(...))
        self.ema_score = 0.7 * self.ema_score + 0.3 * score
    
    @property
    def summary(self) -> str:
        """给 feedback.py 使用：告诉 Coding Agent 它的命中率趋势"""
        recent = self.history[-5:]
        avg = sum(r.score for r in recent) / len(recent)
        if avg > 0.7:
            return f"Scene difficulty calibration is GOOD (avg={avg:.2f}). Maintain current approach."
        elif avg < 0.4:
            low_sr_count = sum(1 for r in recent if r.actual_sr < self.target_range[0])
            if low_sr_count > 2:
                return f"Scenes too hard (SR consistently below target). Reduce obstacle density or path length."
            else:
                return f"Scenes too easy (SR consistently above target). Increase challenge."
        return f"Calibration score moderate ({avg:.2f}). Refine based on failure patterns below."
```

**WandB logging**：`calibration_score` 曲线 + `calibration_ema` 是系统健康度的核心指标。两条曲线都应稳定上升，否则说明 Coding Agent 没有真正改进。

---

### 4.2 `trajectory_store.py` — 失败轨迹存储 ★

**动机**：SPICE 的核心发现是 ungrounded self-play 收益有限，grounding 必须是"物理连接"而不只是语言描述。在我们的系统里，grounding = 把 nav agent 真实失败坐标注入 Coding Agent prompt。

```python
@dataclass
class StepRecord:
    step_idx: int
    position: Tuple[float, float, float]   # UE world coords
    action: str                             # MOVE_FORWARD / TURN_LEFT 等
    result: str                             # success / collision / timeout
    obs_summary: str                        # LLM 对当前观测的简短描述

@dataclass
class FailureTrajectory:
    episode_id: str
    scene_id: str
    start_pos: Tuple[float, float, float]
    goal_pos: Tuple[float, float, float]
    failure_mode: str       # "stuck_in_narrow_gap" / "oscillation" / "timeout_open_area"
    stuck_position: Optional[Tuple[float, float, float]]   # 卡住的具体坐标
    steps: List[StepRecord]
    nav_agent_reflection: str   # HierarchicalMemory 产生的 reflection 文本

class TrajectoryStore:
    def save(self, episode_result: EpisodeResult): ...
    
    def get_failure_summary(self, scene_id: str, max_trajectories: int = 3) -> str:
        """
        生成给 Coding Agent 的 grounding 信号，格式：
        
        FAILURE TRAJECTORY 1 (episode ep_042):
          Start: (1240, 850, 0) → Goal: (4580, 3200, 0)
          Stuck at: (2100, 1050, 0) — narrow gap between Building_03 and Tree_4
          Last 5 actions: MOVE_FORWARD, TURN_LEFT, MOVE_FORWARD, TURN_LEFT, MOVE_FORWARD
          Nav agent reflection: "Passage width ~150cm insufficient. Need >300cm corridor."
        
        →  Include a navigable corridor of at least 300cm width near coordinates (2000-2200, 900-1100).
           Do NOT place dense obstacles in that quadrant.
        """
        failures = self._get_recent_failures(scene_id)
        # 转换世界坐标为相对位置描述，让 Coding Agent 可以在 prompt 里理解
        return self._format_with_spatial_hints(failures[:max_trajectories])
```

**关键设计**：不只是"narrative 描述"，而是把 stuck_position 以 UE 坐标形式传给 Coding Agent，Coding Agent 被 prompt 要求在该区域避免/调整布局。这是真正的 spatial grounding。

---

### 4.3 `feedback.py` — 双向 Verbalized Feedback（升级版）

给 Coding Agent 的 prompt 结构变为三层：

```markdown
## Navigation Agent Performance Report (Generation 7)

### [LAYER 1: CALIBRATION SIGNAL]
Calibration Score: 0.45/1.0 (EMA over last 5 scenes: 0.52)
Interpretation: Scenes are consistently too hard. 
Nav agent SR = 15% vs target ZPD = (25%, 75%).

### [LAYER 2: SPATIAL GROUNDING]  ← NEW，来自 TrajectoryStore
Failure Trajectory 1 (ep_042):
  Stuck at UE coords ≈ (2100, 1050): narrow gap between Building_03 and Tree_4
  Nav agent note: "Corridor ~150cm, need ≥300cm to navigate"
  → Avoid dense building clusters in the 2000-2500 range of X axis.

Failure Trajectory 2 (ep_044):
  Oscillation loop at ≈ (3800, 2200): prop cluster blocking all directions
  Nav agent note: "No viable turn found after 8 attempts"
  → Reduce prop density in center quadrant.

### [LAYER 3: VERBAL PATTERN ANALYSIS]
Agent Strengths: open areas, clear sightlines, bearing < ±30°
Agent Weaknesses: narrow passages, dense prop clusters, detour ratio > 1.5

### [CURRICULUM INSTRUCTION]
Calibration target: SR between 25%-75%.
Generate scene with MODERATE density. Ensure all corridors ≥300cm wide.
No prop clusters larger than 3 items within 500cm radius.
Difficulty target: 0.40.
```

**给 Nav Agent 的 feedback**：直接复用 `HierarchicalMemory.end_episode()` + `reflect()`，不变。
Nav Agent 端的唯一新增是 `memory_manager.py` 的 context 管理（见 4.4）。

---

### 4.4 `memory_manager.py` — Nav Memory Context 管理 ★

**动机**：Reflexion 论文明确指出随 trial 积累 memory buffer 会超出 context window，需要截断或蒸馏。计划里跑 20 gen × 6 episodes，不处理这个问题会在第 8-10 代开始 token overflow。

```python
class MemoryManager:
    """
    三种模式（通过 config 切换）:
    
    "persist"  → 跨 gen 保留所有 memory（适合 gen 少、episodes 少的调试阶段）
    "window"   → sliding window，只保留最近 K 条 reflection（Reflexion 原版策略）
    "distill"  → 每 N gen 做一次 LLM 蒸馏，提取 core principles → 新 gen 用 distilled memory 初始化
    """
    mode: str = "distill"
    window_size: int = 10          # window 模式：保留最近 10 条 reflection
    distill_every_n_gen: int = 5   # distill 模式：每 5 gen 蒸馏一次
    
    def maybe_distill(self, memory: HierarchicalMemory, gen: int) -> None:
        if self.mode != "distill":
            return
        if gen % self.distill_every_n_gen != 0:
            return
        
        raw_reflections = memory.get_all_reflections()
        distilled = self._llm_distill(raw_reflections)
        # distilled 格式：
        # CORE NAVIGATION PRINCIPLES (distilled from 30 episodes):
        # 1. When bearing < ±30°, commit to MOVE_FORWARD for ≥3 steps
        # 2. If stuck >5 turns in same position, try opposite direction
        # 3. ...
        memory.reset_to_distilled(distilled)
    
    def _llm_distill(self, reflections: List[str]) -> str:
        # 调用 nav LLM，prompt: 
        # "You are a navigation agent. Extract 5-10 universal principles 
        #  from these episode reflections that will help in future episodes."
        ...
```

---

### 4.5 `scene_validator.py` — 几何 + 功能性验证（升级版）

初版只做几何验证（navmesh 可达性）。EnvScaler 的经验：还需要**功能性验证**——场景在几何上合法，但训练价值可能为零（比如路径全是直线，agent 不需要任何绕行策略）。

```python
@dataclass
class SceneValidationResult:
    geometric_valid: bool
    functional_score: float        # 0~1，训练价值评分
    rejection_reason: Optional[str]

class SceneValidator:
    def validate(self) -> SceneValidationResult:
        # --- 几何验证（不变）---
        if not self._check_navmesh_reachability():
            return SceneValidationResult(False, 0.0, "insufficient reachable pairs")
        
        # --- 功能性验证（新增）---
        functional_score = self._compute_functional_score()
        if functional_score < self.config.min_functional_score:
            return SceneValidationResult(True, functional_score, 
                                         "low training value: paths too trivial")
        return SceneValidationResult(True, functional_score, None)
    
    def _compute_functional_score(self) -> float:
        """
        评估场景的训练价值，基于以下指标：
        
        1. path_tortuosity: 采样路径的 detour ratio 均值（>1.3 = 有绕行挑战）
        2. decision_point_density: 路径中需要选择方向的节点数 / 路径长度
        3. obstacle_variety: 不同类型障碍物的种类数（多样性 > 单调）
        4. dead_end_ratio: 死胡同占可达节点的比例（太高 = 惩罚性场景，太低 = 无挑战）
        
        加权平均 → [0, 1]
        """
        tortuosity = self._sample_path_tortuosity(n=20)
        decision_density = self._count_decision_points()
        variety = self._count_obstacle_types()
        dead_end = self._estimate_dead_end_ratio()
        
        score = (
            0.4 * min(tortuosity / 2.0, 1.0) +      # 期望 detour ratio ≈ 1.5-2.0
            0.3 * min(decision_density / 0.1, 1.0) +  # 期望每 100cm 有 0.1 个决策点
            0.2 * min(variety / 4.0, 1.0) +            # 期望 ≥4 种障碍类型
            0.1 * (1.0 - abs(dead_end - 0.15) / 0.15) # 期望死胡同率约 15%
        )
        return score
```

---

### 4.6 `scene_pool.py` — 经验池（升级版）

**规模扩大**：`max_size` 从 50 → 200。RLVE 的经验是 environment diversity 是独立的 scaling 维度，pool 太小会成为瓶颈。

**新增 PLR-compatible score 接口**（轻量，不做完整 PLR 实现，但数据结构兼容）：

```python
@dataclass
class SceneRecord:
    scene_id: str
    generation: int
    difficulty_target: float
    actual_difficulty: float
    scene_graph: List[Dict]
    pre_generated_episodes: List[NavigationEpisode]
    fingerprint: np.ndarray            # 24-dim 几何特征
    functional_score: float            # 来自 SceneValidator
    calibration_score: float           # 来自 CalibrationTracker
    sr_history: List[float]
    
    # PLR-compatible 字段（未来 RL 接入时直接使用，现在用 SR-based proxy）
    level_score: float = 0.5           # 当前用 1 - sr 作为 proxy（失败率高 = 更有价值）
    staleness: int = 0                 # 距上次被采样的 generation 数
```

采样策略从"基于 SR"升级为同时考虑 calibration_score 和 staleness：

```python
def sample_for_replay(self, n: int, target_difficulty: float) -> List[SceneRecord]:
    candidates = self._filter_by_difficulty_band(target_difficulty, bandwidth=0.2)
    
    # 综合得分：失败率高（有挑战）+ calibration 好（有价值）+ 不太陈旧
    def replay_priority(r: SceneRecord) -> float:
        failure_signal = 1.0 - (r.sr_history[-1] if r.sr_history else 0.5)
        calibration_signal = r.calibration_score
        freshness = 1.0 / (1.0 + r.staleness * 0.1)
        return 0.5 * failure_signal + 0.3 * calibration_signal + 0.2 * freshness
    
    candidates.sort(key=replay_priority, reverse=True)
    return candidates[:n]
```

---

### 4.7 `curriculum.py` — 课程控制器（不变，小优化）

核心 ZPD 逻辑不变（EMA + mastery/struggle threshold + cap_per_5gen）。新增一个信号源：Calibration Score 连续过低时，**不提难度**，即使 SR 超过 mastery_threshold。

```python
def update(self, metrics: GenerationMetrics, calibration_ema: float) -> CurriculumDecision:
    ema_sr = self._exponential_moving_average(metrics.sr)
    
    # 正常 ZPD 逻辑
    if ema_sr > self.mastery_threshold:
        delta = +self.difficulty_step
    elif ema_sr < self.struggle_threshold:
        delta = -self.difficulty_step
    else:
        delta = 0.0
    
    # 新增：Calibration 过低时锁定难度（说明 Coding Agent 还没学会精准生成）
    if calibration_ema < 0.4 and delta > 0:
        delta = 0.0
        reason = "Holding difficulty: Coding Agent calibration too low to trust SR signal"
    
    hard_cap = min(1.0, self.initial + (gen // 5) * self.cap_per_5gen)
    self.target = clamp(self.target + delta, 0.05, hard_cap)
```

---

### 4.8 `config.py` — 配置（完整版）

```python
@dataclass
class CoEvolveConfig:
    # 循环
    generations: int = 30
    episodes_per_scene: int = 6
    wave_size: int = 3                      # 并发 ghost agent 数

    # Curriculum
    initial_difficulty: float = 0.25
    difficulty_step: float = 0.05
    mastery_threshold: float = 0.70
    struggle_threshold: float = 0.25
    zpd_target_range: Tuple[float, float] = (0.25, 0.75)   # Calibration Score 目标区间
    rolling_window: int = 3
    difficulty_cap_per_5gen: float = 0.10

    # Scene Pool
    pool_max_size: int = 200               # 从 50 扩大到 200（RLVE 经验）
    replay_fraction: float = 0.25
    diversity_min_distance: float = 0.3
    min_functional_score: float = 0.3      # 场景最低训练价值阈值

    # Validation
    validation_retries: int = 3
    fallback_to_pool: bool = True
    min_navigable_positions: int = 10
    min_reachable_pairs: int = 3
    scene_candidates: int = 2              # Best-of-N 生成（取功能分最高的）

    # Memory
    memory_mode: str = "distill"           # "persist" | "window" | "distill"
    memory_window_size: int = 10
    memory_distill_every_n: int = 5

    # Trajectory Store（grounding 信号）
    max_failure_trajectories_per_gen: int = 3   # 最多取 3 条失败轨迹注入 prompt

    # Agent
    coding_model: str = "claude-sonnet-4-20250514"
    nav_model: str = "claude"
    nav_memory: str = "hierarchical"
    max_steps: int = 40
    vision_depth: int = 3
```

---

## 五、关于 RL 接口

RL 完整接入（BasePolicy、TrajectoryBuffer、PPO training loop）需要大量架构改造，收益不确定，**不在当前版本做**。

但保留两个**轻量数据接口**，未来如果要做 RL，这两处改动最小：

**接口 1：TrajectoryStore 已经存了每步的 (position, action, result)**。如果要做 RL，只需要加 reward 计算逻辑和 replay buffer 封装，数据结构本身不用改。

**接口 2：ScenePool 的 `level_score` 字段**。当前用 `1 - sr` 作为 proxy，未来 RL 直接替换为 TD-error。采样接口 `sample_for_replay()` 不需要改。

除此之外，不做任何 RL 相关的抽象层。

---

## 六、稳定性 Tricks 汇总

| Trick | 作用 | 来源 |
|-------|------|------|
| Calibration Score | Coding Agent 精准优化信号 | SSP proposer reward 思路 |
| Spatial Grounding | 失败坐标注入 prompt，防 ungrounded drift | SPICE grounding 结论 |
| Functional Validation | 过滤训练价值低的合法场景 | EnvScaler quality eval |
| Memory Distillation | 防 context overflow，保留 core principles | Reflexion sliding window |
| EMA + Calibration Guard | 双重保护，避免 SR 噪声驱动难度跳变 | 原版 ZPD + 新增 |
| Scene Pool 200 + 分层采样 | Environment diversity 是独立 scaling 维度 | RLVE 结论 |
| Best-of-N (candidates=2) | 提高场景质量基线 | 一等功能，不再是 optional |
| Fingerprint 去重 | 避免 Coding Agent 反复生成相似场景 | 原版保留 |
| Pre-generated Episodes | Replay 时无需重建 UE 场景 | 原版保留（我们独有的优势） |
| Held-out Eval Set | 区分"训练集 SR"和"真实能力提升" | 原版缺失，新增 |

---

## 七、Held-out Evaluation Set（初版缺失，新增）

**问题**：curriculum 根据新生成场景上的 SR 调整难度，但这是"训练集"上的 SR，无法区分"agent 真的变强了"和"场景变容易了"。

**方案**：在系统启动时生成 15 个固定场景（每个难度区间 3 个），**冻结，永不更新**。每 5 个 generation 在这些场景上跑一次评估，得到 `held_out_sr`。

```python
class EvalHarness:
    def __init__(self, fixed_scenes: List[SceneRecord]):
        self.eval_scenes = fixed_scenes   # 初始化后不再修改
    
    def evaluate(self, nav_llm, nav_memory) -> Dict[str, float]:
        results = {}
        for difficulty_band in ["easy", "medium", "hard"]:
            scenes = [s for s in self.eval_scenes if s.matches_band(difficulty_band)]
            sr = run_wave_on_scenes(scenes, nav_llm, nav_memory)
            results[f"held_out_sr_{difficulty_band}"] = sr
        return results
```

WandB 同时记录 `curriculum_sr`（训练信号）和 `held_out_sr_*`（真实能力曲线）。两者分离是判断系统是否真正在进步的核心依据。

---

## 八、实现顺序

**Phase 1 — Skeleton（最小闭环）**
1. `config.py`
2. `coding_agent.py` — Claude CLI subprocess
3. `scene_validator.py` — navmesh 几何验证（functional score 先 stub）
4. `trajectory_store.py` — 数据结构 + save 逻辑
5. `loop.py` — generate → validate → run_wave → print SR

**Phase 2 — Signals（核心优化信号）**
6. `calibration.py` — Calibration Score + summary
7. `curriculum.py` — ZPD + Calibration Guard
8. `feedback.py` — 三层 prompt（Calibration + Spatial Grounding + Verbal）
9. 将 trajectory_store 的 grounding 信号接入 feedback

**Phase 3 — Stability（稳定性组件）**
10. `scene_validator.py` — 补充 functional score
11. `scene_pool.py` — 200 上限 + 新采样策略 + PLR-compatible level_score
12. `memory_manager.py` — distill 模式
13. `checkpoint.py`
14. Best-of-N candidates 逻辑

**Phase 4 — Evaluation + Polish**
15. `EvalHarness` — held-out eval set
16. WandB：calibration_score 曲线、held_out_sr 曲线、curriculum_sr 曲线、pool diversity 指标
17. `__main__.py` — CLI + `--resume`
18. 集成测试（mock UE，3 gen smoke test）

---

## 九、关键指标体系

| 指标 | 含义 | 健康状态 |
|------|------|----------|
| `curriculum_sr` | 当前代 nav agent 在新场景上的 SR | 在 ZPD 区间内波动 |
| `held_out_sr_easy/medium/hard` | 真实能力曲线 | 单调上升 |
| `calibration_score_ema` | Coding Agent 课程设计质量 | 趋势上升，稳定在 0.6+ |
| `pool_diversity` | Pool 场景间平均指纹距离 | 不低于 diversity_min_distance |
| `memory_distill_count` | 蒸馏触发次数 | 每 5 gen 一次 = 正常 |
| `functional_score_avg` | 场景训练价值均值 | 不低于 0.4 |
