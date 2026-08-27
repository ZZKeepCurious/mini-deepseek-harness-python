# 第 15 章：schemastery 配置引擎

> 对应 dsh 真实源码：`vendor/schemastery/src/index.ts`（902 行单文件）
> 前置：第 2 章（Context 服务仓库与插件配置解析）、第 13 章（`_resolve_config` 消费 schemastery 产出的规格）。产出文件：`miniharness/core/schema.py`（完整移植）。

## 15.1 这一章要做什么

第 13 章提到 `Service._resolve_config` 把 intercept 配置合并成一份 dict——但"配置"从哪来、长什么样、怎么保证插件作者填的不是乱码？dsh 用一个独立的配置 schema 引擎 **schemastery**（vendored，对齐 `@deepseek-ai/schemastery`）来回答：

- **声明式 schema**：用 `S.string()` / `S.object({...})` / `S.union([...])` 描述一份配置的形状；
- **解析即校验 + 规整**：`Schema.resolve(data, schema)`（或实例 `schema(data)`）同时做类型检查、默认值注入、范围/步长约束、宽松模式改写，返回规整后的值（`resolve` 返回 `[value]` 单元素列表，`__call__` 取 `[0]`）或带 `$path` 前缀的 `SchemaValidationError`；
- **可序列化**：`schema.toJSON()`（mini 为 `to_json()`）产出 JSON-Schema 风格的规格，`schema.toString()`（mini 为 `to_string()`）产出人类可读描述，供 config-doc / web 配置面消费；
- **`~standard` 协议**：schemastery 的 `toJSON` 形状正是 Cordis `resolveConfig` 用来生成插件配置 UI 的契约（上游 `cordis fiber.ts` 的 `resolveConfig` 消费它）。

它已经在 mini 里**全量对齐**，但此前只在 `architecture.md` 映射行 + 报告概览里出现，没有逐机制解读——本章补上。

## 15.2 概念：Schema 是可调用节点 + 分发器

schemastery 的核心只有一个对象：**`Schema`**。它同时是：

| 角色 | 说明 |
|---|---|
| **可调用节点** | `S.string()` 返回一棵 Schema 树；`schema(data, options)` 解析一次值 |
| **分发器** | `Schema.resolve(data, schema, options, strict)` 按 `schema.type` 派发到 17 类 resolver |
| **构建器** | `schema.required()` / `.default(x)` / `.min(0)` 等方法返回**新** Schema（克隆语义，不修改原节点） |

为什么需要"17 类 resolver + 一个分发器"而不是一堆 `validate_xxx` 函数？因为 schema 是递归嵌套的（object 的字段又是 schema，array 的 item 又是 schema），统一走 `Schema.resolve` 递归下降，配合 `Options`（autofix / ignore / path / strict）就能在任意深度上一致地处理默认值、宽松改写、错误路径前缀。

## 15.3 代码 step-by-step

### 步骤 1：Schema 节点 + resolve 分发

```python
class Schema:
    def __init__(self, options):
        self.uid = ...
        self.type = options["type"]
        self.meta = options.get("meta") or {}
        self.inner = options.get("inner")
        self.dict = options.get("dict")
        self.list = options.get("list")
        ...
    def __call__(self, data, options=None):
        return Schema.resolve(data, self, options or {})[0]   # resolve 返回 [value] 单元素列表，取 [0]
```

`Schema.resolve` 是分发中枢（`schema.py:400-429`）：

```python
@staticmethod
def resolve(data, schema, options=None, strict=False):
    options = options or {}
    if schema is None:
        return [data]
    if callable(options.get("ignore")) and options["ignore"](data, schema):
        return [data]
    # nullable 分支：缺省值注入在这里（见步骤 4 的 object 说明）
    if _is_nullable(data) and schema.type != "lazy":
        if schema.meta.get("required"):
            raise SchemaValidationError("missing required value", options)
        # ... 沿 intersect 剥到非 nullable 的 default，注入之 ...
    callback = _RESOLVERS.get(schema.type)
    if callback is None:
        raise SchemaValidationError(f'unsupported type "{schema.type}"', options)
    try:
        return callback(data, schema, options, strict)
    except SchemaValidationError:
        if not schema.meta.get("loose"):
            raise
        return [schema.meta.get("default")]   # loose：校验失败改写回退默认值
```

要点与文档化的出入：`resolve` 返回 `[value]` **单元素列表**（`__call__` 取 `[0]`）；不支持的类型抛 `SchemaValidationError`（**无 `SchemaUnsupportedError`**）；`$path` 前缀不在本函数——`SchemaValidationError.__init__`（`schema.py:158-170`）用 `options.path` 现拼；`autofix` 也**不在这里**（只在 `_property` 里出现）。

17 类 resolver 各自处理一种 `type`：`any` / `never` / `const` / `string` / `number` / `boolean` / `function` / `is` / `bitset` / `array` / `dict` / `tuple` / `object` / `union` / `intersect` / `transform` / `lazy`，外加 `date` / `regExp` / `arrayBuffer` 三个复合工厂（上游 `index.ts:537/548/561`，由 `is`/`transform` 复合，**不在** resolvers 表 `index.ts:464-509`）。一棵 `S.object({"name": S.string()})` 在解析时：`resolve` 派发到 object resolver，后者对每个字段递归调用 `property`（`index.ts:698-719`），`property` 内部再 `Schema.resolve(data[key], field_schema, {path: [...path, key]})`——递归下降，错误路径因此自然带上 `.name` 这样的前缀。

### 步骤 2：meta 构建器（克隆语义）

每个链式方法都返回**新** Schema，旧节点不变（对齐上游所有 builder 调 `Schema({...self.meta, ...})`）：

```python
def required(self, value=None):
    meta = dict(self.meta)
    if value is False: meta.pop("required", None)
    else: meta["required"] = value if value is not None else True
    return self._copy(meta)

def default(self, value):
    meta = dict(self.meta); meta["default"] = value
    return self._copy(meta)

def min(self, value):
    meta = dict(self.meta); meta["min"] = value
    return self._copy(meta)

def max(self, value):
    meta = dict(self.meta); meta["max"] = value
    return self._copy(meta)
```

注意 `default` 不只是"填个缺省"：字段缺失时走 `Schema.resolve` 的 **nullable 分支**（`schema.py:408-418`）——**`required` 为真 → 直接抛 `missing required value`，跟有没有 default 无关**；只有**非 required 且配置了 `default`** 才注入默认值（对齐上游 `index.ts:474-484`；object resolver 本身在 `index.ts:752-763`）。`min`/`max`/`step` 在 number resolver 里做范围与步长校验（`checkWithinRange` `index.ts:602` + `isMultipleOf` `index.ts:629`）；`pattern` 在 string resolver 里校验（`index.ts:611`）；`role`/`link` 是给 config-doc / web 配置面用的元信息（不参加校验，只进 `toJSON`）。

### 步骤 3：ValidationError 的 `$path` 前缀

`schema.resolve` 失败时不返回 `None`，而是抛 `SchemaValidationError`，消息带**从根到出错字段**的路径前缀：

```python
schema = S.object({"name": S.string()})
try:
    schema({"name": 123})
except SchemaValidationError as e:
    print(e)          # "$.name expected string but got 123"（数字是实际传入值）
```

注意：`Schema.resolve` 是 `staticmethod`（`schema.py:400`），**经实例 `schema.resolve(...)` 调用会拿到未绑定函数而缺参报错**——应直接 `schema({...})`（或 `Schema.resolve({...}, schema)`）。

`$` 是根，`$.name` 表示根对象的 `name` 字段，`$[0]` 表示数组第 0 项。Cordis 的 `resolveConfig` 把这类错误聚合成 `invalid config:\n  - <msg> (at <path>)`（上游 `fiber.ts`，对应 mini `core/scope.py` 的 `ValidationError` 聚合）——所以一个深层嵌套配置的错误，能精确指到 `$.sub.k` 这一级（这也是 `tests/test_bus.py` 里 `TestConfigValidation` 断言 `(at sub.k)` 的依据）。

### 步骤 4：复合 resolver（object / union / intersect / transform / bitset）

- **object**：遍历 `self.dict` 每个字段递归 `resolve`；缺字段看 `required`/`default`（见步骤 2）。未知键：**非 strict 默认保留（merge 进结果）；`strict` 模式下不合并（丢弃）**（`schema.py:771-772`）。`loose` 与未知键无关——它只把 resolver 校验失败改写回默认值（`schema.py:426-429`）。`adapted` 回写机制让 resolver 能"改写后返回规整值"。
- **array / dict / tuple**：对 item / 值 / 每个位置递归；`tuple` 按位置对 `list` 逐个 resolve，长度不符报错。
- **union**：依次试每个候选，第一个不抛错者胜；全失败则报"未匹配任一分支"。
- **intersect**：把多个 schema 的解析结果浅合并（上游 `index.ts` 的 intersect resolver —— 常用于"基础 schema + 额外约束"叠加）。
- **transform**：`S.transform(source, callback)` 先 resolve `source` 得到中间值，再 `callback(中间值)` 产出最终值（callback 在 Python 载体下只收 callable，不做字符串 eval——上游 `new Function` 是 JS 反序列化路径，mini 标注为差异）。
- **bitset**：工厂收**字典** `S.bitset({"a": 1, "b": 2})`（对齐上游，非数值项被过滤掉，`_build_bitset` `schema.py:975-976`）；解析时只收**数字**或**位名的 list/tuple**——`schema(["a","b"])` 得 `3`（`_resolve_bitset` `schema.py:696-717`；既不收单个字符串也不收 set，收 `"a"`/`{"a"}` 会报 `expected number or array`）。config-doc 用它渲染多选项。
- **lazy**：`S.lazy(lambda: some_schema)` 延迟构造，打破递归 schema 的循环引用（上游 `index.ts:525`）。

### 步骤 5：序列化与简化（toJSON / toString / i18n / simplify）

配置面是"机器读 + 人读"两用的，schemastery 给两套投影：

- **`toJSON()`**：产出 JSON-Schema 风格规格 `{type, ...meta, inner?, dict?, list?}`，`uid` + `refs` 共享序列化（上游 `index.ts:518-541`）——config-doc 与 web 配置面靠它渲染表单。`core/scope.py` 的 `resolve_config` 内部就消费这个形状。
- **`toString()`**：人类可读描述，每种 type 一个 formatter（`formatters` 表 `index.ts:815-892`，如 object 渲染成 `{ key: <inner>, ... }`），供 CLI / 日志展示。
- **`i18n` / `mergeDesc`**：多语言描述合并（`mergeDesc` `index.ts:319-330`，把 `$description` / `$desc` 按 locale 合并）——配置项的说明文字可随语言切换。
- **`simplify(value)`**：把一份已解析数据再压成"最简化"表示（dict-aware 的 `deepEqual` 去掉冗余，上游 `index.ts:411-423`），常用于配置持久化时只存"与默认不同的部分"。

### 步骤 6：`~standard` 协议面

这是 schemastery 与 Cordis 的连接点。`Schema` 暴露一个 `toJSON` 形状（即上面步骤 5 的规格），Cordis 的 `resolveConfig` 读取它来生成插件配置 UI——也就是说，你写一个带 `.role()` / `.description()` / `.default()` 的 schema，框架就能自动渲染出对应的配置表单与校验。mini 在 `core/schema.py:1008` 的 `resolve_config` 里保留了这个消费面：直接 `schema['~standard'].validate(config)`（对齐上游 `fiber.ts:50-62`，逐字镜像），错误经 `ValidationError`（`schema.py:987-1005`）聚合成 `invalid config:\n  - <msg> (at <path>)`。注意：**不存在"把 cordis 配置对象编译成 Schema"的 `_build_config_schema()`**——插件的 `Config` 本身就是作者写的 `S.object({...})` Schema（见第 13 章 `Service` 的 `Config`），`resolve_config` 只做 validate。这是"声明式配置"与"运行时校验"之间的桥梁——也是为什么 schemastery 是 Cordis 生态里不可缺少的一环，而非孤立工具。

## 15.4 mini 里它长在哪

1. **`core/schema.py` 的 `resolve_config`**（`schema.py:1008`，cordis fiber 适配层）：`schema['~standard'].validate(config)` 直接校验插件作者的 `Config` Schema（上游 `fiber.ts` `resolveConfig` 的等价物），错误经 `ValidationError` 聚合成 `invalid config:\n  - <msg> (at <path>)`。
2. **`tests/test_schema_full.py`**：逐机制验收（原语 / 复合 / 缺省 / loose / ignore / 序列化 / `~standard` 协议 / `resolve_config` 聚合），消息逐字断言。
3. **config-doc / web 配置面**：消费 `toJSON` / `toString`（上游 schema 引擎内部 `toJSON` 定义于 `index.ts:296-307`、`toString` 由 `formatters` 表 `index.ts:815-892` 提供）。

## 15.5 验收：硬性规定

`tests/test_schema_full.py` 逐条覆盖：

1. 17 类 resolver 对合法值返回规整结果、对非法值抛 `SchemaValidationError`；递归 object/array/tuple 错误路径带 `$` 前缀。
2. meta 构建器克隆语义：`required`/`default`/`min`/`max`/`step`/`pattern`/`role`/`link` 不改原节点，返回新 Schema。
3. `default` 注入（nullable 分支，非 required 才注入）、`required` 缺失报错、未知键 `strict` 丢弃 / 非 strict 保留、`loose` 校验失败改写回退默认、`autofix` 改写。`loose` 与未知键无直接关系。
4. `union` 首匹配胜、`intersect` 浅合并、`transform` 中间值回调、`bitset` 位标记、`lazy` 延迟构造。
5. `toJSON` uid+refs 共享序列化、`toString` 全 formatter、`i18n` 多语言合并、`simplify` dict-aware 去冗余。
6. `resolve_config` 聚合错误带 `(at <path>)` 双重路径。

```bash
python -m unittest tests.test_schema_full -v
```

## 15.6 检查点练习

1. **写一份插件配置 schema**：`S.object({"concurrency": S.number().min(1).max(16).step(1).default(4), "mode": S.union([S.const("fast"), S.const("safe")]).default("safe")})`；断言缺 `mode` 时回退 `"safe"`、给 `0` 报 `$.concurrency expected number >= 1 but got 0`（`_check_within_range` 产出完整消息，`schema.py:553-559`）。
2. **transform 派生字段**：`S.transform(S.number(), lambda x: x * 2)` 断言 `resolve(3)` 得 `6`。
3. **toJSON 可序列化**：把上面 object schema `to_json()`（mini 的 snake_case 映射，上游为 `toJSON()`）跑 `json.dumps`，断言结果含 `"type": "object"` 且 `dict` 下有 `concurrency` 的 `min:1` 元信息（验证配置面能读到约束）。

## 15.7 回到 dsh：真实源码对照

打开 `deepseek-harness/vendor/schemastery/src/index.ts`：

- `index.ts:239-269`：`Schema` 构造函数（可调用节点）；`index.ts:275-292`：`~standard` 协议 getter（其中的 `validate` 才是校验入口，非静态方法）；`index.ts:296-307`：`toJSON` 形状。
- `index.ts:319-330`：`mergeDesc` / i18n 多语言描述合并。
- `index.ts:411-423`：`simplify` dict-aware 去冗余。
- `index.ts:464-509`：`resolvers` 表与 `Schema.resolve` 递归分发（含 `property` `index.ts:698` 递归下降、`object` / `union` / `intersect` / `array` 等 resolver）。
- `index.ts:296-307`：`toJSON` 序列化；`index.ts:517-527`：`lazy` 延迟构造。
- `index.ts:602-642`：范围与步长校验（`checkWithinRange` / `isMultipleOf` / `decimalShift`）。
- `index.ts:815-892`：`toString` formatter 表与 `defineMethod` 构建器注册。

## 15.8 收尾

这一章的四个字可以带走：**声明即校验**。schemastery 用一棵可调用、可克隆、可序列化的 Schema 树，把"配置长什么样、怎么校验、怎么渲染表单"三件事收进一个引擎——它既是插件作者写配置时的类型护栏，又是框架自动生成配置 UI 的数据源（通过 `~standard` 协议接入 Cordis `resolveConfig`）。

至此，Cordis 技术机制与核心架构在 mini 里已经**全量对齐且逐机制解读**：第 2 章讲服务仓库 + 事件总线 + fiber 生命周期（地基），第 13 章讲 Service 基类 + intercept/LoggerService + intercept 配置，第 14 章讲 dsh_scope 身份路由载波，第 15 章讲 schemastery 配置引擎——四章合起来就是 dsh"一切皆插件、声明即校验、身份即路由"的核心架构全貌。
