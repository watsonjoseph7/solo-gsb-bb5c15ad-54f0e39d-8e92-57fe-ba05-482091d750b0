"""结算模块测试：POST/GET /events/{event_id}/settlement。

SQLite 文件库 + 模拟时钟（与 test_extend.py 相同手法），覆盖：
  - closed 后提交非空标准结果：200，正确/错误/未揭示计数按揭示原文精确匹配
  - 精确匹配语义：区分大小写、不去除空白（前后空格 / 大小写不同均算错）
  - commit / reveal 阶段结算 -> 409 SETTLEMENT_NOT_CLOSED，状态不变
  - 重复结算（相同或不同标准结果）-> 409 SETTLEMENT_ALREADY_CONFIRMED，
    既有结算（结果、计数、确认时间）不被改写
  - 查询接口：未结算返回 200 status=pending 且各字段为 null；结算后返回确认值
  - 空 / 缺失标准结果 -> 422；事件不存在 -> 404；非 UUID -> 422
  - 结算出现在时间线中（settlement_confirmed）
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_settlement_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/settlement.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402
from app.models import Settlement  # noqa: E402
from uuid import UUID  # noqa: E402

T0 = datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)

# 三类参与者：一个揭示为正确原文、一个揭示为错误原文、一个承诺永不揭示
CORRECT_TEXT = "YES"
WRONG_TEXT = "NO"
UNREVEALED_TEXT = "MAYBE"
SALTS = {"yes": "salt-yes", "no": "salt-no", "maybe": "salt-maybe"}


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


clock = MutableClock(T0)

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.t = T0
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


@pytest.fixture
def closed_event_id() -> str:
    """创建事件、提交 3 个承诺，closed 后揭示其中 2 个（正确/错误各一）。"""
    r = client.post("/events", json={
        "title": "待结算事件",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    assert r.status_code == 201, r.text
    eid = r.json()["id"]

    for text, salt in [
        (CORRECT_TEXT, SALTS["yes"]),
        (WRONG_TEXT, SALTS["no"]),
        (UNREVEALED_TEXT, SALTS["maybe"]),
    ]:
        r = client.post(
            f"/events/{eid}/commitments",
            json={"commitment_hash": compute_commitment(text, salt)},
        )
        assert r.status_code == 201, r.text

    clock.t = COMMIT_DL  # 进入 reveal 阶段
    for text, salt in [(CORRECT_TEXT, SALTS["yes"]), (WRONG_TEXT, SALTS["no"])]:
        r = client.post(
            f"/events/{eid}/reveals",
            json={"original_text": text, "salt": salt},
        )
        assert r.status_code == 201, r.text

    clock.t = REVEAL_DL  # 进入 closed 阶段
    return eid


def settle(event_id: str, result: str, expected: int = 200):
    r = client.post(
        f"/events/{event_id}/settlement",
        json={"standard_result": result},
    )
    assert r.status_code == expected, r.text
    return r


def settlement_count(event_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(Settlement).filter(Settlement.event_id == UUID(event_id)).count()
    finally:
        db.close()


def test_settle_closed_returns_exact_match_counts(closed_event_id):
    r = settle(closed_event_id, CORRECT_TEXT)
    body = r.json()
    assert body["event_id"] == closed_event_id
    assert body["status"] == "confirmed"
    assert body["standard_result"] == CORRECT_TEXT
    assert body["correct_count"] == 1
    assert body["incorrect_count"] == 1
    assert body["unrevealed_count"] == 1
    assert body["total_count"] == 3
    assert "settled_at" in body and body["settled_at"] is not None
    assert settlement_count(closed_event_id) == 1


@pytest.mark.parametrize("result,case", [
    (" yes", "前导空格"),
    ("yes ", "尾随空格"),
    ("yes", "大小写不同"),
    (" YES ", "前后空格且大小写不同"),
    ("", "空串"),
])
def test_exact_match_is_case_sensitive_and_untrimmed(result, case):
    # 单独构造：只揭示一个原文恰为 "YES" 的承诺
    r = client.post("/events", json={
        "title": f"精确匹配-{case}",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    eid = r.json()["id"]
    r = client.post(
        f"/events/{eid}/commitments",
        json={"commitment_hash": compute_commitment(CORRECT_TEXT, "s")},
    )
    assert r.status_code == 201
    clock.t = COMMIT_DL
    r = client.post(
        f"/events/{eid}/reveals",
        json={"original_text": CORRECT_TEXT, "salt": "s"},
    )
    assert r.status_code == 201
    clock.t = REVEAL_DL

    if result == "":
        # 空串由请求体校验拦截（422），不产生写入
        r = client.post(f"/events/{eid}/settlement", json={"standard_result": result})
        assert r.status_code == 422
        return

    body = settle(eid, result).json()
    assert body["correct_count"] == 0, case
    assert body["incorrect_count"] == 1, case
    assert body["unrevealed_count"] == 0, case


def test_settle_event_with_no_commitments(closed_event_id):
    clock.t = T0  # 新事件的截止时间须在创建时刻之后
    r = client.post("/events", json={
        "title": "无承诺事件",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    eid = r.json()["id"]
    clock.t = REVEAL_DL
    body = settle(eid, "ANY").json()
    assert body["correct_count"] == 0
    assert body["incorrect_count"] == 0
    assert body["unrevealed_count"] == 0
    assert body["total_count"] == 0


@pytest.mark.parametrize("phase,at,code", [
    ("commit", T0 + timedelta(minutes=30), "SETTLEMENT_NOT_CLOSED"),
    ("reveal", COMMIT_DL, "SETTLEMENT_NOT_CLOSED"),
])
def test_settle_before_closed_conflicts(phase, at, code):
    r = client.post("/events", json={
        "title": f"过早结算-{phase}",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    eid = r.json()["id"]
    clock.t = at
    r = settle(eid, CORRECT_TEXT, expected=409)
    assert r.json()["detail"]["code"] == code
    # 未结算状态不变
    r = client.get(f"/events/{eid}/settlement")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert settlement_count(eid) == 0


def test_duplicate_settlement_conflicts_and_never_overwrites(closed_event_id):
    first = settle(closed_event_id, CORRECT_TEXT).json()

    # 相同结果重复结算 -> 409
    r = settle(closed_event_id, CORRECT_TEXT, expected=409)
    assert r.json()["detail"]["code"] == "SETTLEMENT_ALREADY_CONFIRMED"

    # 不同结果重复结算 -> 同样 409，且既有结果与计数不被改写
    r = settle(closed_event_id, WRONG_TEXT, expected=409)
    assert r.json()["detail"]["code"] == "SETTLEMENT_ALREADY_CONFIRMED"

    clock.t = REVEAL_DL + timedelta(days=1)  # 无论时钟推进多久
    r = settle(closed_event_id, WRONG_TEXT, expected=409)
    assert r.json()["detail"]["code"] == "SETTLEMENT_ALREADY_CONFIRMED"

    assert settlement_count(closed_event_id) == 1
    r = client.get(f"/events/{closed_event_id}/settlement")
    stored = r.json()
    assert stored["status"] == "confirmed"
    assert stored["standard_result"] == CORRECT_TEXT
    assert (stored["correct_count"], stored["incorrect_count"], stored["unrevealed_count"]) == (1, 1, 1)
    assert stored["settled_at"] == first["settled_at"]


def test_get_settlement_pending_then_confirmed():
    r = client.post("/events", json={
        "title": "查询结算状态",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    eid = r.json()["id"]

    # 未结算（即便仍在 commit / reveal 阶段）：明确 pending 状态
    r = client.get(f"/events/{eid}/settlement")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "event_id": eid,
        "status": "pending",
        "standard_result": None,
        "settled_at": None,
        "correct_count": None,
        "incorrect_count": None,
        "unrevealed_count": None,
        "total_count": None,
    }

    clock.t = REVEAL_DL
    settle(eid, "DONE")

    r = client.get(f"/events/{eid}/settlement")
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["standard_result"] == "DONE"
    assert body["settled_at"] is not None
    assert body["total_count"] == 0


def test_settle_empty_or_missing_body_returns_422(closed_event_id):
    r = client.post(f"/events/{closed_event_id}/settlement", json={"standard_result": ""})
    assert r.status_code == 422
    r = client.post(f"/events/{closed_event_id}/settlement", json={})
    assert r.status_code == 422
    assert settlement_count(closed_event_id) == 0


def test_settle_unknown_event_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(f"/events/{missing}/settlement", json={"standard_result": "X"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"
    r = client.get(f"/events/{missing}/settlement")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_settle_non_uuid_returns_422():
    r = client.post("/events/not-a-uuid/settlement", json={"standard_result": "X"})
    assert r.status_code == 422
    r = client.get("/events/not-a-uuid/settlement")
    assert r.status_code == 422


def test_settlement_appears_in_timeline(closed_event_id):
    settle(closed_event_id, CORRECT_TEXT)
    r = client.get(f"/events/{closed_event_id}/timeline")
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    types = [e["type"] for e in entries]
    assert types[-1] == "settlement_confirmed"
    detail = entries[-1]["detail"]
    assert detail["standard_result"] == CORRECT_TEXT
    assert (detail["correct_count"], detail["incorrect_count"], detail["unrevealed_count"]) == (1, 1, 1)
    assert "settlement_id" in detail
