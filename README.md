# 离线对白字幕分轨 HTTP API

用 Python + FastAPI 实现的离线服务：给定 2～3 条屏幕轨道和 1～12 条对白字幕，为每条字幕选择一条轨道，满足：

- **轨道互斥**：字幕占用左闭右开时间区间 `[start_ms, end_ms)`；时间重叠的字幕不能占同一轨；端点相接（前一条的 `end_ms` == 后一条的 `start_ms`）可以复用同一轨。
- **锁定**：字幕可指定 `locked_track`，强制占用该轨道（轨道号从 1 开始）。
- **优化目标**：同一说话人按时间相邻的两条字幕，换轨次数总和最小。
- **并列裁决**：换轨次数相同时，取“按输入顺序排列的轨道号序列”字典序最小的方案。
- 同一说话人的字幕时间不得重叠（属于输入合法性约束，非法返回 422）。
- 分轨结果可作为**场景（scene）**按 `scene_id` 保存为版本化修订：新场景以 `expected_revision: 0` 创建（成功后版本 1），后续修订须带当前版本；版本不符返回 409。数据落在本地 SQLite，容器重启后仍可读取。
- 已保存场景可创建**播放会话（session）**：单进程内存态会话复制场景完整快照，游标从 0 毫秒开始推进（只能不减）；播放中可接纳场景的后续修订——已开始（`start_ms` ≤ 游标）的字幕保持原分轨，其余字幕按原最少换轨与并列规则重算。会话不跨重启保留。接纳是完整操作：场景读取、已开始字幕校验、分轨重算与会话替换在会话锁内串行生效，迟到的接纳请求读到的是轮到它生效时的场景快照，不会把会话回写到旧版本。
- 播放客户端可通过 `POST /sessions/{session_id}/events` 做**带事件的推进**：一次推进返回**旧游标 < 事件时刻 ≤ `time_ms`** 的全部字幕进入/退出事件，跨过整条短字幕（进入与退出都落在两次采样之间）也不会漏掉。事件生成与游标更新、接纳修订在同一会话锁内完整串行。

## 一条命令启动

```bash
docker compose up --build -d
```

服务监听 **8080** 端口，接口地址：`http://localhost:8080/assign`

停止：`docker compose down`

> - 交互式查看日志可去掉 `-d`。交互式文档：`http://localhost:8080/docs`（Swagger UI）。
> - Compose 自动把具名卷 `scenes-data` 挂载到容器内 `/data`：场景数据库（`SCENES_DB_PATH=/data/scenes.db`）在容器重启甚至 `docker compose down && up` 后仍然保留。仅 `docker compose down -v` 删除卷时才会清除场景。

## 请求

`POST /assign`，JSON Body：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `track_count` | int | 屏幕轨道数，2 或 3 |
| `subtitles` | array | 1～12 条，顺序即输入顺序 |
| `subtitles[].id` | string \| int | 全局唯一，字符串不得为空 |
| `subtitles[].speaker` | string | 非空（不能全为空白） |
| `subtitles[].start_ms` | int | 非负 |
| `subtitles[].end_ms` | int | 非负，且严格大于 `start_ms` |
| `subtitles[].locked_track` | int \| null | 可选，锁定轨道号（≥1 且 ≤ `track_count`） |

## 响应

- 有解：HTTP 200，`feasible: true`，`changes` 为换轨总数，`assignments` 按**输入原顺序**给出每条字幕的轨道。
- 锁定冲突或轨道不足：HTTP 200，`feasible: false`，`changes: 0`，`assignments: []`（空方案）。
- 非法输入（字段缺失/越界、id 重复、同一说话人字幕时间重叠等）：HTTP **422**，带校验错误明细。

## 可执行的 HTTP 调用示例

### 示例 1：有解（含锁定轨道与端点相接）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
      {"id": "s4", "speaker": "Alice", "start_ms": 2500, "end_ms": 4000}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 1}
  ]
}
```

说明：s1 与 s3 时间相接可同轨，s3 与 s4 同轨；Alice 三条字幕在同一轨，换轨 0 次。

### 示例 2：非零换轨（锁定迫使相邻字幕分轨）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100, "locked_track": 1},
      {"id": "b", "speaker": "A", "start_ms": 100, "end_ms": 200, "locked_track": 2},
      {"id": "c", "speaker": "A", "start_ms": 200, "end_ms": 300}
    ]
  }'
```

响应：

```json
{
  "feasible": true,
  "changes": 1,
  "assignments": [
    {"id": "a", "track": 1},
    {"id": "b", "track": 2},
    {"id": "c", "track": 2}
  ]
}
```

### 示例 3：无解 —— 两条重叠字幕锁定同一轨（空方案）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 2000, "locked_track": 1},
      {"id": "s2", "speaker": "Bob",   "start_ms": 1000, "end_ms": 3000, "locked_track": 1}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 4：无解 —— 3 条字幕同时重叠但只有 2 轨（轨道不足）

```bash
curl -s http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,  "end_ms": 100},
      {"id": "b", "speaker": "B", "start_ms": 10, "end_ms": 110},
      {"id": "c", "speaker": "C", "start_ms": 20, "end_ms": 120}
    ]
  }'
```

响应：

```json
{"feasible": false, "changes": 0, "assignments": []}
```

### 示例 5：非法输入返回 422（同一说话人字幕时间重叠）

```bash
curl -s -i http://localhost:8080/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "subtitles": [
      {"id": "a", "speaker": "A", "start_ms": 0,   "end_ms": 100},
      {"id": "b", "speaker": "A", "start_ms": 50,  "end_ms": 150}
    ]
  }'
```

返回 `HTTP/1.1 422 Unprocessable Entity`，body 为 FastAPI 标准校验错误结构。

## 场景（scene）：可保存的修订

`/assign` 只做一次性计算、不保存任何数据。如果需要把某次分轨结果保存下来、以后读取并在其基础上继续修订，使用场景接口，数据写入容器内本地 SQLite（Docker Compose 已挂载具名卷，**容器重启甚至重新创建后场景仍可读取**）。

- `PUT /scenes/{scene_id}`：创建或修订场景。请求体沿用 `/assign` 的 `track_count` 与 `subtitles`（同样的字段约束、校验、最少换轨与并列规则），另加一个字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `expected_revision` | int | 非负。客户端所依据的当前版本：**新场景填 `0`**；修订已存在场景时必须填写其当前版本 |

- 新场景首次成功后 `revision = 1`；此后每次成功修订 `revision + 1`。
- 合法但无解：返回与 `/assign` **完全相同**的无解响应（`feasible: false`、空方案），**不保存、不改变任何已有版本**。
- 非法输入：**422**，不改变已有版本。
- 版本不符（含同一版本的并发竞争落败方）：**409**，响应体给出 `current_revision`，不改变已有版本。同一版本的并发修订最多只有一次成功，读取结果永远是某次成功提交的完整快照，不会混用两次提交的数据。
- `GET /scenes/{scene_id}`：读取最近一次成功修订保存的完整场景（字幕、分轨、changes、版本）；场景不存在返回 **404**。
- 数据库路径可用环境变量 `SCENES_DB_PATH` 覆盖（Compose 中为 `/data/scenes.db`）。

### 示例 6：创建场景（`expected_revision=0`，成功后版本为 1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500}
    ]
  }'
```

响应（HTTP 200）：

```json
{
  "scene_id": "scene-001",
  "revision": 1,
  "track_count": 2,
  "subtitles": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000, "locked_track": null},
    {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500, "locked_track": null}
  ],
  "changes": 0,
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ],
  "feasible": true
}
```

### 示例 7：在当前版本上修订（`expected_revision` 填当前版本，成功后 +1）

```bash
curl -s -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1200},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000},
      {"id": "s3", "speaker": "Alice", "start_ms": 1200, "end_ms": 2500}
    ]
  }'
```

响应中的 `"revision": 2`。再次修订时需把 `expected_revision` 改为 `2`。

### 示例 8：读取场景

```bash
curl -s http://localhost:8080/scenes/scene-001
```

返回最近一次成功修订的完整快照（结构同示例 7 的成功响应）。场景不存在时返回 `HTTP/1.1 404 Not Found`。

### 示例 9：版本冲突返回 409（不改动已有版本）

假设场景当前版本为 2，却仍按旧版本 1 提交：

```bash
curl -s -i -X PUT http://localhost:8080/scenes/scene-001 \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000}
    ]
  }'
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "场景版本冲突：expected_revision 与当前版本不一致",
    "scene_id": "scene-001",
    "current_revision": 2
  }
}
```

此时读取场景，`revision` 仍为 `2`，字幕与分轨仍是修订前的内容。客户端应先 `GET /scenes/scene-001` 取得当前版本，再带上新的 `expected_revision` 重试。对一个已存在的场景提交 `expected_revision: 0`（按新场景创建）同样返回 409；同一版本的并发提交也只有一次成功，其余均返回此响应。

> 合法但无解的场景修订不产生 409/200 快照，而是与示例 3/4 的 `/assign` 一样返回 `{"feasible": false, "changes": 0, "assignments": []}`，且不落库、不推进版本。非法输入（缺少 `expected_revision`、`track_count` 越界、说话人字幕重叠等）返回 422，同样不改动已有版本。

## 播放会话（session）：播放中接纳后续修订

`/assign` 与 `/scenes` 都是无状态的计算/存储；播放会话则让“正在播放”的场景跟上后续修订：先按 `scene_id` 创建会话（复制场景当时的完整快照，游标从 0 毫秒开始），播放过程中推进游标，场景被修订后让会话接纳最新快照。会话是**单进程内存态**，不写入 SQLite、不跨重启保留；同一会话上的推进与接纳按完整操作串行生效（并发请求不会交错出中间态）。接纳的完整操作包含**场景读取本身**：请求在会话锁内才读取场景快照，因此并发下每个接纳请求应用的都是轮到它生效时的快照，会话的 `source_revision` 不会被迟到的请求回退。

- `POST /sessions`：创建会话。请求体 `{"scene_id": "..."}`；场景不存在返回 404。响应为会话完整状态：`session_id`、`scene_id`、`source_revision`（快照来源的场景版本）、`cursor_ms`（初始 0）、`track_count`、`subtitles`、`assignments`、`changes`。
- `POST /sessions/{session_id}/advance`：推进游标。请求体 `{"time_ms": N}`，`N` 为非负整数且**只能不减**（等于当前游标允许）；负数、非整数或倒退返回 422 且会话不变。响应含 `source_revision`、`cursor_ms` 与 `active`：游标时刻处于 `[start_ms, end_ms)`（左闭右开）区间的字幕及其轨道。
- `POST /sessions/{session_id}/events`：**带字幕事件的推进**。请求体与 `/advance` 相同（`{"time_ms": N}`，非负整数、只能不减），同样把游标推进到 `time_ms`，但响应额外含本次推进跨过的全部 `events`：
  - 事件窗口为 **旧游标 < 事件时刻 ≤ `time_ms`**：进入事件时刻取 `start_ms`，退出事件时刻取 `end_ms`（区间左闭右开）。短字幕即使整条落在两次采样之间，其进入与退出也会在同一次响应中返回，不会漏掉；不保存事件历史，也不提供重放——窗口之前的事件不再返回。
  - **0 毫秒已活动**（`start_ms == 0`）的字幕不产生进入事件：它们由 `POST /sessions` 创建响应的快照（`assignments`）确定，客户端创建会话时即应置为活动。
  - 每个事件含字幕 `id`、`speaker`、`track`（事件发生时该字幕在会话中的分轨）、`time_ms`（毫秒时刻）与 `type`（`"enter"` 进入 / `"exit"` 退出）。
  - 事件按 `time_ms` 升序；**同一时刻先退出后进入**；同类事件按会话字幕顺序排列。**等时推进**（`time_ms` 等于当前游标）`events` 为空。
  - 响应还含推进后的 `active`（结构与 `/advance` 相同）、`source_revision` 与推进后的 `cursor_ms`。
  - 事件基于**轮到本请求生效时的会话快照**生成，不读取未接纳的场景修订；事件生成与游标更新是同一个完整操作，与同一会话的 `/advance`、`/accept` 完整串行。未知会话返回 404；非法时间或倒退返回 422，失败不改变会话与场景。
  - `/advance` 的请求与响应保持不变；两个端点推进的是同一个游标，可混用（事件窗口始终为“上一次任一推进后的游标 → 本次游标”）。
- `POST /sessions/{session_id}/accept`：接纳场景当前快照。规则：
  - 轨道数不得变；
  - 会话中 `start_ms` ≤ 游标的字幕（已开始，含已播完），其 `id`、`speaker`、起止时间、`locked_track` 必须不变，并**固定原分轨**；
  - 其余字幕可改、可删、可新增，但都必须在游标之后开始（`start_ms` > 游标）；
  - 按新快照顺序与最少换轨、并列规则重算全部分轨。
  - 场景读取、上述校验、分轨重算与会话替换是同一个完整操作，与同一会话的推进、其他接纳串行生效：并发接纳时，每个请求接纳的都是轮到它生效时的场景快照。
  - 冲突或无解返回 **409** 且会话不变；成功则整体替换会话快照与 `source_revision`（**场景本身不变**），返回会话最新完整状态。
- 会话不存在（含服务重启后）返回 404。

### 示例 10：创建会话并推进游标

沿用示例 6 创建的场景 `scene-001`（revision 1）：

```bash
curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-001"}'
```

```json
{
  "session_id": "9f2c…（略）",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 0,
  "track_count": 2,
  "changes": 0,
  "subtitles": [ … ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1}
  ]
}
```

推进到 900 毫秒（`$SID` 为上一步返回的 `session_id`）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' \
  -d '{"time_ms": 900}'
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 1,
  "cursor_ms": 900,
  "active": [
    {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000, "locked_track": null, "track": 1},
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2}
  ]
}
```

`active` 为游标时刻 `[start_ms, end_ms)` 内的字幕及轨道：900 时 s1（0≤900<1000）与 s2（800≤900<3000）在屏，s3（1000 才开始）不在。再次提交更小的 `time_ms` 返回 422。

### 示例 11：一次推进跨过整条短字幕（进入/退出事件都不漏）

先准备一个含“闪现”短字幕的场景并创建会话（s4 仅在 `[2500, 2550)` 出现 50 毫秒，端点相接复用 s3 腾出的轨 1）：

```bash
curl -s -X PUT http://localhost:8080/scenes/events-demo \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
      {"id": "s4", "speaker": "Dan",   "start_ms": 2500, "end_ms": 2550}
    ]
  }'

SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "events-demo"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
```

> 0 毫秒已活动的 s1（`start_ms=0`）**不产生进入事件**：创建会话的响应快照里 s1→轨 1，客户端创建时即应把它置为活动。

粗粒度采样，客户端在 0 与 2600 毫秒各推进一次——s4 的整个生命期都落在两次采样之间（2500 与 s3 的退出同刻，先退后入）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' \
  -d '{"time_ms": 0}'
# {"session_id":"…","scene_id":"events-demo","source_revision":1,"cursor_ms":0,
#  "events":[],
#  "active":[{"id":"s1","speaker":"Alice","start_ms":0,"end_ms":1000,"locked_track":null,"track":1}]}

curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' \
  -d '{"time_ms": 2600}'
```

```json
{
  "session_id": "…",
  "scene_id": "events-demo",
  "source_revision": 1,
  "cursor_ms": 2600,
  "events": [
    {"id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 800,  "type": "enter"},
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 2500, "type": "exit"},
    {"id": "s4", "speaker": "Dan",   "track": 1, "time_ms": 2500, "type": "enter"},
    {"id": "s4", "speaker": "Dan",   "track": 1, "time_ms": 2550, "type": "exit"}
  ],
  "active": [
    {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000, "locked_track": 2, "track": 2}
  ]
}
```

事件已排好序：800 进入 s2 → **1000 先退出 s1、后进入 s3**（同刻先退后入）→ **2500 先退出 s3、后进入 s4**（端点相接，同刻先退后入）→ 2550 退出 s4。短字幕 s4 虽从未出现在任何一次采样的 `active` 中，进入与退出事件仍完整送达。事件窗口是 `(旧游标, 新游标]`，因此再推进到 2600（等时推进）返回 `"events": []`；倒退到 2599 返回 422 且游标不变。

### 示例 12：端点交接——创建快照置初始活动，/advance 与 /events 共用游标

事件端点与原 `/advance` 推进的是同一个游标，可以混用；事件只覆盖“上一次推进（任一端点）之后”的窗口，因此 0 时刻的活动状态要靠创建响应交接：

```bash
# 创建会话：用响应快照确定 0 毫秒已活动的字幕（s1 在轨 1），不等待事件
curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' -d '{"scene_id": "events-demo"}' \
  | python3 -c 'import sys,json; s=json.load(sys.stdin); print(s["session_id"]); print(s["assignments"])'

# 用原 /advance 推进到 700（无事件语义，只取活动快照）
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 700}'

# 改用 /events 推进到 900：事件窗口为 (700, 900]
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}'
```

第二次请求的事件只含窗口内的边界：`800 enter s2`。s1 的进入（0 毫秒）永远不会作为事件下发——它由创建响应的快照确定；继续等时或小步推进也**不重放**窗口之前的事件：

```bash
# 再推进到 1000：窗口 (900, 1000]，1000 同刻先退后入
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
# events: 1000 exit s1, 1000 enter s3

# 等时推进：事件为空
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 1000}'
# {"…","cursor_ms":1000,"events":[],"active":[{"id":"s2",…},{"id":"s3",…}]}
```

非法时间（负数、非整数）或小于当前游标返回 **422**，会话不变；未知会话（含服务重启后）返回 **404**。服务不保存事件历史，也不提供重放；中途加入的客户端应新建会话并以创建快照为准。

### 示例 13：播放中接纳场景修订

> 本例沿用示例 10 的会话（`scene-001`，游标 900）；如已用示例 11/12 的 `events-demo` 做过实验，请按示例 10 重新创建 `scene-001` 的会话并推进到 900。

播放期间场景被修订（把 `scene-001` 推进到 revision 2：s3 延长到 4000，并新增 s4 `[3200, 4000)`；修订方式同示例 7）。会话接纳最新快照：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/accept
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 2,
  "cursor_ms": 900,
  "track_count": 2,
  "changes": 0,
  "subtitles": [ … s1, s2, s3(1000–4000), s4(3200–4000) ],
  "assignments": [
    {"id": "s1", "track": 1},
    {"id": "s2", "track": 2},
    {"id": "s3", "track": 1},
    {"id": "s4", "track": 2}
  ]
}
```

游标 900 时 s1、s2 已开始，保持原分轨 1、2 不变；s3、s4 在游标之后开始，按新快照重算分轨。`source_revision` 变为 2，场景本身的版本与内容不受接纳影响。

接纳之后再用事件端点推进，事件按**新快照**生成（未接纳的场景修订则完全不可见）。例如接着推进到 3500，s3 在修订后延长到 4000、s4 `[3200,4000)` 已进入，二者都仍在屏：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/events \
  -H 'Content-Type: application/json' -d '{"time_ms": 3500}'
```

```json
{
  "session_id": "9f2c…",
  "scene_id": "scene-001",
  "source_revision": 2,
  "cursor_ms": 3500,
  "events": [
    {"id": "s1", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "exit"},
    {"id": "s3", "speaker": "Alice", "track": 1, "time_ms": 1000, "type": "enter"},
    {"id": "s2", "speaker": "Bob",   "track": 2, "time_ms": 3000, "type": "exit"},
    {"id": "s4", "speaker": "Carol", "track": 2, "time_ms": 3200, "type": "enter"}
  ],
  "active": [
    {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000, "locked_track": null, "track": 1},
    {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000, "locked_track": null, "track": 2}
  ]
}
```

事件窗口是接纳前的旧游标 900：s1 的退出（1000）与 s3 的进入（1000，同刻先退后入）来自已开始字幕的固定区间，s2 的退出（3000）、s4 的进入（3200）只可能来自**已接纳**的修订（s4 是版本 2 新增字幕）。事件生成、游标更新与接纳在同一会话锁内完整串行，并发下不会读到半接纳状态。

### 示例 14：接纳冲突返回 409（会话不变）

若场景的修订动了已开始字幕（上例中游标 900 时修改了 s1 的起止时间、删除 s2、把轨道数改为 3，或新增的字幕在 900 之前开始等），接纳失败：

```bash
curl -s -i -X POST http://localhost:8080/sessions/$SID/accept
```

```
HTTP/1.1 409 Conflict
```

```json
{
  "detail": {
    "message": "字幕 's1' 已开始，其 id/speaker/起止时间/locked_track 不得修改",
    "scene_id": "scene-001"
  }
}
```

固定已开始字幕的原分轨后新快照无解时同样返回 409。失败的接纳不改变会话：游标、快照、`source_revision` 保持原值，可继续推进或在场景改回合法内容后重试接纳。

### 接纳的并发修复说明

此前 `accept` 在进入会话锁**之前**读取场景快照，存在回写缺陷：请求 R1 读到场景版本 1 后被挂起；期间场景把某条未开始字幕的 `end_ms` 从 2000 改为 4000 并保存为版本 2，另一请求 R2 先完成接纳（会话到达版本 2）；R1 恢复后仍应用手中过期的版本 1 快照，把会话的 `source_revision` 与字幕**回退**到版本 1。

修复后，接纳把“读取场景快照 → 校验已开始字幕 → 重算分轨 → 替换会话状态”作为**完整操作**在会话锁内串行生效：场景读取发生在轮到该请求生效之时，因此迟到的请求接纳的一定是当时的场景快照——既不会把旧数据回写到会话上，也不会跳过旧数据却报告成功。会话的 `source_revision` 对同一会话只增不回退。请求、响应与错误码（200/404/409/422）均与之前兼容。

### 示例 15：并发接纳——迟到请求不会回写旧快照

```bash
# 1) 创建场景（版本 1：未开始的 s3 end_ms=2000）
curl -s -X PUT http://localhost:8080/scenes/scene-race \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 0,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2000}
    ]
  }'

# 2) 创建会话并推进游标到 900（s1、s2 已开始，s3 未开始）
SID=$(curl -s -X POST http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"scene_id": "scene-race"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 900}'

# 3) 场景修订为版本 2：s3 的 end_ms 从 2000 改为 4000
curl -s -X PUT http://localhost:8080/scenes/scene-race \
  -H 'Content-Type: application/json' \
  -d '{
    "track_count": 2,
    "expected_revision": 1,
    "subtitles": [
      {"id": "s1", "speaker": "Alice", "start_ms": 0,    "end_ms": 1000},
      {"id": "s2", "speaker": "Bob",   "start_ms": 800,  "end_ms": 3000, "locked_track": 2},
      {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000}
    ]
  }'

# 4) 并发发起两次接纳：串行生效，响应一致，均为版本 2 快照
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
curl -s -X POST http://localhost:8080/sessions/$SID/accept &
wait
```

两个并发响应都是 `source_revision: 2`、`cursor_ms: 900`、s3 的 `end_ms: 4000`，分轨为 s1→1、s2→2（已开始，固定原分轨）、s3→1。无论请求到达顺序如何交错，会话都不会再回退到版本 1 的内容；可用推进验证（s3 在 2500 时仍在屏，说明其 `end_ms` 是 4000 而非 2000）：

```bash
curl -s -X POST http://localhost:8080/sessions/$SID/advance \
  -H 'Content-Type: application/json' -d '{"time_ms": 2500}'
# {"session_id":"…","scene_id":"scene-race","source_revision":2,"cursor_ms":2500,
#  "active":[{"id":"s2",…,"track":2},{"id":"s3",…,"end_ms":4000,…,"track":1}]}
```

健康检查：

```bash
curl -s http://localhost:8080/health
# {"status":"ok"}
```

## 本地开发与测试（不使用 Docker）

需 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
pytest
```

本地（非 Docker）运行时，场景数据库默认写入工作目录下的 `scenes.db`；可用环境变量改到别处，例如 `SCENES_DB_PATH=/tmp/scenes.db uvicorn app.main:app --port 8080`。

核心求解器（`app/solver.py`）无第三方依赖，也可直接运行其测试：

```bash
python tests/test_solver.py
```

## 算法说明

输入规模很小（≤12 条、≤3 轨），采用带剪枝的深度优先枚举：

1. 按输入顺序逐条分配轨道，每个位置按轨道号 1→k 升序尝试；
2. 候选轨道必须未被任何时间重叠的已分配字幕占用，锁定字幕只尝试锁定轨；
3. 代价在“同一说话人按时间排序后相邻”的字幕对两端都落位时累计（换轨计 1）；
4. 剪枝：当前代价不低于已找到的最优解时剪枝。由于按轨道号升序枚举，第一个到达的最优解即为字典序最小的轨道序列，无需额外比较。

实现已用穷举参考解对 4000 组随机实例交叉验证。

## 目录结构

```
app/
  main.py      # FastAPI 路由与请求/响应模型（Pydantic 校验）
  solver.py    # 纯 Python 求解器与业务校验（无第三方依赖）
  storage.py   # 场景持久化：本地 SQLite（乐观锁版本控制）
  playback.py  # 播放会话：单进程内存态，推进/带事件推进/接纳串行化（接纳含场景读取的完整操作）、跨过事件生成与保轨重算
tests/
  conftest.py        # pytest 夹具（场景接口使用临时数据库、会话隔离）
  test_solver.py     # 求解器单元测试
  test_api.py        # /assign HTTP 层测试
  test_scenes.py     # 场景创建/修订/读取/版本冲突/并发测试
  test_playback.py   # 播放会话创建/推进/事件流/接纳/冲突/并发测试
Dockerfile
docker-compose.yml   # 挂载 scenes-data 具名卷到 /data
requirements.txt / requirements-dev.txt
```
