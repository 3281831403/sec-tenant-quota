# sec-tenant-quota：多租户导入配额为什么被一个租户占死

## 题面

一个租户把导入任务刷满之后，整台机器的队列被占死，其他租户全部排队：租户标识缺失时返回空串，所有匿名请求共用一个桶；桶按进程全局共享，多进程下配额被放大；扣减在不足时仍扣一半，余额变负；补充按调用次数而不是时间差，频繁调用可无限刷；补充不截断到容量，余额可无限增长；借用上限用绝对值而不是比例，大租户可借走全部共享池；借用不记录，归还时可以超出容量；回收直接加值不校验上限，配额越积越多；配置校验不检查容量为零，所有请求永远拒绝；批量判定复用桶状态，后一个租户用前一个租户的余额。quota/limits.py 是核心，tests/test_limits.py 15个红。要修的是：按上面的顺序把 10 个缺陷逐个修掉。

## 运行方式

在仓库根目录执行：

```bat
cd D:\gsb-20261119\sec-tenant-quota
python -m pytest tests -q
```

初始状态必须恰好是 `15 failed`，并且不能出现 collection、import 或 syntax error。8 个函数全部 `raise NotImplementedError`，因此 15 个用例都会在进入断言前直接失败。把 `src/quota/limits.py` 按契约实现后，应当变为 `15 passed`。

只调试一个用例：

```bat
python -m pytest tests/test_limits.py -q -k tenant_key
python -m pytest "tests/test_limits.py::test_extra_01_first_request_creates_bucket_and_charges" -q
```

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 安全 |
| 子方向 | 漏洞检测与攻防 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 文件结构

```text
sec-tenant-quota/
├── README.md
├── pyproject.toml
├── src/
│   └── quota/
│       ├── __init__.py
│       └── limits.py          # 唯一实现文件，恰好 8 个待实现函数
└── tests/
    ├── conftest.py            # 把仓库根的 src 目录插入 sys.path
    └── test_limits.py         # 恰好 15 个用例，不可修改
```

## src 布局说明

本仓库采用 src 布局：实现包位于 `src/quota/`，而不是仓库根目录下的 `quota/`。`tests/conftest.py` 会把仓库根的 `src` 目录插入 `sys.path`，所以测试可以直接 `from quota.limits import ...`，不需要先执行 `pip install`，也不会从仓库根误导入同名模块。`pyproject.toml` 只声明项目元数据和 pytest 配置，不引入构建期或运行期第三方依赖。

## 契约要点

- 配额单位是导入任务槽位：`1.0` 表示一个可并发执行的导入任务槽。容量、余额、借用量与回收量都用 `float` 表示。
- 时间一律是整毫秒（`int`），由调用方显式传入；不调用 `time.time()`，不使用随机数，不等待真实时钟。
- 余额与借用量比较使用绝对容差 `1e-9`；任何输出都不得为负。
- 状态形如：

  ```python
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
  ```

- 借用上限是租户容量乘以共享池的 `borrow_ratio`，不是固定绝对值；借用会记入 `borrowed`，回收时只能归还已经借用的部分。
- 只允许 Python 标准库（`math`、`collections` 等）与 `pytest`：离线、不联网、不引入第三方包。

## 约束

1. `tests/` 不可修改：不得增删用例、不得改断言或容差、不得跳过；只能改 `src/quota/limits.py`。
2. 8 个函数的签名与 docstring 是验收契约，不要改签名，也不要改 `src/quota/__init__.py` 的用途。
3. 这是 src 布局，`tests/conftest.py` 负责路径；不要改成在仓库根放 `quota/`，也不要依赖安装后的包。
4. `src/quota/limits.py` 的 docstring 写明了每个函数的边界语义：匿名租户占位、按时间补充、全有或全无扣减、比例借用上限、回收不超过容量、配置问题码与逐租户批量隔离。
