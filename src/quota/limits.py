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


def tenant_key(request) -> str:
    """从请求中提取租户标识键，缺失时返回固定占位符。

    参数
    ----
    request : dict
        至少可能包含 ``"tenant"`` 字段；缺失、``None``、纯空白都按匿名处理。

    返回
    ----
    str
        非空租户键。显式租户返回 ``"tenant:" + 去首尾空白后的值``；匿名租户返回
        固定占位符 ``"__anonymous_tenant__"``。

    契约
    ----
    - 显式租户值做 ``strip``，保留大小写；内部空白不折叠。
    - 缺失、``None``、空串和纯空白一律归入同一个固定占位符，绝不能返回空串，
      否则所有匿名请求会共用一个桶。
    - 固定占位符与显式租户值处于不同命名空间：即使租户名恰好是
      ``__anonymous_tenant__``，其键也是 ``tenant:__anonymous_tenant__``，
      不会和匿名请求碰撞。
    - 本函数是纯函数，不修改 ``request``；非 dict 入参按空请求处理。字符串比较
      是精确比较，不涉及数值容差 ``1e-9``。
    """
    raise NotImplementedError("tenant_key 尚未实现，契约见 docstring")


def bucket_for(state, tenant, capacity) -> dict:
    """取出租户配额桶；不存在时按给定容量创建，返回桶本身。

    参数
    ----
    state : dict
        配额状态，至少含 ``"buckets": {}``；缺失时本函数负责建立该映射。
    tenant : str
        :func:`tenant_key` 返回的租户键。
    capacity : float
        新建桶的容量，单位是导入任务槽位，必须有限且大于 ``0``。

    返回
    ----
    dict
        ``state["buckets"][tenant]`` 的同一个对象，字段为 ``capacity``、
        ``remaining``、``updated_at``、``borrowed``、``last_charged_at``。

    契约
    ----
    - 新桶从满额开始：``remaining == capacity``，``borrowed == 0.0``，
      ``updated_at == 0``，``last_charged_at is None``。
    - 已存在的桶原样返回，不得重置余额、不得清零借用记录；即使传入的
      ``capacity`` 与桶内记录不同，也以创建时的容量为准，防止通过重复建桶
      绕过已消耗的配额。
    - 桶必须存放在调用方传入的 ``state`` 中；不得使用模块级全局字典，否则多个
      进程或两个独立状态对象会共享同一份余额。
    - 容量必须有限且 ``> 0``，否则抛 ``ValueError``。比较使用容差 ``1e-9``。
    """
    raise NotImplementedError("bucket_for 尚未实现，契约见 docstring")


def charge(state, tenant, amount, now) -> dict:
    """从租户桶扣减配额，余额不足时整笔拒绝且不扣减。

    参数
    ----
    state : dict
        :func:`bucket_for` 维护的状态。
    tenant : str
        已存在的租户键。
    amount : float
        本次要扣的槽位数，必须有限且大于 ``0``。
    now : int
        当前时刻（整毫秒），成功时记录到 ``last_charged_at``，不改变
        ``updated_at``，因此扣减本身不会吞掉尚未结算的补充时间。

    返回
    ----
    dict
        ``{"ok": bool, "charged": float, "remaining": float}``。拒绝时
        ``charged == 0.0``，``remaining`` 与调用前一致。

    契约
    ----
    - 余额 ``>= amount``（容差 ``1e-9``）时一次扣掉整笔，成功后余额不得为负，
      并把 ``last_charged_at`` 设为 ``now``。
    - 余额不足时不得扣一半、不得先扣后补、不得把余额写成负数；桶的
      ``remaining``、``borrowed``、``updated_at``、``last_charged_at``
      全部保持不变。
    - ``tenant`` 不存在时抛 ``KeyError``；``amount <= 0`` 或非有限值时抛
      ``ValueError``。
    - 本函数不补充配额；需要补充时先调用 :func:`refill`。
    """
    raise NotImplementedError("charge 尚未实现，契约见 docstring")


def refill(state, tenant, rate, now) -> dict:
    """按距离上次补充的时间差补充租户配额，并截断到容量。

    参数
    ----
    state : dict
        :func:`bucket_for` 维护的状态。
    tenant : str
        已存在的租户键。
    rate : float
        补充速率，单位“配额单位/秒”，必须有限且 ``>= 0``。
    now : int
        当前时刻（整毫秒），必须由调用方显式传入。

    返回
    ----
    dict
        ``{"remaining": float, "added": float, "updated_at": int}``。

    契约
    ----
    - 补充量只由 ``elapsed = now - updated_at`` 决定：
      ``added = rate * elapsed / 1000.0``；不能按调用次数补充，重复用同一
      ``now`` 调用必须补出 ``0.0``，否则频繁调用即可无限刷额度。
    - ``remaining = min(capacity, max(0.0, remaining + added))``，超出容量的部分
      直接丢弃；空闲再久也只能补到 ``capacity``，不允许无限增长。
    - 仅当 ``elapsed > 0`` 时把 ``updated_at`` 前移到 ``now``。``elapsed <= 0``
      （同一时刻或时钟回拨）不补额度、不倒退时间戳，避免同一段时间被反复兑换。
    - 容量与余额比较使用绝对容差 ``1e-9``；结果不得为负，也不得超过容量。
    - ``rate < 0`` 或非有限值时抛 ``ValueError``；``tenant`` 不存在时抛
      ``KeyError``。
    """
    raise NotImplementedError("refill 尚未实现，契约见 docstring")


def borrow(state, tenant, from_pool, amount) -> dict:
    """从共享池借入配额，超过比例上限或池余额时整笔拒绝。

    参数
    ----
    state : dict
        :func:`bucket_for` 维护的状态；其中的 ``"pool"`` 应与 ``from_pool``
        是同一个 dict，供 :func:`reclaim` 归还时使用。
    tenant : str
        已存在的租户键。
    from_pool : dict
        ``{"available": float, "borrow_ratio": float}``。``available`` 是共享池
        当前可借量，``borrow_ratio`` 是借用上限占租户容量的比例。
    amount : float
        本次借入量，必须有限且大于 ``0``。

    返回
    ----
    dict
        ``{"ok": bool, "borrowed": float, "remaining": float,
        "pool_available": float}``；拒绝时 ``borrowed`` 为当前已借量，其余字段
        保持调用前的值。

    契约
    ----
    - 借用上限是比例而不是绝对值：``limit = capacity * borrow_ratio``。单次可借
      上限还要同时受 ``limit - borrowed``、``available`` 以及桶的剩余容量
      ``capacity - remaining`` 约束；任何一项不足都必须整笔拒绝，不得部分借入。
      这样大租户不能靠大容量把共享池一次借空。
    - 成功时 ``from_pool["available"] -= amount``，桶 ``remaining += amount``，
      ``borrowed += amount``；三者都必须保持非负，``remaining`` 不得超过容量。
    - 必须记录借入量到 ``borrowed``；不记录会导致后续回收无法判断可归还多少。
      拒绝时 ``from_pool``、桶余额和 ``borrowed`` 均不得改变。
    - ``borrow_ratio < 0``、``available < 0``、``amount <= 0`` 或非有限值时抛
      ``ValueError``；``tenant`` 不存在时抛 ``KeyError``。比较使用容差 ``1e-9``。
    """
    raise NotImplementedError("borrow 尚未实现，契约见 docstring")


def reclaim(state, tenant, amount) -> dict:
    """把租户已借用的配额归还共享池，不能超过已借量或桶内余额。

    参数
    ----
    state : dict
        :func:`bucket_for` 维护的状态，必须包含 ``"pool"`` 共享池。
    tenant : str
        已存在的租户键。
    amount : float
        本次归还量，必须有限且大于 ``0``。

    返回
    ----
    dict
        ``{"ok": bool, "reclaimed": float, "remaining": float,
        "borrowed": float, "pool_available": float}``；拒绝时 ``reclaimed`` 为
        ``0.0``，其余字段保持调用前的值。

    契约
    ----
    - 只能归还已经借用的部分：``amount <= borrowed``（容差 ``1e-9``），也不能
      超过当前 ``remaining``，否则整笔拒绝。不允许直接给余额加一个未经借用的值，
      否则配额会越积越多并突破容量。
    - 成功时桶 ``remaining -= amount``、``borrowed -= amount``，共享池
      ``available += amount``；结果必须满足 ``0 <= remaining <= capacity`` 且
      ``borrowed >= 0``，共享池不得超过其原有可借量加归还量。
    - 拒绝时桶和共享池都不改变；本函数是 :func:`borrow` 的逆操作，不是再次
      发放配额。
    - ``amount <= 0`` 或非有限值时抛 ``ValueError``；``tenant`` 缺失或
      ``state["pool"]`` 缺失时抛 ``KeyError``。比较使用绝对容差 ``1e-9``。
    """
    raise NotImplementedError("reclaim 尚未实现，契约见 docstring")


def validate_config(config) -> list[str]:
    """检查配额配置，按固定顺序返回问题码列表；合法配置返回空列表。

    参数
    ----
    config : dict
        ``{"capacity": float, "refill_rate": float, "borrow_ratio": float,
        "reclaim_enabled": bool, "pool_available": float}``。缺失字段按非法处理，
        但本函数不抛异常，只返回问题码。

    返回
    ----
    list[str]
        按下列顺序排列且不重复的问题码：

        - ``"capacity_non_positive"``：容量缺失、非有限或 ``<= 0``，桶永远为空，
          所有请求都会被拒。
        - ``"borrow_ratio_invalid"``：借用比例缺失、非有限或小于 ``0``。
        - ``"borrow_limit_exceeds_capacity"``：``borrow_ratio > 1.0``，即借用上限
          ``capacity * borrow_ratio`` 超过租户容量，大租户可能借空共享池。
        - ``"reclaim_not_configured"``：``reclaim_enabled`` 未显式设为 ``True``，
          借出的配额没有归还路径。

    契约
    ----
    - 必须真正检查容量为零的情况；零容量不是“稍后补充”能修复的非法配置。
    - 三个量级问题相互独立，但返回顺序固定为容量、比例非法、比例超限、未设回收。
    - ``config`` 不是 dict、字段缺失或类型不可转换时，按对应问题码报告，不抛异常。
    - 本函数只校验，不修改 ``config``；数值比较使用容差 ``1e-9``。
    """
    raise NotImplementedError("validate_config 尚未实现，契约见 docstring")


def quota_batch(requests, config, now) -> dict:
    """批量判定一组租户请求；每个租户拥有独立桶，结果与逐条判定一致。

    参数
    ----
    requests : list[dict]
        ``[{"tenant": str, "amount": float}, ...]``，顺序即判定顺序。
    config : dict
        ``{"capacity": float, "refill_rate": float, "borrow_ratio": float,
        "reclaim_enabled": bool, "pool_available": float}``。
    now : int
        本批请求共用的当前时刻（整毫秒）。

    返回
    ----
    dict
        ``{"decisions": list[bool], "tenants": list[str],
        "buckets": dict, "pool": dict, "allowed": int, "rejected": int}``。
        ``decisions`` 与 ``tenants`` 和入参顺序一一对应。

    契约
    ----
    - 每次调用创建一份全新的状态（空桶表和按 ``pool_available`` 初始化的共享池），
      两批独立调用不得互相偷余额。
    - 每个唯一租户键只对应一个桶；不同租户的桶必须彼此独立。不得把前一个租户的
      桶直接复用给后一个租户，否则后一个租户会花掉前一个租户的余额。
    - 单个请求依次执行取桶、按 ``refill_rate`` 补充、按 ``amount`` 扣减；扣减不足
      时该条为 ``False`` 且不部分扣减，但后续租户继续正常判定。
    - 本函数不自动借共享池；需要借用的场景由调用方显式调用 :func:`borrow`。
      返回的 ``buckets`` 和 ``pool`` 是本次批量结束后的状态。
    - 不修改 ``requests`` 或 ``config``；空输入返回空列表和零计数。比较使用
      绝对容差 ``1e-9``。
    """
    raise NotImplementedError("quota_batch 尚未实现，契约见 docstring")
