# 产品碳足迹核算与证据 API

基于 **Python + FastAPI + SQLite + Decimal** 的产品碳足迹（PCF）核算服务。
客户端通过 HTTP 登记过程批次（产量、单位、排放因子版本）、物料投入与多产品产出
构成的有向图，并选择 **质量 / 能量 / 经济价值** 分摊法；服务按拓扑顺序计算
每个产品的单位 CO₂e、贡献链（contribution chain），并保存**不可变证据快照**。

## 功能

- **有向过程图**：外部物料 → 过程 → 多产品（联产品）→ 下游过程，Kahn 拓扑排序计算
- **拒绝非法输入**（一次性聚合返回，错误项按路径稳定排序）：
  - `CYCLE_DETECTED` 过程图成环（Tarjan SCC 找出具体环路径）
  - `NEGATIVE_AMOUNT` / `ZERO_AMOUNT` 负量与零量
  - `MISSING_FACTOR` 指定因子版本中缺少因子
  - `FACTOR_UNIT_MISMATCH` / `UNIT_MISMATCH` 因子或投入/产出单位不一致
  - `ALLOCATION_DENOMINATOR_ZERO` 分摊权重之和为零
  - `UNKNOWN_REFERENCE` / `FLOW_EXCEEDS_OUTPUT` / `SELF_REFERENCE` 引用与流量平衡问题
- **Decimal 精确运算**：50 位精度上下文，结果保留 12 位小数（`ROUND_HALF_UP`），
  JSON 中所有数值均为**字符串**传输，杜绝浮点误差
- **贡献链**：每个产品的足迹可追溯到每一条外部物料投入 / 直接排放源，含数量×因子明细、
  传递比例、因子版本与因子值（证据随快照固化）
- **幂等**：以「规范化输入摘要 + 因子版本」的 SHA-256 作为幂等键，重复提交返回同一快照；
  节点重排不产生新快照；`client_request_id` 冲突返回 409
- **不可变快照**：因子版本**只增不改**——重提同名版本时，因子 ref 集合、
  名称、单位与 Decimal 数值全部一致才幂等返回 `200`，任一差异返回 `409`
  （并发同名提交不会部分写入或互相覆盖），改值必须用新版本名。快照同样永不
  修改；用新因子版本重算会生成**新快照**并通过 `source_snapshot` 关联，返回
  逐产品差异（新旧单位足迹、Δ、Δ%）与因子变更清单。差异的新旧因子值全部取自
  两个快照**封存的 `factors_used`**，即使旧版本曾被覆盖也与 `product_diffs`
  口径一致
- 内置两个种子因子版本：`v2023-01`（基准）、`v2024-01`（因子下降，便于演示重算差异）

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 http://localhost:8000 ，SQLite 数据保存在命名卷 `carbon-data`（容器内 `/data`）。

- Swagger 交互文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health

停止并保留数据：`docker compose stop`；清空数据：`docker compose down -v`。

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CARBON_DB_PATH=data/carbon.db uvicorn app.main:app --reload
```

运行测试：

```bash
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查 |
| GET  | `/api/factor-versions` | 列出因子版本及全部因子 |
| POST | `/api/factor-versions` | 登记不可变因子版本（新版本 201；内容完全一致的重提 200；同名但内容变化 409） |
| POST | `/api/assessments` | 提交核算（首次 201，幂等重放 200） |
| GET  | `/api/snapshots?limit=&offset=` | 快照列表 |
| GET  | `/api/snapshots/{id}` | 快照完整证据（输入、结果、因子副本） |
| GET  | `/api/snapshots/{id}/products/{product_id}` | 单产品足迹与贡献链 |
| POST | `/api/snapshots/{id}/recalculate` | 用新因子版本生成新快照并返回逐产品差异 |

> **不可变性**：没有任何修改/删除快照的接口，也没有任何覆盖因子版本的接口。
> `POST /api/factor-versions` 只接受**新版本名**；对已封存版本重提时，仅当
> 因子 ref 集合、各因子名称、单位、Decimal 数值（按数值比较，`1.20` 与 `1.2`
> 等价）**全部相同**才幂等返回 `200`，否则返回 `409 FACTOR_VERSION_CONFLICT`
> 并在 `details.issues` 中逐项列出差异（`FACTOR_ADDED` / `FACTOR_REMOVED` /
> `FACTOR_NAME_CONFLICT` / `FACTOR_UNIT_CONFLICT` / `FACTOR_VALUE_CONFLICT`）。
> 因子版本与全部因子在单个 SQLite 事务中写入，并发同名提交至多一方成功，
> 其余按内容返回 `200` 或 `409`，不会部分写入或互相覆盖。要改因子值，请用
> 新的版本名（如 `v2025-01`）。

## 实际请求 / 响应示例

场景：1000 kg 原油经「炼制」过程（天然气 + 电网电力直排）产出汽油 400 / 柴油 500 /
渣油 50 kg（质量分摊）；全部柴油再进入「配送」过程（公路货运）交付。

### 1) 提交核算

```bash
curl -s -X POST http://localhost:8000/api/assessments \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "client_request_id": "refinery-batch-2024-001",
  "factor_version": "v2023-01",
  "allocation_method": "mass",
  "external_materials": [
    {"ref": "crude", "name": "原油", "quantity": "1000", "unit": "kg",
     "factor_ref": "ef.crude_oil"}
  ],
  "processes": [
    {
      "process_id": "refining", "name": "原油炼制",
      "batch_quantity": "950", "unit": "kg",
      "direct_emissions": [
        {"source": "natural_gas", "quantity": "1000", "unit": "MJ",
         "factor_ref": "ef.natural_gas"},
        {"source": "electricity", "quantity": "100", "unit": "kWh",
         "factor_ref": "ef.grid_electricity"}
      ],
      "inputs": [
        {"ref": "crude", "quantity": "1000", "unit": "kg"}
      ],
      "outputs": [
        {"product_id": "gasoline", "name": "汽油", "quantity": "400",
         "unit": "kg", "mass_weight": "400", "energy_weight": "450",
         "economic_weight": "600"},
        {"product_id": "diesel", "name": "柴油", "quantity": "500",
         "unit": "kg", "mass_weight": "500", "energy_weight": "400",
         "economic_weight": "300"},
        {"product_id": "residue", "name": "渣油", "quantity": "50",
         "unit": "kg", "mass_weight": "50", "energy_weight": "50",
         "economic_weight": "10"}
      ]
    },
    {
      "process_id": "distribution", "name": "柴油配送",
      "batch_quantity": "500", "unit": "kg",
      "direct_emissions": [
        {"source": "freight", "quantity": "100", "unit": "tkm",
         "factor_ref": "ef.truck_freight"}
      ],
      "inputs": [
        {"ref": "diesel", "quantity": "500", "unit": "kg"}
      ],
      "outputs": [
        {"product_id": "diesel_delivered", "name": "交付柴油",
         "quantity": "500", "unit": "kg", "mass_weight": "500",
         "energy_weight": "400", "economic_weight": "300"}
      ]
    }
  ]
}
JSON
```

返回 `201 Created`（节选；数值均为字符串）：

```json
{
  "snapshot_id": "snap_404159d90ed529d8",
  "input_hash": "404159d90ed529d8fe4023d45cbf737628eb7c026dd8026c47fb7e72107f59a7",
  "client_request_id": "refinery-batch-2024-001",
  "factor_version": "v2023-01",
  "allocation_method": "mass",
  "source_snapshot": null,
  "created_at": "2026-09-29T16:53:28+00:00",
  "idempotent_replay": false,
  "result": {
    "factor_version": "v2023-01",
    "allocation_method": "mass",
    "topological_order": ["refining", "distribution"],
    "processes": [
      {
        "process_id": "refining",
        "direct_kg_co2e": "126.450000000000",
        "incoming_kg_co2e": "80.000000000000",
        "total_kg_co2e": "206.450000000000",
        "outputs": [
          {"product_id": "gasoline", "allocation_fraction": "0.421052631579",
           "allocated_kg_co2e": "86.926315789474",
           "unit_kg_co2e": "0.217315789474"},
          {"product_id": "diesel", "allocation_fraction": "0.526315789474",
           "allocated_kg_co2e": "108.657894736842",
           "unit_kg_co2e": "0.217315789474"},
          {"product_id": "residue", "allocation_fraction": "0.052631578947",
           "allocated_kg_co2e": "10.865789473684",
           "unit_kg_co2e": "0.217315789474"}
        ]
      },
      {
        "process_id": "distribution",
        "direct_kg_co2e": "9.000000000000",
        "incoming_kg_co2e": "108.657894736842",
        "total_kg_co2e": "117.657894736842",
        "outputs": [
          {"product_id": "diesel_delivered", "allocation_fraction": "1.000000000000",
           "allocated_kg_co2e": "117.657894736842",
           "unit_kg_co2e": "0.235315789474"}
        ]
      }
    ],
    "products": [ "/* 每个产品的总/单位足迹与 contributions 贡献链，见下 */" ],
    "factors_used": [
      {"ref": "ef.crude_oil", "unit": "kg", "kg_co2e_per_unit": "0.080000000000",
       "factor_version": "v2023-01"}
    ]
  }
}
```

核对（质量分摊下各联产品单位足迹相同 = 206.45 ÷ 950）：

- 原油 1000 × 0.08 = 80；天然气 1000 MJ × 0.0561 = 56.1；电力 100 kWh × 0.7035 = 70.35
- 炼制合计 **206.45 kgCO₂e**；单位足迹 **0.217315789474 kgCO₂e/kg**
- 配送：柴油份额 108.6579 + 货运 100 tkm × 0.09 = 9 → **117.6579 / 500 = 0.235315789474**

### 2) 幂等重放

用完全相同的请求体再 POST 一次 → `200 OK`，且 `snapshot_id` 不变：

```json
{"snapshot_id": "snap_404159d90ed529d8", "idempotent_replay": true, "...": "..."}
```

即使把 `processes` 数组里两个过程的顺序颠倒，规范化摘要哈希相同，仍命中同一快照；
结果里的 `topological_order` 依旧是 `["refining", "distribution"]`。

### 3) 查询单产品的贡献链（证据）

```bash
curl -s http://localhost:8000/api/snapshots/snap_404159d90ed529d8/products/diesel_delivered
```

```json
{
  "snapshot_id": "snap_404159d90ed529d8",
  "factor_version": "v2023-01",
  "created_at": "2026-10-02T06:36:18+00:00",
  "product": {
    "product_id": "diesel_delivered",
    "name": "交付柴油",
    "producing_process": "distribution",
    "quantity": "500.000000000000",
    "unit": "kg",
    "total_kg_co2e": "117.657894736842",
    "unit_kg_co2e": "0.235315789474",
    "contributions": [
      {"kg_co2e": "42.105263157895", "share_of_product": "0.357861775889",
       "chain": ["external_material", "crude", "refining", "distribution",
                 "diesel_delivered"],
       "kind": "external_material", "source": "crude",
       "factor_ref": "ef.crude_oil", "factor_version": "v2023-01",
       "factor_unit": "kg", "factor_kg_co2e_per_unit": "0.080000000000"},
      {"kg_co2e": "37.026315789474", "share_of_product": "0.314694699172",
       "chain": ["refining", "direct_emission", "electricity", "distribution",
                 "diesel_delivered"],
       "kind": "direct_emission", "source": "electricity",
       "factor_ref": "ef.grid_electricity", "factor_version": "v2023-01",
       "factor_unit": "kWh", "factor_kg_co2e_per_unit": "0.703500000000"},
      {"kg_co2e": "29.526315789474", "share_of_product": "0.250950570342",
       "chain": ["refining", "direct_emission", "natural_gas", "distribution",
                 "diesel_delivered"],
       "kind": "direct_emission", "source": "natural_gas",
       "factor_ref": "ef.natural_gas", "factor_version": "v2023-01",
       "factor_unit": "MJ", "factor_kg_co2e_per_unit": "0.056100000000"},
      {"kg_co2e": "9.000000000000", "share_of_product": "0.076492954596",
       "chain": ["distribution", "direct_emission", "freight",
                 "diesel_delivered"],
       "kind": "direct_emission", "source": "freight",
       "factor_ref": "ef.truck_freight", "factor_version": "v2023-01",
       "factor_unit": "tkm", "factor_kg_co2e_per_unit": "0.090000000000"}
    ]
  }
}
```

### 4) 用新因子版本重算（历史快照不变）

```bash
curl -s -X POST \
  http://localhost:8000/api/snapshots/snap_404159d90ed529d8/recalculate \
  -H 'Content-Type: application/json' \
  -d '{"factor_version": "v2024-01"}'
```

返回 `201 Created`，`new_snapshot.snapshot_id` 是新 ID，`source_snapshot` 指向旧快照：

```json
{
  "source_snapshot_id": "snap_404159d90ed529d8",
  "source_factor_version": "v2023-01",
  "new_snapshot": {
    "snapshot_id": "snap_1a23bd137e949fb0",
    "source_snapshot": "snap_404159d90ed529d8",
    "factor_version": "v2024-01",
    "idempotent_replay": false
  },
  "factor_changes": [
    {"factor_ref": "ef.crude_oil", "status": "changed",
     "old_unit": "kg", "new_unit": "kg",
     "old_kg_co2e_per_unit": "0.080000000000",
     "new_kg_co2e_per_unit": "0.075000000000",
     "delta_kg_co2e_per_unit": "-0.005000000000"},
    {"factor_ref": "ef.grid_electricity", "status": "changed",
     "old_unit": "kWh", "new_unit": "kWh",
     "old_kg_co2e_per_unit": "0.703500000000",
     "new_kg_co2e_per_unit": "0.570300000000",
     "delta_kg_co2e_per_unit": "-0.133200000000"},
    {"factor_ref": "ef.truck_freight", "status": "changed",
     "old_unit": "tkm", "new_unit": "tkm",
     "old_kg_co2e_per_unit": "0.090000000000",
     "new_kg_co2e_per_unit": "0.082000000000",
     "delta_kg_co2e_per_unit": "-0.008000000000"}
  ],
  "product_diffs": [
    {"product_id": "diesel_delivered", "status": "changed",
     "old_factor_version": "v2023-01", "new_factor_version": "v2024-01",
     "old_unit_kg_co2e": "0.235315789474",
     "new_unit_kg_co2e": "0.214431578947",
     "delta_unit_kg_co2e": "-0.020884210527", "delta_percent": "-8.874972"}
  ],
  "changed_product_count": 4
}
```

> **证据口径**：`factor_changes` 的新旧因子值都来自两个快照**封存的
> `result.factors_used`**，而不是当前 factors 表。因此即使旧因子版本在过去
> 被错误覆盖（升级前的历史数据），旧侧依旧是封存时的 `0.0800`，与
> `product_diffs` 所依据的旧快照结果（`0.235315789474`）严格一致；
> `natural_gas` 两侧数值相同，故不出现在清单中。

再查旧快照 `GET /api/snapshots/snap_404159d90ed529d8`，单位足迹仍是
`0.235315789474`，因子副本仍是 `v2023-01` 的数值——历史不会被因子更新改写。
重算接口本身也是幂等的：第二次调用返回 `200` 与同一个新快照 ID，差异清单
同样基于封存证据，重复结果完全一致。

### 5) 登记因子版本（不可变，拒绝覆盖）

登记一个新版本返回 `201`：

```bash
curl -s -X POST http://localhost:8000/api/factor-versions \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "v2025-demo",
    "factors": [
      {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
       "kg_co2e_per_unit": "1.200"}
    ]
  }'
```

```json
{"version": "v2025-demo", "created": true,
 "idempotent_replay": false, "factor_count": 1}
```

用**完全相同**的内容（ref 集合、名称、单位、数值按 Decimal 数值比较，
所以 `1.2` 与 `1.200` 等价）再提交一次，幂等返回 `200`，不写任何数据：

```bash
curl -s -X POST http://localhost:8000/api/factor-versions \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "v2025-demo",
    "factors": [
      {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
       "kg_co2e_per_unit": "1.2"}
    ]
  }'
```

```json
{"version": "v2025-demo", "created": false,
 "idempotent_replay": true, "factor_count": 1}
```

若同名版本中**任一**因子的值 / 单位 / 名称变化，或因子 ref 集合增删，
返回 `409 Conflict`，差异逐项列出，封存内容原样保留。例如把数值改成 `1.3`：

```bash
curl -s -X POST http://localhost:8000/api/factor-versions \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "v2025-demo",
    "factors": [
      {"ref": "ef.hydrogen", "name": "绿氢", "unit": "kg",
       "kg_co2e_per_unit": "1.3"}
    ]
  }'
```

```json
{
  "error": {
    "code": "FACTOR_VERSION_CONFLICT",
    "message": "因子版本 v2025-demo 已封存且内容不一致；版本不可覆盖，请改用新的版本名提交",
    "details": {
      "version": "v2025-demo",
      "issues": [
        {"path": "factors[ref=ef.hydrogen].kg_co2e_per_unit",
         "code": "FACTOR_VALUE_CONFLICT",
         "message": "因子 ef.hydrogen 的数值与已封存版本不一致: 1.200 != 1.3"}
      ]
    }
  }
}
```

向已存在的 `v2023-01` 提交缺失多个因子、又多出一个新因子的请求体，
会一次性得到全部差异（节选；`issues` 按 path 稳定排序）：

```json
{"code": "FACTOR_ADDED",
 "path": "factors[ref=ef.new_one]", "message": "版本已封存，不能新增因子: ef.new_one"}
{"code": "FACTOR_REMOVED",
 "path": "factors[ref=ef.diesel]", "message": "版本已封存，不能删除因子: ef.diesel"}
```

同一请求体内出现重复因子 ref 返回 `422`（`DUPLICATE_FACTOR`），且不写入任何行。
**要使用新数值，请登记新版本名**（如 `v2026-01`），随后在核算或重算中引用它。

### 6) 错误响应示例（环 + 负量 + 零分母一次性返回）

`POST /api/assessments`，过程 A 投入 B 的产品、B 投入 A 的产品，且 B 有负投入量、
质量权重全为 0 → `422 Unprocessable Entity`：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "核算请求校验失败",
    "issues": [
      {"path": "processes.graph", "code": "CYCLE_DETECTED",
       "message": "过程投入有向图存在环: A -> B -> A"},
      {"path": "processes[process_id=B].allocation:mass",
       "code": "ALLOCATION_DENOMINATOR_ZERO",
       "message": "过程 B 的mass分摊权重之和为零，无法分摊"},
      {"path": "processes[process_id=B].inputs[0].quantity",
       "code": "NEGATIVE_AMOUNT", "message": "投入数量不能为负"}
    ]
  }
}
```

`issues` 按 `(path, code)` 稳定排序，便于客户端做断言与回归比对。

## 设计要点

- **幂等键**：`sha256(canonical_json(因子版本, 分摊法, 外部物料[], 过程[]))`，
  列表按键排序、Decimal 用规范化文本（`0.10` 与 `0.1` 等价）；显示名与
  `client_request_id` 不参与哈希。
- **分摊与传递**：过程足迹 = 直接排放（数量×因子）+ 外部物料足迹 + Σ上游产品足迹×
  （消耗量/上游批量）；再按所选权重分摊到本过程的全部联产品。贡献链携带每一段的
  缩放与分摊因子，可逐条审计。
- **因子版本不可变**：`factor_versions` 与 `factors` 仅以普通 `INSERT`
  在**单个事务**中写入（无 `UPDATE`/`REPLACE`），主键 `(version, ref)`
  唯一约束兜底。服务层先比对再写入、并用进程级锁串行化；同名重提仅在
  ref 集合 + 名称 + 单位 + Decimal 数值全等时幂等，否则 `409` 并回滚，
  跨连接竞态靠 `IntegrityError` 后复核，保证并发提交不部分写入、不互相覆盖。
- **证据固化**：快照表保存原始请求 JSON、完整结果 JSON（含 `factors_used` 因子值副本）、
  输入哈希、因子版本、来源快照与 UTC 时间戳，只增不改。重算的 `product_diffs`
  与 `factor_changes` **只读取两个快照封存的 `factors_used`**，因此旧因子版本
  即便在历史上被覆盖，重算结果仍与旧快照自身的足迹一致、且重放幂等。
- **数值口径**：全链路 `Decimal`（进入时从字符串构造，拒绝 NaN/Infinity），
  50 位上下文运算，输出量化到 12 位小数，差异百分比量化到 6 位。

## 目录结构

```
app/
  main.py         FastAPI 路由、异常信封、应用工厂
  models.py       Pydantic 请求模型（Decimal 字符串类型）
  service.py      因子版本不可变登记、幂等快照、封存证据差异
  accounting.py   校验聚合 + 拓扑计算 + 分摊 + 贡献链
  graph.py        Kahn 拓扑排序 / Tarjan 环检测
  idempotency.py  规范化输入摘要与 SHA-256
  decimals.py     Decimal 精度、量化、JSON 化
  db.py           SQLite schema 与快照/因子仓储（因子只 INSERT）
  seed.py         内置 v2023-01 / v2024-01 因子
tests/            25 个端到端测试（pytest + TestClient，含并发同名提交）
docker-compose.yml / Dockerfile
```
