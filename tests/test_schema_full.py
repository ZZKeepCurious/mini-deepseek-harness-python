# -*- coding: utf-8 -*-
"""schemastery 全量移植验收（对齐 vendor/schemastery/src/index.ts）。

消息文案逐字对照上游 resolver；行为差异仅来自 Python str() 对 bool/None 的拼写
（上游 true/null），已在非逐字用例中规避 bool/None 作为 got 位置。
"""
import unittest
import re
import datetime as dt

from miniharness.core.schema import (
    S,
    Schema,
    SchemaValidationError,
    ValidationError,
    resolve_config,
)


class TestPrimitives(unittest.TestCase):
    def test_any_passthrough(self):
        self.assertEqual(S.any()(42), 42)
        self.assertEqual(S.any()("x"), "x")

    def test_never_rejects(self):
        with self.assertRaises(SchemaValidationError) as cm:
            S.never()(0)
        self.assertIn("expected nullable but got", str(cm.exception))

    def test_const_match_and_mismatch(self):
        self.assertEqual(S.const("foo")("foo"), "foo")
        with self.assertRaises(SchemaValidationError) as cm:
            S.const("foo")("bar")
        self.assertEqual(str(cm.exception), "expected foo but got bar")

    def test_string_type_and_pattern(self):
        self.assertEqual(S.string()("hi"), "hi")
        with self.assertRaises(SchemaValidationError):
            S.string()(5)
        with self.assertRaises(SchemaValidationError) as cm:
            S.string().pattern(re.compile(r"^a"))("bx")
        self.assertIn("expect string to match regexp", str(cm.exception))

    def test_string_length_range(self):
        with self.assertRaises(SchemaValidationError) as cm:
            S.string().min(2)("a")
        self.assertIn("expected string length >= 2", str(cm.exception))
        with self.assertRaises(SchemaValidationError) as cm:
            S.string().max(1)("ab")
        self.assertIn("expected string length <= 1", str(cm.exception))

    def test_number_type_range_step(self):
        self.assertEqual(S.number()(3.5), 3.5)
        with self.assertRaises(SchemaValidationError):
            S.number()("x")
        with self.assertRaises(SchemaValidationError) as cm:
            S.number().min(0)(-1)
        self.assertIn("expected number >= 0", str(cm.exception))
        # float 安全 step（decimalShift）：0.3 是 0.1 的整数倍，0.35 不是
        self.assertEqual(S.number().step(0.1).min(0)(0.3), 0.3)
        with self.assertRaises(SchemaValidationError) as cm:
            S.number().step(0.1).min(0)(0.35)
        self.assertIn("expected number multiple of 0.1", str(cm.exception))

    def test_natural_and_percent(self):
        self.assertEqual(S.natural()(3), 3)
        with self.assertRaises(SchemaValidationError):
            S.natural()(-1)
        self.assertEqual(S.percent()(0.5), 0.5)
        with self.assertRaises(SchemaValidationError):
            S.percent()(2)

    def test_boolean_and_function(self):
        self.assertEqual(S.boolean()(True), True)
        with self.assertRaises(SchemaValidationError):
            S.boolean()(1)
        self.assertEqual(S.function()(len), len)
        with self.assertRaises(SchemaValidationError):
            S.function()(5)

    def test_is_constructor(self):
        self.assertEqual(S.is_(int)(5), 5)
        with self.assertRaises(SchemaValidationError) as cm:
            S.is_(int)("x")
        self.assertIn("expected int but got", str(cm.exception))
        # 名称字符串沿 mro 走查；is_ 返回原对象（同一引用）
        class A:
            pass
        class B(A):
            pass
        b = B()
        self.assertIs(S.is_("A")(b), b)


class TestComposite(unittest.TestCase):
    def test_object_defaults_and_required(self):
        schema = S.object({"name": S.string(), "n": S.natural().default(3)})
        self.assertEqual(schema({"name": "x"}), {"name": "x", "n": 3})

    def test_object_merge_extras(self):
        # 上游 object 非 strict 时把未知键并入结果
        self.assertEqual(S.object({"a": S.string()})({"a": "x", "b": 1}), {"a": "x", "b": 1})

    def test_object_required_path_prefix(self):
        schema = S.object({"a": S.object({"b": S.string().required()})})
        with self.assertRaises(SchemaValidationError) as cm:
            schema({"a": {}})
        self.assertEqual(str(cm.exception), "$.a.b missing required value")

    def test_array(self):
        self.assertEqual(S.array(S.number())([1, 2, 3]), [1, 2, 3])
        with self.assertRaises(SchemaValidationError):
            S.array(S.number())(["x"])

    def test_array_length_min_with_default_inner(self):
        # inner 有默认时 skipMin=True（上游 !isNullable(inner.default)）
        self.assertEqual(S.array(S.string().default("x"))([]), [])
        with self.assertRaises(SchemaValidationError) as cm:
            S.array(S.string())("notlist")
        self.assertIn("expected array", str(cm.exception))

    def test_dict_key_rewrite_and_type(self):
        schema = S.dict(S.number(), S.string())
        self.assertEqual(schema({"a": 1, "b": 2}), {"a": 1, "b": 2})
        with self.assertRaises(SchemaValidationError):
            schema({"a": "bad"})

    def test_tuple_extra_appended(self):
        schema = S.tuple([S.string(), S.number()])
        self.assertEqual(schema(["a", 1, 9]), ["a", 1, 9])

    def test_union_error_message(self):
        schema = S.union([S.string(), S.number()])
        self.assertEqual(schema("x"), "x")
        with self.assertRaises(SchemaValidationError) as cm:
            schema([1, 2])
        self.assertTrue(str(cm.exception).startswith("expected string | number but got"))

    def test_intersect_merge(self):
        schema = S.intersect([S.object({"a": S.string()}), S.object({"b": S.number()})])
        self.assertEqual(schema({"a": "x", "b": 1}), {"a": "x", "b": 1})

    def test_intersect_type_mismatch(self):
        schema = S.intersect([S.object({"a": S.string()}), S.object({"b": S.number()})])
        # 第二个 cross 解析产生类型冲突（number vs string 合并）
        with self.assertRaises(SchemaValidationError) as cm:
            schema({"a": "x", "b": "y"})
        self.assertIn("but got", str(cm.exception))

    def test_transform(self):
        self.assertEqual(S.transform(S.string(), lambda v: v.upper())("ab"), "AB")

    def test_bitset(self):
        schema = S.bitset({"x": 1, "y": 2})
        # 可调用返回 [0]（归一化值）；keys 经 resolve 第二元素取得
        self.assertEqual(schema(["x", "y"]), 3)
        full = Schema.resolve(["x", "y"], schema, {})[1]
        self.assertEqual(full, ["x", "y"])

    def test_lazy(self):
        self.assertEqual(S.lazy(lambda: S.string().role("r"))("z"), "z")

    def test_date_regexp_arraybuffer(self):
        self.assertIsInstance(S.date()("2020-01-01T00:00:00"), dt.datetime)
        self.assertIsInstance(S.reg_exp("i")("abc"), re.Pattern)
        self.assertEqual(S.array_buffer("base64")("YWJj"), b"abc")
        self.assertEqual(S.array_buffer("hex")("00ff"), b"\x00\xff")


class TestDefaultsLooseIgnore(unittest.TestCase):
    def test_default_fallback(self):
        self.assertEqual(S.string().default("d")(None), "d")

    def test_intersect_default_walk(self):
        schema = S.intersect([S.object({"a": S.string()}), S.object({"b": S.number().default(2)})])
        self.assertEqual(schema(None), {"b": 2})

    def test_loose_returns_default(self):
        self.assertEqual(S.number().loose()(None), None)

    def test_ignore_option(self):
        schema = S.string().required()
        self.assertEqual(schema(5, {"ignore": lambda data, sc: True}), 5)

    def test_unsupported_type(self):
        node = Schema({"type": "frobnicate"})
        with self.assertRaises(SchemaValidationError) as cm:
            node("x")
        self.assertIn('unsupported type "frobnicate"', str(cm.exception))


class TestBuilderCloneSemantics(unittest.TestCase):
    def test_required_not_mutate_original(self):
        base = S.string()
        req = base.required()
        self.assertIsNot(base, req)
        self.assertTrue(req.meta.get("required"))
        self.assertIsNone(base.meta.get("required"))

    def test_deprecated_shares_badges_list(self):
        # 上游缺陷式行为：Schema(this) 浅拷贝后原地 push，badges 列表与原节点共享
        base = S.string()
        decorated = base.deprecated()
        self.assertEqual(decorated.meta["badges"], base.meta["badges"])
        self.assertEqual(decorated.meta["badges"][0]["text"], "deprecated")

    def test_uid_monotonic(self):
        a = S.string()
        b = S.string()
        self.assertGreater(b.uid, a.uid)

    def test_set_push_mutate_in_place(self):
        node = S.object({})
        n = S.number()
        node.set("k", n)
        self.assertIn("k", node.dict)
        self.assertIs(node.dict["k"], n)
        # union 节点自带 list，push 原地追加
        uni = S.union([S.string()])
        m = S.number()
        self.assertIs(uni.push(m), uni)
        self.assertIn(m, uni.list)


class TestSerialization(unittest.TestCase):
    def test_to_string_basic(self):
        self.assertEqual(S.string().to_string(), "string")
        self.assertEqual(S.object({"a": S.string()}).to_string(), "{ a?: string }")
        self.assertEqual(S.array(S.number()).to_string(), "number[]")
        self.assertEqual(S.tuple([S.string(), S.number()]).to_string(), "[string, number]")
        self.assertEqual(S.union([S.string(), S.number()]).to_string(True), "(string | number)")
        self.assertEqual(
            S.intersect([S.object({"a": S.string()}), S.object({"b": S.number()})]).to_string(),
            "{ a?: string } & { b?: number }",
        )
        self.assertEqual(S.dict(S.number(), S.string()).to_string(), "{ [key: string]: number }")

    def test_to_json_roundtrip(self):
        schema = S.object({"a": S.string()})
        payload = schema.to_json()
        self.assertIn("uid", payload)
        self.assertIn("refs", payload)
        self.assertIn(payload["uid"], payload["refs"])
        ref = payload["refs"][payload["uid"]]
        self.assertEqual(ref["type"], "object")
        self.assertIn("a", ref["dict"])

    def test_i18n_merge(self):
        schema = S.string().description("orig")
        localized = schema.i18n({"en": {"$description": "english"}})
        self.assertEqual(localized.meta["description"], {"": "orig", "en": "english"})

    def test_simplify_default_collapse(self):
        schema = S.object({"a": S.string().default("x")})
        self.assertIsNone(schema.simplify({"a": "x"}))


class TestFromAndExtend(unittest.TestCase):
    def test_from_primitives(self):
        self.assertEqual(S.from_("x").type, "const")
        self.assertEqual(S.from_(str).type, "string")
        self.assertEqual(S.from_(int).type, "number")

    def test_from_type_error(self):
        with self.assertRaises(TypeError):
            S.from_(object())

    def test_extend_custom_type(self):
        Schema.extend("doubled", lambda data, schema, options, strict: [data * 2])
        node = Schema({"type": "doubled"})
        self.assertEqual(node(5), 10)


class TestStandardProtocol(unittest.TestCase):
    def test_validate_success(self):
        result = S.string()["~standard"].validate("ok")
        self.assertIn("value", result)
        self.assertEqual(result["value"], "ok")

    def test_validate_failure_shape(self):
        result = S.string()["~standard"].validate(5)
        self.assertIn("issues", result)
        issue = result["issues"][0]
        self.assertEqual(issue["message"], "expected string but got 5")
        self.assertEqual(issue["path"], [])

    def test_validate_failure_path(self):
        schema = S.object({"a": S.object({"b": S.string().required()})})
        issue = schema["~standard"].validate({"a": {}})["issues"][0]
        self.assertEqual(issue["path"], ["a", "b"])
        self.assertEqual(issue["message"], "$.a.b missing required value")

    def test_resolve_config_passthrough(self):
        self.assertEqual(resolve_config(None, {"a": 1}), {"a": 1})

    def test_resolve_config_aggregated_error(self):
        from miniharness.core.schema import ValidationError as CordisError
        schema = S.object({"name": S.string(), "sub": S.object({"k": S.natural()})})
        with self.assertRaises(CordisError) as cm:
            resolve_config(schema, {"name": 1, "sub": {"k": "bad"}})
        # 首个失败字段为 name（object 按字典序校验），schemastery 已在 message 内带 $ 前缀，
        # cordis 层再追加 (at path) —— 与上游 fiber.ts 双重 path 一致
        self.assertIn("(at name)", str(cm.exception))
        self.assertIn("$.name expected string but got 1", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
