"""播放会话测试：创建/推进/接纳、已开始字幕保轨、冲突 409、并发串行化、
迟到接纳请求不得回写旧快照的回归。"""
import json
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import app  # noqa: E402


def scene_payload(track_count=2):
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) —— 分轨 1/2/1，换轨 0。"""
    return {
        "track_count": track_count,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
        ],
    }


def make_session(client, scene_id="scene-play"):
    """创建场景（revision=1）并以其创建会话，返回会话状态。"""
    resp = client.put(f"/scenes/{scene_id}", json=scene_payload())
    assert resp.status_code == 200
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()


def revise(client, scene_id, expected_revision, subtitles, track_count=2):
    body = {
        "track_count": track_count,
        "expected_revision": expected_revision,
        "subtitles": subtitles,
    }
    resp = client.put(f"/scenes/{scene_id}", json=body)
    assert resp.status_code == 200
    assert resp.json()["feasible"] is True
    return resp.json()


# ---------------------------------------------------------------------------
# 创建会话
# ---------------------------------------------------------------------------


def test_create_session_copies_full_snapshot():
    client = TestClient(app)
    state = make_session(client)
    assert state["session_id"]
    assert state["scene_id"] == "scene-play"
    assert state["source_revision"] == 1
    assert state["cursor_ms"] == 0
    assert state["track_count"] == 2
    assert state["changes"] == 0
    assert [s["id"] for s in state["subtitles"]] == ["s1", "s2", "s3"]
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_create_session_unknown_scene_404():
    client = TestClient(app)
    resp = client.post("/sessions", json={"scene_id": "nope"})
    assert resp.status_code == 404


def test_create_session_blank_scene_id_422():
    client = TestClient(app)
    resp = client.post("/sessions", json={"scene_id": "  "})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


def test_advance_returns_active_left_closed_right_open():
    client = TestClient(app)
    sid = make_session(client)["session_id"]

    # 800：s1（0<=800<1000）与 s2（800<=800<3000，左闭）活动
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 800})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cursor_ms"] == 800
    assert body["source_revision"] == 1
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]

    # 1000：s1 结束（右开），s3 开始（左闭）
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000}).json()
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s2", 2),
        ("s3", 1),
    ]

    # 3000：s2、s3 均已结束
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 3000}).json()
    assert body["active"] == []


def test_advance_active_entries_carry_full_subtitle_fields():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900}).json()
    s2 = next(a for a in body["active"] if a["id"] == "s2")
    assert s2 == {
        "id": "s2",
        "speaker": "Bob",
        "start_ms": 800,
        "end_ms": 3000,
        "locked_track": 2,
        "track": 2,
    }


def test_advance_same_time_allowed():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    for _ in range(2):
        resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 500})
        assert resp.status_code == 200
        assert resp.json()["cursor_ms"] == 500


def test_advance_regression_422_and_session_unchanged():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000})

    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 999})
    assert resp.status_code == 422
    assert resp.json()["detail"]["cursor_ms"] == 1000

    # 失败的推进不改变会话：游标仍是 1000
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 1000}).json()
    assert body["cursor_ms"] == 1000
    assert [a["id"] for a in body["active"]] == ["s2", "s3"]


def test_advance_negative_time_422():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": -1})
    assert resp.status_code == 422


def test_advance_unknown_session_404():
    client = TestClient(app)
    resp = client.post("/sessions/nope/advance", json={"time_ms": 0})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 推进事件流：POST /sessions/{id}/events
# ---------------------------------------------------------------------------


def events_scene_payload():
    """s1[0,1000) s2[800,3000)锁2 s3[1000,2500) 短字幕 s4[2500,2550)。

    分轨 1/2/1/1（s4 在 s3 结束后进入，与 s2 不重叠，可复用轨 1）。
    """
    return {
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
            {"id": "s4", "speaker": "Dan", "start_ms": 2500, "end_ms": 2550},
        ],
    }


def make_events_session(client, scene_id="scene-events"):
    resp = client.put(f"/scenes/{scene_id}", json=events_scene_payload())
    assert resp.status_code == 200
    assert resp.json()["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 1},
    ]
    resp = client.post("/sessions", json={"scene_id": scene_id})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def post_events(client, sid, time_ms):
    return client.post(f"/sessions/{sid}/events", json={"time_ms": time_ms})


def test_events_zero_ms_active_subtitles_have_no_enter_event():
    """0 毫秒已活动（start_ms=0）的字幕由创建快照确定，不产生进入事件。"""
    client = TestClient(app)
    sid = make_events_session(client)

    resp = post_events(client, sid, 0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cursor_ms"] == 0
    assert body["source_revision"] == 1
    assert body["events"] == []  # 等时推进/初始推进到 0：无事件
    # 活动字幕来自创建快照的语义：0 时 s1 在屏
    assert [a["id"] for a in body["active"]] == ["s1"]


def test_events_cross_whole_short_subtitle_in_one_advance():
    """一次大步推进跨过整条短字幕：进入与退出都要返回，不漏事件。"""
    client = TestClient(app)
    sid = make_events_session(client)

    body = post_events(client, sid, 2600).json()
    events = [(e["time_ms"], e["type"], e["id"]) for e in body["events"]]
    assert events == [
        (800, "enter", "s2"),
        (1000, "exit", "s1"),   # 同刻先退出
        (1000, "enter", "s3"),  # 后进入
        (2500, "exit", "s3"),
        (2500, "enter", "s4"),  # 端点相接：s3 退、s4 进同刻，先退后入
        (2550, "exit", "s4"),   # 短字幕退出（两次采样之间完整跨过）
    ]
    # 推进后活动字幕（s4 已退出，仅 s2 在屏）
    assert [(a["id"], a["track"]) for a in body["active"]] == [("s2", 2)]
    assert body["cursor_ms"] == 2600


def test_events_carry_id_speaker_track_time_and_type():
    client = TestClient(app)
    sid = make_events_session(client)

    body = post_events(client, sid, 800).json()
    assert body["events"] == [
        {"id": "s2", "speaker": "Bob", "track": 2,
         "time_ms": 800, "type": "enter"}
    ]

    # 跨过 s1 退出
    body = post_events(client, sid, 1000).json()
    assert body["events"] == [
        {"id": "s1", "speaker": "Alice", "track": 1,
         "time_ms": 1000, "type": "exit"},
        {"id": "s3", "speaker": "Alice", "track": 1,
         "time_ms": 1000, "type": "enter"},
    ]


def test_events_same_timestamp_exits_before_enters():
    """同刻事件：先全部退出，后全部进入；同类按会话字幕顺序。"""
    client = TestClient(app)
    # 构造同刻两条退出（s1 与 sx 都在 1000 结束）与一条进入
    client.put("/scenes/scene-sametime", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "sx", "speaker": "X", "start_ms": 900, "end_ms": 1000,
             "locked_track": 2},
            {"id": "s2", "speaker": "Bob", "start_ms": 1000, "end_ms": 2000},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": "scene-sametime"}).json()[
        "session_id"
    ]

    body = post_events(client, sid, 1000).json()
    events = [(e["type"], e["id"]) for e in body["events"]]
    # 1000：退出按会话顺序 s1、sx，之后进入 s2
    assert events == [("exit", "s1"), ("exit", "sx"), ("enter", "s2")]
    assert [a["id"] for a in body["active"]] == ["s2"]


def test_events_equal_time_advance_is_empty():
    client = TestClient(app)
    sid = make_events_session(client)
    post_events(client, sid, 2000)
    resp = post_events(client, sid, 2000)
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["cursor_ms"] == 2000
    assert [(a["id"]) for a in body["active"]] == ["s2", "s3"]


def test_events_do_not_reemit_across_separate_advances():
    """事件窗口为 (旧游标, 新游标]：分段推进不会重复、不会漏掉。"""
    client = TestClient(app)
    sid = make_events_session(client)

    first = [(e["time_ms"], e["type"], e["id"])
             for e in post_events(client, sid, 900).json()["events"]]
    assert first == [(800, "enter", "s2")]

    second = [(e["time_ms"], e["type"], e["id"])
              for e in post_events(client, sid, 2550).json()["events"]]
    assert second == [
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
        (2500, "exit", "s3"),
        (2500, "enter", "s4"),
        (2550, "exit", "s4"),
    ]

    third = [(e["time_ms"], e["type"], e["id"])
             for e in post_events(client, sid, 4000).json()["events"]]
    assert third == [(3000, "exit", "s2")]


def test_events_regression_422_and_session_unchanged():
    client = TestClient(app)
    sid = make_events_session(client)
    post_events(client, sid, 2000)

    resp = post_events(client, sid, 1999)
    assert resp.status_code == 422
    assert resp.json()["detail"]["cursor_ms"] == 2000
    assert resp.json()["detail"]["time_ms"] == 1999

    # 失败不产生事件、不推进游标：用等时推进确认游标仍为 2000
    body = post_events(client, sid, 2000).json()
    assert body["cursor_ms"] == 2000
    assert body["events"] == []
    assert [a["id"] for a in body["active"]] == ["s2", "s3"]


def test_events_negative_time_422():
    client = TestClient(app)
    sid = make_events_session(client)
    assert post_events(client, sid, -1).status_code == 422
    # 非整数同样 422（Pydantic 校验）
    resp = client.post(f"/sessions/{sid}/events", json={"time_ms": 1.5})
    assert resp.status_code == 422


def test_events_unknown_session_404():
    client = TestClient(app)
    assert post_events(client, "nope", 0).status_code == 404


def test_events_use_accepted_snapshot_not_unaccepted_revision():
    """接纳后事件按新快照生成；未接纳的场景修订不影响事件。"""
    client = TestClient(app)
    scene_id = "scene-events-acc"
    sid = make_events_session(client, scene_id)
    post_events(client, sid, 900)  # s1、s2 已开始

    # 修订场景为版本 2（s3 延长、新增 s5、删除短字幕 s4），但先不接纳
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2600},
        {"id": "s5", "speaker": "Eve", "start_ms": 2700, "end_ms": 2900},
    ])

    # 未接纳：推进仍用会话快照（s3 end_ms=2500，有 s4、无 s5）
    body = post_events(client, sid, 3000).json()
    events = [(e["time_ms"], e["type"], e["id"]) for e in body["events"]]
    assert events == [
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
        (2500, "exit", "s3"),
        (2500, "enter", "s4"),
        (2550, "exit", "s4"),
        (3000, "exit", "s2"),
    ]
    assert body["source_revision"] == 1

    # 接纳后按新快照：s3 在 2600 才退出，s5 的进入/退出可见，s4 不存在
    assert client.post(f"/sessions/{sid}/accept").status_code == 200
    body = post_events(client, sid, 3000).json()
    events = [(e["time_ms"], e["type"], e["id"]) for e in body["events"]]
    assert events == [
        (2600, "exit", "s3"),
        (2700, "enter", "s5"),
        (2900, "exit", "s5"),
        (3000, "exit", "s2"),
    ]
    assert body["source_revision"] == 2
    assert body["active"] == []


def test_events_serialized_with_accept_under_concurrency(monkeypatch):
    """事件推进与接纳完整串行：在途接纳在锁内读快照，事件推进不会读到
    半接纳状态；两者结果均自洽。"""
    client = TestClient(app)
    scene_id = "scene-events-race"
    sid = make_events_session(client, scene_id)
    post_events(client, sid, 900)  # s1、s2 已开始

    # 接纳读到的快照为版本 2：s3 延长到 2600，删除短字幕 s4，新增 s5
    extend_s3_to_4000_nos4(client, scene_id)

    # 暂停接纳的“锁内场景读取”，让事件推进先抵达会话锁并排队
    store = main_module.get_store()
    real_get_scene = store.get_scene
    paused = threading.Event()
    release = threading.Event()
    entered = []

    def wrapped_get_scene(sid_):
        if entered:
            return real_get_scene(sid_)
        entered.append(sid_)
        paused.set()
        assert release.wait(timeout=10)
        return real_get_scene(sid_)

    monkeypatch.setattr(store, "get_scene", wrapped_get_scene)

    responses = {}

    def post_accept():
        local = TestClient(app)
        responses["accept"] = local.post(f"/sessions/{sid}/accept")

    t = threading.Thread(target=post_accept)
    t.start()
    assert paused.wait(timeout=10)  # 接纳已持锁并停在锁内读取
    try:
        # 事件推进必须等接纳的完整操作结束，不能穿插
        fired = threading.Event()

        def post_events_locked():
            local = TestClient(app)
            r = local.post(f"/sessions/{sid}/events", json={"time_ms": 2500})
            responses["events"] = r
            fired.set()

        te = threading.Thread(target=post_events_locked)
        te.start()
        assert not fired.wait(timeout=0.5), "事件推进在接纳完整操作结束前生效"
    finally:
        release.set()
    t.join(timeout=10)
    te.join(timeout=10)
    assert not t.is_alive() and not te.is_alive()

    assert responses["accept"].status_code == 200
    ev_body = responses["events"].json()
    # 接纳先生效：事件基于版本 2 快照——s3 延长到 2600（2500 仍活动），s4 不存在
    assert ev_body["source_revision"] == 2
    events = [(e["time_ms"], e["type"], e["id"]) for e in ev_body["events"]]
    assert events == [
        (1000, "exit", "s1"),
        (1000, "enter", "s3"),
    ]
    assert [a["id"] for a in ev_body["active"]] == ["s2", "s3"]


def extend_s3_to_4000_nos4(client, scene_id):
    """版本 2：s3 end_ms 2500 -> 2600，删除短字幕 s4，新增 s5[2700,2900)。"""
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2600},
        {"id": "s5", "speaker": "Eve", "start_ms": 2700, "end_ms": 2900},
    ])


# ---------------------------------------------------------------------------
# 接纳
# ---------------------------------------------------------------------------


def test_accept_keeps_started_tracks_and_recomputes_rest():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # 游标 900：s1、s2 已开始（固定轨道 1、2）；s3 未开始

    # 场景修订：s3 延长、新增 s4（均在游标之后开始）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])

    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    state = resp.json()
    assert state["source_revision"] == 2
    assert state["cursor_ms"] == 900  # 游标不变
    assert state["track_count"] == 2
    assert state["assignments"] == [
        {"id": "s1", "track": 1},  # 已开始：固定原分轨
        {"id": "s2", "track": 2},  # 已开始：固定原分轨
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 2},
    ]
    assert state["changes"] == 0

    # 接纳不改动场景本身
    scene = client.get(f"/scenes/{scene_id}").json()
    assert scene["revision"] == 2
    assert [s["id"] for s in scene["subtitles"]] == ["s1", "s2", "s3", "s4"]

    # 推进按新快照生效
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 3500}).json()
    assert body["source_revision"] == 2
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s3", 1),
        ("s4", 2),
    ]


def test_accept_without_scene_change_is_noop_success():
    client = TestClient(app)
    sid = make_session(client)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    state = resp.json()
    assert state["source_revision"] == 1
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_accept_future_subtitle_deleted_ok():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # 删除未开始的 s3（start 1000 > 游标 900）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
    ])
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    state = resp.json()
    assert [s["id"] for s in state["subtitles"]] == ["s1", "s2"]
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
    ]


def test_accept_track_count_changed_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]

    revise(client, scene_id, 1, scene_payload()["subtitles"], track_count=3)
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 409

    # 会话未被失败改变：场景改回 2 轨后接纳成功
    revise(client, scene_id, 2, scene_payload()["subtitles"], track_count=2)
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    assert resp.json()["source_revision"] == 3
    assert resp.json()["track_count"] == 2


def test_accept_started_subtitle_modified_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # s1 已开始（start 0 <= 900）：end_ms 被修改
    subs = scene_payload()["subtitles"]
    subs[0]["end_ms"] = 900
    revise(client, scene_id, 1, subs)
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 409

    # 会话保持旧快照：场景改回原样后接纳成功
    revise(client, scene_id, 2, scene_payload()["subtitles"])
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 200
    assert resp.json()["source_revision"] == 3
    assert resp.json()["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
    ]


def test_accept_started_subtitle_locked_track_changed_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    subs = scene_payload()["subtitles"]
    subs[1]["locked_track"] = 1  # s2 已开始，locked_track 不得改
    revise(client, scene_id, 1, subs)
    assert client.post(f"/sessions/{sid}/accept").status_code == 409


def test_accept_started_subtitle_deleted_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    # 删除已开始的 s2（start 800 <= 900）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2500},
    ])
    assert client.post(f"/sessions/{sid}/accept").status_code == 409


def test_accept_new_subtitle_starting_before_cursor_409():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})

    subs = scene_payload()["subtitles"] + [
        {"id": "s4", "speaker": "Dan", "start_ms": 500, "end_ms": 600},
    ]
    revise(client, scene_id, 1, subs)
    # 新增字幕开始于游标之前（500 <= 900）：409
    assert client.post(f"/sessions/{sid}/accept").status_code == 409


def test_accept_infeasible_with_pinned_tracks_409():
    client = TestClient(app)
    scene_id = "scene-inf"
    # 自定义场景：s2 不锁轨，分轨 1/2/1
    client.put(f"/scenes/{scene_id}", json={
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "A", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "B", "start_ms": 800, "end_ms": 940},
            {"id": "s3", "speaker": "A", "start_ms": 1000, "end_ms": 2500},
        ],
    })
    sid = client.post("/sessions", json={"scene_id": scene_id}).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    # 游标 900：s1 固定轨道 1，s2 固定轨道 2

    # 修订：新增 s4[950,980) 锁轨道 1（在游标之后开始，本身合法可保存）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "A", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "B", "start_ms": 800, "end_ms": 940},
        {"id": "s3", "speaker": "A", "start_ms": 1000, "end_ms": 2500},
        {"id": "s4", "speaker": "C", "start_ms": 950, "end_ms": 980,
         "locked_track": 1},
    ])
    # s4 与已开始的 s1（固定轨道 1）时间重叠且同轨：无解
    resp = client.post(f"/sessions/{sid}/accept")
    assert resp.status_code == 409

    # 失败的接纳不改变会话：活动字幕仍来自旧快照
    body = client.post(f"/sessions/{sid}/advance", json={"time_ms": 901}).json()
    assert body["source_revision"] == 1
    assert [(a["id"], a["track"]) for a in body["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]


def test_accept_unknown_session_404():
    client = TestClient(app)
    assert client.post("/sessions/nope/accept").status_code == 404


# ---------------------------------------------------------------------------
# 多会话与并发
# ---------------------------------------------------------------------------


def test_sessions_are_independent():
    client = TestClient(app)
    client.put("/scenes/scene-play", json=scene_payload())
    a = client.post("/sessions", json={"scene_id": "scene-play"}).json()
    b = client.post("/sessions", json={"scene_id": "scene-play"}).json()
    assert a["session_id"] != b["session_id"]

    body = client.post(
        f"/sessions/{a['session_id']}/advance", json={"time_ms": 2500}
    ).json()
    assert [(x["id"], x["track"]) for x in body["active"]] == [("s2", 2)]

    # 另一会话游标不受影响；0 时刻 s1 活动（左闭）
    body = client.post(
        f"/sessions/{b['session_id']}/advance", json={"time_ms": 0}
    ).json()
    assert [(x["id"], x["track"]) for x in body["active"]] == [("s1", 1)]


def test_concurrent_accepts_serialized_and_identical():
    client = TestClient(app)
    scene_id = "scene-play"
    sid = make_session(client, scene_id)["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
        {"id": "s4", "speaker": "Carol", "start_ms": 3200, "end_ms": 4000},
    ])

    results = []
    barrier = threading.Barrier(6)

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/sessions/{sid}/accept")
        results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 完整操作串行生效：全部成功，且响应完全一致（同一源版本、同一分轨）
    assert all(status == 200 for status, _ in results)
    bodies = {json.dumps(body, sort_keys=True) for _, body in results}
    assert len(bodies) == 1
    state = results[0][1]
    assert state["source_revision"] == 2
    assert state["assignments"] == [
        {"id": "s1", "track": 1},
        {"id": "s2", "track": 2},
        {"id": "s3", "track": 1},
        {"id": "s4", "track": 2},
    ]


def test_concurrent_advances_serialized():
    client = TestClient(app)
    sid = make_session(client)["session_id"]

    results = []
    barrier = threading.Barrier(5)

    def worker(time_ms):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/sessions/{sid}/advance", json={"time_ms": time_ms})
        results.append((time_ms, resp.status_code))

    times = [100, 300, 500, 700, 900]
    threads = [threading.Thread(target=worker, args=(t,)) for t in times]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 串行后游标只增不减：提交最大时刻的请求必定成功，其余成功或 422
    assert (900, 200) in results
    assert all(status in (200, 422) for _, status in results)

    # 最终游标为 900：等于游标可推进，小于游标被拒绝
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 900}
    ).status_code == 200
    assert client.post(
        f"/sessions/{sid}/advance", json={"time_ms": 899}
    ).status_code == 422


# ---------------------------------------------------------------------------
# 并发回归：迟到的接纳请求不得把会话回写到旧快照
# ---------------------------------------------------------------------------


def stale_scene_payload():
    """版本 1：s3 未开始（start 1000），end_ms=2000。"""
    return {
        "track_count": 2,
        "expected_revision": 0,
        "subtitles": [
            {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
            {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
             "locked_track": 2},
            {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2000},
        ],
    }


def make_stale_session(client, scene_id="scene-stale"):
    """创建场景（revision=1）与会话，并把游标推进到 900（s1、s2 已开始）。"""
    resp = client.put(f"/scenes/{scene_id}", json=stale_scene_payload())
    assert resp.status_code == 200
    sid = client.post("/sessions", json={"scene_id": scene_id}).json()["session_id"]
    resp = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900})
    assert resp.status_code == 200
    return sid


def extend_s3_to_4000(client, scene_id):
    """把场景推进到版本 2：未开始的 s3 的 end_ms 从 2000 改为 4000。"""
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 1000},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 4000},
    ])


def patch_first_scene_read(monkeypatch, *, reread_on_release):
    """故障注入：让第一次场景读取受控暂停，返回 (已暂停事件, 放行事件)。

    reread_on_release=False：暂停前已读出记录，放行后返回该（过时的）
    记录——模拟“读到旧版本后被挂起”的在途请求；
    reread_on_release=True：放行后才真正读取——读取生效点即轮到该
    请求生效的时刻。
    """
    store = main_module.get_store()
    real_get_scene = store.get_scene
    paused = threading.Event()
    release = threading.Event()
    injected = []

    def wrapped_get_scene(scene_id):
        if injected:
            return real_get_scene(scene_id)
        injected.append(scene_id)
        if reread_on_release:
            paused.set()
            assert release.wait(timeout=10), "测试未能及时放行被暂停的读取"
            return real_get_scene(scene_id)
        record = real_get_scene(scene_id)
        paused.set()
        assert release.wait(timeout=10), "测试未能及时放行被暂停的读取"
        return record

    monkeypatch.setattr(store, "get_scene", wrapped_get_scene)
    return paused, release


def post_accept_in_thread(sid, responses, key):
    local = TestClient(app)
    responses[key] = local.post(f"/sessions/{sid}/accept")


def test_accept_stale_inflight_read_cannot_regress_session(monkeypatch):
    """回归：R1 读到版本 1 后暂停，场景推进到版本 2 且 R2 先接纳版本 2；
    R1 恢复后不得把 source_revision 与字幕回写到版本 1。"""
    client = TestClient(app)
    scene_id = "scene-stale"
    sid = make_stale_session(client, scene_id)

    # R1 读到版本 1 的记录后被挂起，恢复后仍返回该（已过时的）记录
    paused, release = patch_first_scene_read(monkeypatch, reread_on_release=False)

    responses = {}
    t1 = threading.Thread(
        target=post_accept_in_thread, args=(sid, responses, "r1")
    )
    t1.start()
    assert paused.wait(timeout=10), "R1 未读到场景版本 1"

    # R1 暂停期间：场景把未开始的 s3 的 end_ms 从 2000 改为 4000（版本 2）
    extend_s3_to_4000(client, scene_id)

    # R2 先接纳版本 2（修复后它会排在 R1 的完整操作之后生效）
    t2 = threading.Thread(
        target=post_accept_in_thread, args=(sid, responses, "r2")
    )
    t2.start()
    try:
        time.sleep(0.2)  # 给 R2 到达会话锁（旧实现下则是完成接纳）的时间
    finally:
        release.set()  # R1 恢复
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    assert responses["r1"].status_code == 200
    assert responses["r2"].status_code == 200
    assert responses["r2"].json()["source_revision"] == 2

    # 关键断言（用推进探测，避免再次接纳掩盖回写）：会话最终停在版本 2，
    # 没有被 R1 恢复后回写到版本 1
    probe = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900}).json()
    assert probe["source_revision"] == 2
    probe = client.post(f"/sessions/{sid}/advance", json={"time_ms": 2500}).json()
    assert probe["source_revision"] == 2
    assert [(a["id"], a["track"]) for a in probe["active"]] == [
        ("s2", 2),
        ("s3", 1),
    ]
    s3 = next(a for a in probe["active"] if a["id"] == "s3")
    assert s3["end_ms"] == 4000  # 字幕未被回写为版本 1 的 2000


def test_accept_reads_scene_snapshot_when_its_turn_comes(monkeypatch):
    """场景读取属于接纳的完整操作：轮到请求生效时才读取场景快照。"""
    client = TestClient(app)
    scene_id = "scene-stale"
    sid = make_stale_session(client, scene_id)

    # R1 在读取生效前被挂起；放行后才真正读取（读到的是当时的快照）
    paused, release = patch_first_scene_read(monkeypatch, reread_on_release=True)

    responses = {}
    t1 = threading.Thread(
        target=post_accept_in_thread, args=(sid, responses, "r1")
    )
    t1.start()
    assert paused.wait(timeout=10)

    # R1 暂停期间场景推进到版本 2（s3 end_ms 2000 -> 4000）
    extend_s3_to_4000(client, scene_id)

    # R2 到达：与同一会话的接纳串行，必须排在 R1 之后，不能抢先生效
    t2 = threading.Thread(
        target=post_accept_in_thread, args=(sid, responses, "r2")
    )
    t2.start()
    try:
        t2.join(timeout=0.5)
        assert t2.is_alive(), "R2 在 R1 的完整操作结束前抢先生效"
    finally:
        release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    # 迟到的 R1 接纳的是轮到它生效时的快照（版本 2），R2 亦然
    for key in ("r1", "r2"):
        assert responses[key].status_code == 200
        body = responses[key].json()
        assert body["source_revision"] == 2
        assert body["cursor_ms"] == 900
        assert [s["end_ms"] for s in body["subtitles"]] == [1000, 3000, 4000]
        assert body["assignments"] == [
            {"id": "s1", "track": 1},  # 已开始：固定原分轨
            {"id": "s2", "track": 2},  # 已开始：固定原分轨
            {"id": "s3", "track": 1},
        ]


def test_accept_conflict_after_interleaved_revision_leaves_session(monkeypatch):
    """交错修订导致的冲突仍返回 409，且失败不改会话也不改场景。"""
    client = TestClient(app)
    scene_id = "scene-stale"
    sid = make_stale_session(client, scene_id)

    # R1 在读取生效前被挂起；放行后读到的是版本 2
    paused, release = patch_first_scene_read(monkeypatch, reread_on_release=True)

    responses = {}
    t1 = threading.Thread(
        target=post_accept_in_thread, args=(sid, responses, "r1")
    )
    t1.start()
    assert paused.wait(timeout=10)

    # R1 暂停期间：版本 2 修改了已开始的 s1（end_ms 1000 -> 900）
    revise(client, scene_id, 1, [
        {"id": "s1", "speaker": "Alice", "start_ms": 0, "end_ms": 900},
        {"id": "s2", "speaker": "Bob", "start_ms": 800, "end_ms": 3000,
         "locked_track": 2},
        {"id": "s3", "speaker": "Alice", "start_ms": 1000, "end_ms": 2000},
    ])

    t2 = threading.Thread(
        target=post_accept_in_thread, args=(sid, responses, "r2")
    )
    t2.start()
    try:
        t2.join(timeout=0.5)
        assert t2.is_alive()
    finally:
        release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # 两个请求读到的都是版本 2：已开始字幕被修改，均冲突 409
    assert responses["r1"].status_code == 409
    assert responses["r2"].status_code == 409

    # 会话未被失败改动：仍是版本 1 快照、游标 900、原分轨
    probe = client.post(f"/sessions/{sid}/advance", json={"time_ms": 900}).json()
    assert probe["source_revision"] == 1
    assert [(a["id"], a["track"]) for a in probe["active"]] == [
        ("s1", 1),
        ("s2", 2),
    ]

    # 场景本身也不受接纳失败影响：仍是版本 2 的内容
    scene = client.get(f"/scenes/{scene_id}").json()
    assert scene["revision"] == 2
    assert [s["end_ms"] for s in scene["subtitles"]] == [900, 3000, 2000]
