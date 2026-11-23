"""多租户导入配额的行为契约测试（恰好 15 个，初始全红）。

前 10 个用例分别对应题面中的 10 个缺陷，后 5 个是基础回归与一致性用例。
所有时间都由调用方显式传入，不使用真实时钟、不联网、不随机；浮点比较统一使用
``abs=1e-9``。tests 目录不可修改。
"""

import pytest

from quota.limits import (
    borrow,
    bucket_for,
    charge,
    quota_batch,
    reclaim,
    refill,
    tenant_key,
    validate_config,
)

TOL = 1e-9


def _cfg(
    capacity=10.0,
    refill_rate=1.0,
    borrow_ratio=0.2,
    reclaim_enabled=True,
    pool_available=100.0,
):
    return {
        "capacity": capacity,
        "refill_rate": refill_rate,
        "borrow_ratio": borrow_ratio,
        "reclaim_enabled": reclaim_enabled,
        "pool_available": pool_available,
    }


def _pool(available=100.0, borrow_ratio=0.2):
    return {"available": available, "borrow_ratio": borrow_ratio}


def _state(available=100.0, borrow_ratio=0.2):
    return {"buckets": {}, "pool": _pool(available, borrow_ratio)}


def test_defect_01_missing_tenant_uses_fixed_placeholder():
    """缺陷 1：缺失租户标识不能返回空串，否则所有匿名请求共用一个桶。"""
    missing = tenant_key({})
    assert missing == tenant_key({"tenant": None})
    assert missing == tenant_key({"tenant": ""})
    assert missing == tenant_key({"tenant": "   "})
    assert isinstance(missing, str)
    assert missing != ""

    assert tenant_key({"tenant": " acme "}) == "tenant:acme"
    assert tenant_key({"tenant": "ACME"}) == "tenant:ACME"
    # 显式租户名即使与匿名占位符同名，也不能和匿名请求碰撞
    assert tenant_key({"tenant": "__anonymous_tenant__"}) != missing


def test_defect_02_buckets_are_not_process_global():
    """缺陷 2：桶不能放在进程级全局字典里，两个独立状态必须互不影响。"""
    state_a = _state()
    state_b = _state()

    bucket_a = bucket_for(state_a, "tenant:acme", 5.0)
    assert charge(state_a, "tenant:acme", 3.0, 0)["ok"] is True
    assert bucket_a["remaining"] == pytest.approx(2.0, abs=TOL)

    bucket_b = bucket_for(state_b, "tenant:acme", 5.0)
    assert bucket_b is not bucket_a
    assert bucket_b["remaining"] == pytest.approx(5.0, abs=TOL)
    assert state_b["buckets"]["tenant:acme"]["remaining"] == pytest.approx(5.0, abs=TOL)
    assert state_a["buckets"]["tenant:acme"]["remaining"] == pytest.approx(2.0, abs=TOL)


def test_defect_03_insufficient_charge_does_not_partially_deduct():
    """缺陷 3：余额不足时必须整笔拒绝，不能扣一半或把余额写成负数。"""
    state = _state()
    bucket = bucket_for(state, "tenant:acme", 10.0)
    bucket["remaining"] = 0.5
    before = dict(bucket)

    result = charge(state, "tenant:acme", 1.0, 10)

    assert result["ok"] is False
    assert result["charged"] == pytest.approx(0.0, abs=TOL)
    assert result["remaining"] == pytest.approx(0.5, abs=TOL)
    assert bucket["remaining"] == pytest.approx(0.5, abs=TOL)
    assert bucket["remaining"] >= 0.0
    assert bucket == before


def test_defect_04_refill_uses_elapsed_time_not_call_count():
    """缺陷 4：补充只按时间差结算，反复调用不能反复补出额度。"""
    state = _state()
    bucket = bucket_for(state, "tenant:acme", 10.0)
    bucket["remaining"] = 0.0
    bucket["updated_at"] = 0

    for _ in range(5):
        result = refill(state, "tenant:acme", 1.0, 0)
        assert result["added"] == pytest.approx(0.0, abs=TOL)
        assert result["remaining"] == pytest.approx(0.0, abs=TOL)

    half = refill(state, "tenant:acme", 1.0, 500)
    assert half["added"] == pytest.approx(0.5, abs=TOL)
    assert half["remaining"] == pytest.approx(0.5, abs=TOL)

    # 同一时刻再调用一次，不能把 500ms 再兑换一次
    same = refill(state, "tenant:acme", 1.0, 500)
    assert same["added"] == pytest.approx(0.0, abs=TOL)
    assert same["remaining"] == pytest.approx(0.5, abs=TOL)

    later = refill(state, "tenant:acme", 1.0, 1500)
    assert later["added"] == pytest.approx(1.0, abs=TOL)
    assert later["remaining"] == pytest.approx(1.5, abs=TOL)


def test_defect_05_refill_is_clamped_to_capacity():
    """缺陷 5：补充不截断会让余额无限增长，空闲再久也只能补到容量。"""
    state = _state()
    bucket = bucket_for(state, "tenant:acme", 10.0)
    bucket["remaining"] = 9.0
    bucket["updated_at"] = 0

    filled = refill(state, "tenant:acme", 1000.0, 1_000_000)
    assert filled["remaining"] == pytest.approx(10.0, abs=TOL)
    assert filled["remaining"] <= 10.0 + TOL

    for _ in range(5):
        again = refill(state, "tenant:acme", 1000.0, 1_000_000)
        assert again["added"] == pytest.approx(0.0, abs=TOL)
        assert again["remaining"] == pytest.approx(10.0, abs=TOL)
        assert again["remaining"] <= 10.0 + TOL


def test_defect_06_borrow_limit_scales_with_capacity():
    """缺陷 6：借用上限必须按容量比例，大租户不能借走全部共享池。"""
    pool = _pool(available=100.0, borrow_ratio=0.2)
    state = {"buckets": {}, "pool": pool}

    small = bucket_for(state, "tenant:small", 5.0)
    small["remaining"] = 0.0
    rejected = borrow(state, "tenant:small", pool, 2.0)
    assert rejected["ok"] is False
    assert small["borrowed"] == pytest.approx(0.0, abs=TOL)
    assert pool["available"] == pytest.approx(100.0, abs=TOL)

    large = bucket_for(state, "tenant:large", 50.0)
    large["remaining"] = 0.0
    # 比例上限是 50 * 0.2 = 10，不能一次借 50 把池子搬空
    too_much = borrow(state, "tenant:large", pool, 50.0)
    assert too_much["ok"] is False
    assert pool["available"] == pytest.approx(100.0, abs=TOL)

    accepted = borrow(state, "tenant:large", pool, 10.0)
    assert accepted["ok"] is True
    assert accepted["borrowed"] == pytest.approx(10.0, abs=TOL)
    assert accepted["remaining"] == pytest.approx(10.0, abs=TOL)
    assert pool["available"] == pytest.approx(90.0, abs=TOL)


def test_defect_07_borrow_is_recorded_and_reclaim_is_bounded():
    """缺陷 7：借用必须记入 borrowed，归还不能超过已借量。"""
    pool = _pool(available=10.0, borrow_ratio=0.5)
    state = {"buckets": {}, "pool": pool}
    bucket = bucket_for(state, "tenant:acme", 10.0)
    bucket["remaining"] = 0.0

    borrowed = borrow(state, "tenant:acme", pool, 4.0)
    assert borrowed["ok"] is True
    assert bucket["borrowed"] == pytest.approx(4.0, abs=TOL)
    assert bucket["remaining"] == pytest.approx(4.0, abs=TOL)
    assert pool["available"] == pytest.approx(6.0, abs=TOL)

    over = reclaim(state, "tenant:acme", 5.0)
    assert over["ok"] is False
    assert over["reclaimed"] == pytest.approx(0.0, abs=TOL)
    assert bucket["remaining"] == pytest.approx(4.0, abs=TOL)
    assert bucket["borrowed"] == pytest.approx(4.0, abs=TOL)
    assert pool["available"] == pytest.approx(6.0, abs=TOL)

    returned = reclaim(state, "tenant:acme", 4.0)
    assert returned["ok"] is True
    assert bucket["remaining"] == pytest.approx(0.0, abs=TOL)
    assert bucket["borrowed"] == pytest.approx(0.0, abs=TOL)
    assert bucket["remaining"] <= bucket["capacity"] + TOL
    assert pool["available"] == pytest.approx(10.0, abs=TOL)


def test_defect_08_reclaim_cannot_push_balance_above_capacity():
    """缺陷 8：回收不能直接加值，余额不得因回收突破容量。"""
    pool = _pool(available=5.0, borrow_ratio=0.5)
    state = {"buckets": {}, "pool": pool}
    bucket = bucket_for(state, "tenant:acme", 10.0)
    bucket["remaining"] = 9.0
    bucket["borrowed"] = 5.0

    result = reclaim(state, "tenant:acme", 5.0)

    assert result["ok"] is True
    assert result["reclaimed"] == pytest.approx(5.0, abs=TOL)
    assert bucket["remaining"] == pytest.approx(4.0, abs=TOL)
    assert bucket["remaining"] <= 10.0 + TOL
    assert bucket["borrowed"] == pytest.approx(0.0, abs=TOL)
    assert pool["available"] == pytest.approx(10.0, abs=TOL)


def test_defect_09_validate_config_rejects_zero_capacity():
    """缺陷 9：配置校验必须发现零容量、借用上限超过容量和未设回收。"""
    config = _cfg()
    assert validate_config(config) == []

    assert validate_config({**config, "capacity": 0.0}) == [
        "capacity_non_positive"
    ]

    assert validate_config({**config, "borrow_ratio": 1.5}) == [
        "borrow_limit_exceeds_capacity"
    ]

    without_reclaim = {
        key: value for key, value in config.items() if key != "reclaim_enabled"
    }
    assert validate_config(without_reclaim) == ["reclaim_not_configured"]


def test_defect_10_batch_keeps_tenants_isolated():
    """缺陷 10：批量判定不能复用桶，后一个租户不能花前一个租户的余额。"""
    config = _cfg(capacity=1.0, refill_rate=1.0)
    requests = [
        {"tenant": "a", "amount": 1.0},
        {"tenant": "b", "amount": 1.0},
        {"tenant": "a", "amount": 1.0},
    ]

    result = quota_batch(requests, config, 0)

    assert result["decisions"] == [True, True, False]
    assert result["tenants"] == ["tenant:a", "tenant:b", "tenant:a"]
    assert result["allowed"] == 2
    assert result["rejected"] == 1
    assert len(result["buckets"]) == 2
    assert result["buckets"]["tenant:a"]["remaining"] == pytest.approx(0.0, abs=TOL)
    assert result["buckets"]["tenant:b"]["remaining"] == pytest.approx(0.0, abs=TOL)


def test_extra_01_first_request_creates_bucket_and_charges():
    """额外用例：首次请求建立满额桶，并成功扣减。"""
    state = _state()
    assert state["buckets"] == {}

    bucket = bucket_for(state, "tenant:acme", 5.0)
    result = charge(state, "tenant:acme", 2.0, 7)

    assert len(state["buckets"]) == 1
    assert bucket["capacity"] == pytest.approx(5.0, abs=TOL)
    assert bucket["remaining"] == pytest.approx(3.0, abs=TOL)
    assert bucket["last_charged_at"] == 7
    assert result["ok"] is True
    assert result["charged"] == pytest.approx(2.0, abs=TOL)
    assert result["remaining"] == pytest.approx(3.0, abs=TOL)


def test_extra_02_insufficient_balance_rejected_and_unchanged():
    """额外用例：余额不足被拒后余额不变，攒够后可以正常扣减。"""
    state = _state()
    bucket = bucket_for(state, "tenant:acme", 2.0)
    bucket["remaining"] = 0.75

    rejected = charge(state, "tenant:acme", 1.0, 10)
    assert rejected["ok"] is False
    assert rejected["remaining"] == pytest.approx(0.75, abs=TOL)
    assert bucket["remaining"] == pytest.approx(0.75, abs=TOL)

    accepted = charge(state, "tenant:acme", 0.75, 11)
    assert accepted["ok"] is True
    assert accepted["remaining"] == pytest.approx(0.0, abs=TOL)
    assert bucket["remaining"] == pytest.approx(0.0, abs=TOL)
    assert bucket["remaining"] >= 0.0


def test_extra_03_refill_does_not_exceed_capacity():
    """额外用例：一次长时间补充后余额也不超过容量。"""
    state = _state()
    bucket = bucket_for(state, "tenant:acme", 5.0)
    bucket["remaining"] = 4.5
    bucket["updated_at"] = 0

    result = refill(state, "tenant:acme", 10.0, 1000)

    assert result["added"] == pytest.approx(0.5, abs=TOL)
    assert result["remaining"] == pytest.approx(5.0, abs=TOL)
    assert result["remaining"] <= 5.0 + TOL
    assert bucket["remaining"] == pytest.approx(5.0, abs=TOL)


def test_extra_04_borrow_over_limit_rejected():
    """额外用例：超过借用上限的请求被拒，桶和共享池都不改变。"""
    pool = _pool(available=20.0, borrow_ratio=0.2)
    state = {"buckets": {}, "pool": pool}
    bucket = bucket_for(state, "tenant:acme", 10.0)
    bucket["remaining"] = 0.0

    rejected = borrow(state, "tenant:acme", pool, 3.0)
    assert rejected["ok"] is False
    assert bucket["remaining"] == pytest.approx(0.0, abs=TOL)
    assert bucket["borrowed"] == pytest.approx(0.0, abs=TOL)
    assert pool["available"] == pytest.approx(20.0, abs=TOL)

    accepted = borrow(state, "tenant:acme", pool, 2.0)
    assert accepted["ok"] is True
    assert bucket["remaining"] == pytest.approx(2.0, abs=TOL)
    assert bucket["borrowed"] == pytest.approx(2.0, abs=TOL)
    assert pool["available"] == pytest.approx(18.0, abs=TOL)


def test_extra_05_batch_matches_individual_judgements():
    """额外用例：两批独立判定互不影响，且与逐条单独判定结果一致。"""
    config = _cfg(capacity=2.0, refill_rate=1.0)
    requests = [
        {"tenant": "a", "amount": 2.0},
        {"tenant": "b", "amount": 2.0},
        {"tenant": "a", "amount": 1.0},
    ]

    batch = quota_batch(requests, config, 0)
    assert batch["decisions"] == [True, True, False]

    manual_state = _state()
    manual = []
    for request in requests:
        key = tenant_key(request)
        bucket_for(manual_state, key, config["capacity"])
        refill(manual_state, key, config["refill_rate"], 0)
        manual.append(charge(manual_state, key, request["amount"], 0)["ok"])

    assert manual == batch["decisions"]

    # 第二批使用全新状态，不应被第一批已经耗尽的 tenant:a 影响
    second = quota_batch([{"tenant": "a", "amount": 2.0}], config, 0)
    assert second["decisions"] == [True]
    assert second["buckets"]["tenant:a"]["remaining"] == pytest.approx(0.0, abs=TOL)
