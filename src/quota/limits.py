"""多租户导入配额：租户桶、扣减、按时间补充、共享池借用与回收。

配额单位
--------
一个配额单位是“一个可并发执行的导入任务槽位”。容量、余额、借用量和回收量
都用 ``float`` 表示，``1.0`` 就是一个任务槽；比较一律使用绝对容差 ``1e-9``。
任何余额、借用量、共享池可借量都不得为负。

时间
----
时间一律是整毫秒（``int``），由调用方显式传入。本模块不读取真实时钟、
不使用随机数、不等待，因此同一输入必须得到同一结果。

状态
----
租户配额状态形如::

    {
        "buckets": {
            "tenant:acme": {
                "capacity": 10.0,
                "remaining": 10.0,
                "updated_at": 0,
                "borrowed": 0.0,
                "last_charged_at": None,
            }
        },
        "pool": {"available": 100.0, "borrow_ratio": 0.2},
    }

``updated_at`` 是上一次成功补充的时刻，``last_charged_at`` 只用于审计最近一次
成功扣减，不参与补充计算。共享池的 ``borrow_ratio`` 是借用上限比例：租户最多
能借到 ``capacity * borrow_ratio``，不是固定绝对值，也不能把整个池子借空。

安全前提
--------
匿名租户不能挤进同一个空字符串桶；桶不能跨进程或跨状态对象共享；扣减必须
全有或全无；补充只能按真实经过的毫秒数结算并截断到容量；借用必须记录且受
比例上限约束；回收不能把余额推到容量之上；配置问题必须显式报告；批量判定
必须让每个租户拥有各自独立的桶。

只允许 Python 标准库（``math``、``collections`` 等）和 ``pytest``；离线运行。
"""

import math

TOL = 1e-9

ANONYMOUS_TENANT_KEY = "__anonymous_tenant__"


def tenant_key(request) -> str:
    """从请求中提取租户标识键，缺失时返回固定占位符。"""
    if not isinstance(request, dict):
        return ANONYMOUS_TENANT_KEY
    value = request.get("tenant")
    if not isinstance(value, str):
        return ANONYMOUS_TENANT_KEY
    stripped = value.strip()
    if not stripped:
        return ANONYMOUS_TENANT_KEY
    return "tenant:" + stripped


def bucket_for(state, tenant, capacity) -> dict:
    """取出租户配额桶；不存在时按给定容量创建，返回桶本身。"""
    if not isinstance(capacity, (int, float)) or isinstance(capacity, bool):
        raise ValueError("capacity 必须是有限正数")
    capacity = float(capacity)
    if not math.isfinite(capacity) or capacity <= 0.0:
        raise ValueError("capacity 必须是有限正数")

    buckets = state.setdefault("buckets", {})
    bucket = buckets.get(tenant)
    if bucket is None:
        bucket = {
            "capacity": capacity,
            "remaining": capacity,
            "updated_at": 0,
            "borrowed": 0.0,
            "last_charged_at": None,
        }
        buckets[tenant] = bucket
    return bucket


def charge(state, tenant, amount, now) -> dict:
    """从租户桶扣减配额，余额不足时整笔拒绝且不扣减。"""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ValueError("amount 必须是有限正数")
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0.0:
        raise ValueError("amount 必须是有限正数")

    bucket = state["buckets"][tenant]
    remaining = bucket["remaining"]
    if remaining + TOL < amount:
        return {"ok": False, "charged": 0.0, "remaining": remaining}

    new_remaining = remaining - amount
    if new_remaining < 0.0:
        new_remaining = 0.0
    bucket["remaining"] = new_remaining
    bucket["last_charged_at"] = now
    return {"ok": True, "charged": amount, "remaining": new_remaining}


def refill(state, tenant, rate, now) -> dict:
    """按距离上次补充的时间差补充租户配额，并截断到容量。"""
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        raise ValueError("rate 必须是有限非负数")
    rate = float(rate)
    if not math.isfinite(rate) or rate < 0.0:
        raise ValueError("rate 必须是有限非负数")

    bucket = state["buckets"][tenant]
    elapsed = now - bucket["updated_at"]
    if elapsed <= 0:
        return {
            "remaining": bucket["remaining"],
            "added": 0.0,
            "updated_at": bucket["updated_at"],
        }

    added = rate * elapsed / 1000.0
    capacity = bucket["capacity"]
    new_remaining = bucket["remaining"] + added
    if new_remaining < 0.0:
        new_remaining = 0.0
    if new_remaining > capacity:
        new_remaining = capacity
    actual_added = new_remaining - bucket["remaining"]
    if abs(actual_added) <= TOL:
        actual_added = 0.0
        new_remaining = bucket["remaining"]

    bucket["remaining"] = new_remaining
    bucket["updated_at"] = now
    return {"remaining": new_remaining, "added": actual_added, "updated_at": now}


def borrow(state, tenant, from_pool, amount) -> dict:
    """从共享池借入配额，超过比例上限或池余额时整笔拒绝。"""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ValueError("amount 必须是有限正数")
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0.0:
        raise ValueError("amount 必须是有限正数")

    borrow_ratio = from_pool["borrow_ratio"]
    available = from_pool["available"]
    if not math.isfinite(borrow_ratio) or borrow_ratio < 0.0:
        raise ValueError("borrow_ratio 必须是有限非负数")
    if not math.isfinite(available) or available < 0.0:
        raise ValueError("available 必须是有限非负数")

    bucket = state["buckets"][tenant]
    capacity = bucket["capacity"]
    limit = capacity * borrow_ratio
    headroom = limit - bucket["borrowed"]
    space = capacity - bucket["remaining"]

    if (
        amount > headroom + TOL
        or amount > available + TOL
        or amount > space + TOL
    ):
        return {
            "ok": False,
            "borrowed": bucket["borrowed"],
            "remaining": bucket["remaining"],
            "pool_available": available,
        }

    bucket["borrowed"] += amount
    bucket["remaining"] += amount
    from_pool["available"] = available - amount
    return {
        "ok": True,
        "borrowed": bucket["borrowed"],
        "remaining": bucket["remaining"],
        "pool_available": from_pool["available"],
    }


def reclaim(state, tenant, amount) -> dict:
    """把租户已借用的配额归还共享池，不能超过已借量或桶内余额。"""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ValueError("amount 必须是有限正数")
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0.0:
        raise ValueError("amount 必须是有限正数")

    bucket = state["buckets"][tenant]
    pool = state["pool"]

    if amount > bucket["borrowed"] + TOL or amount > bucket["remaining"] + TOL:
        return {
            "ok": False,
            "reclaimed": 0.0,
            "remaining": bucket["remaining"],
            "borrowed": bucket["borrowed"],
            "pool_available": pool["available"],
        }

    bucket["remaining"] -= amount
    bucket["borrowed"] -= amount
    if bucket["remaining"] < 0.0:
        bucket["remaining"] = 0.0
    if bucket["borrowed"] < 0.0:
        bucket["borrowed"] = 0.0
    pool["available"] += amount
    return {
        "ok": True,
        "reclaimed": amount,
        "remaining": bucket["remaining"],
        "borrowed": bucket["borrowed"],
        "pool_available": pool["available"],
    }


def validate_config(config) -> list[str]:
    """检查配额配置，按固定顺序返回问题码列表；合法配置返回空列表。"""
    problems = []

    capacity = None
    borrow_ratio = None
    reclaim_enabled = None
    if isinstance(config, dict):
        capacity = config.get("capacity")
        borrow_ratio = config.get("borrow_ratio")
        reclaim_enabled = config.get("reclaim_enabled")

    capacity_ok = (
        isinstance(capacity, (int, float))
        and not isinstance(capacity, bool)
        and math.isfinite(capacity)
        and capacity > 0.0
    )
    if not capacity_ok:
        problems.append("capacity_non_positive")

    ratio_ok = (
        isinstance(borrow_ratio, (int, float))
        and not isinstance(borrow_ratio, bool)
        and math.isfinite(borrow_ratio)
        and borrow_ratio >= 0.0
    )
    if not ratio_ok:
        problems.append("borrow_ratio_invalid")
    elif borrow_ratio > 1.0 + TOL:
        problems.append("borrow_limit_exceeds_capacity")

    if reclaim_enabled is not True:
        problems.append("reclaim_not_configured")

    return problems


def quota_batch(requests, config, now) -> dict:
    """批量判定一组租户请求；每个租户拥有独立桶，结果与逐条判定一致。"""
    state = {
        "buckets": {},
        "pool": {
            "available": float(config["pool_available"]),
            "borrow_ratio": float(config["borrow_ratio"]),
        },
    }

    decisions = []
    tenants = []
    for request in requests:
        key = tenant_key(request)
        bucket_for(state, key, config["capacity"])
        refill(state, key, config["refill_rate"], now)
        result = charge(state, key, request["amount"], now)
        decisions.append(result["ok"])
        tenants.append(key)

    return {
        "decisions": decisions,
        "tenants": tenants,
        "buckets": state["buckets"],
        "pool": state["pool"],
        "allowed": sum(1 for ok in decisions if ok),
        "rejected": sum(1 for ok in decisions if not ok),
    }
