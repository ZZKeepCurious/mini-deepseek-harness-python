"""boot config-schema 导出（对齐 packages/boot/app-boot/src/config-schema）。"""
import unittest

from miniharness.boot.config_schema import (
    collect_config_schemas,
    generate_config_schema,
    project_native_schema,
)
from miniharness.boot.profile import Profile
from miniharness.core.schema import S


def _profile(name="headless"):
    return Profile(name=name, dir="x", layers=[], patch_path="", patches=[])


class TestGenerateConfigSchema(unittest.TestCase):
    def test_document_shape(self):
        schema = generate_config_schema(
            _profile(),
            [[{"insert": [{"id": "a", "name": "miniharness.example_plugins",
                           "config": {"greeting": "hi"}}]}]],
            "miniharness")
        self.assertEqual(schema["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["type"], "array")
        self.assertEqual(schema["items"], {"$ref": "#/$defs/entry"})
        for name in ("loaderExpression", "entryMetadata", "entry", "entryList",
                     "patchList", "patch", "unknownConfig", "includeConfig",
                     "includePatch"):
            self.assertIn(name, schema["$defs"])
        x = schema["x-cordis"]
        self.assertEqual(x["profile"], "headless")
        self.assertEqual(x["patchSchema"], "#/$defs/patchList")
        self.assertEqual(x["complete"], True)

    def test_entry_without_config_absent(self):
        schema = generate_config_schema(
            _profile(),
            [[{"insert": [{"id": "a", "name": "miniharness.example_plugins",
                           "config": {"greeting": "hi"}}]}]],
            "miniharness")
        entry = schema["x-cordis"]["entries"][0]
        self.assertEqual(entry["status"], "absent")
        self.assertEqual(entry["configRef"], "#/$defs/unknownConfig")
        self.assertEqual(entry["id"], "a")
        self.assertEqual(entry["name"], "miniharness.example_plugins")

    def test_entry_list_validation(self):
        collected, defs = collect_config_schemas([{"name": "not-a-plugin"}], [])
        self.assertEqual(collected[0]["status"], "absent")
        self.assertEqual(collected[0]["configRef"], "#/$defs/unknownConfig")

    def test_malformed_entry_diagnostic(self):
        diags = []
        collected, _ = collect_config_schemas([{"group": "nope"}], diags)
        self.assertEqual(collected[0]["status"], "error")
        self.assertTrue(any(d["level"] == "error" for d in diags))

    def test_unknown_builtin_diagnostic(self):
        diags = []
        collected, _ = collect_config_schemas(
            [{"name": "cordis:notreal"}], diags)
        self.assertEqual(collected[0]["status"], "error")
        self.assertTrue(any("unknown Cordis builtin" in d["message"] for d in diags))

    def test_complete_false_on_error(self):
        schema = generate_config_schema(
            _profile(),
            [[{"insert": [{"name": "cordis:nope"}]}]],
            "miniharness")
        self.assertEqual(schema["x-cordis"]["complete"], False)
        self.assertTrue(any(d["level"] == "error"
                            for d in schema["x-cordis"]["diagnostics"]))

    def test_skipped_bundle_yields_error_diagnostic(self):
        from miniharness.boot.profile import SkippedBundle
        profile = _profile()
        profile = Profile(name=profile.name, dir=profile.dir,
                          layers=profile.layers, patch_path=profile.patch_path,
                          patches=profile.patches,
                          skipped_bundles=[SkippedBundle("@deepseek-ai/dsh-base",
                                                         "cannot resolve")])
        schema = generate_config_schema(profile, [], "miniharness")
        diagnostics = schema["x-cordis"]["diagnostics"]
        self.assertTrue(any(d["level"] == "error" for d in diagnostics))
        self.assertTrue(any("could not be loaded" in d["message"]
                            for d in diagnostics))
        self.assertEqual(schema["x-cordis"]["complete"], False)

    def test_no_manifest_read_needed(self):
        # generate_config_schema 不再读 manifest：dir 不存在也不抛。
        schema = generate_config_schema(
            Profile(name="p", dir="/does/not/exist", layers=[],
                    patch_path="", patches=[]),
            [], "miniharness")
        self.assertEqual(schema["x-cordis"]["profile"], "p")


class TestProjectNativeSchema(unittest.TestCase):
    def test_object_projection(self):
        cfg = S.object({
            "greeting": S.string().default("hi"),
            "maxTokens": S.number().min(1).max(4096).default(1024).required(),
        }).required()
        proj = project_native_schema(cfg.to_json(), "config0")
        root = proj.schema["anyOf"][0]
        self.assertEqual(root["type"], "object")
        self.assertEqual(root["required"], ["maxTokens"])
        max_tokens = root["properties"]["maxTokens"]["anyOf"][0]
        self.assertEqual(max_tokens["minimum"], 1)
        self.assertEqual(max_tokens["maximum"], 4096)
        self.assertEqual(proj.accepts_missing, False)
        # 值位置包裹 loaderExpression
        self.assertEqual(proj.schema["anyOf"][1],
                         {"$ref": "#/$defs/loaderExpression"})

    def test_array_and_dict(self):
        cfg = S.object({
            "labels": S.array(S.string()),
            "map": S.dict(S.number()),
        })
        proj = project_native_schema(cfg.to_json(), "config0")
        root = proj.schema["anyOf"][0]
        labels = root["properties"]["labels"]["anyOf"][0]
        self.assertEqual(labels["type"], "array")
        self.assertEqual(labels["items"]["anyOf"][0]["type"], "string")
        mapping = root["properties"]["map"]["anyOf"][0]
        self.assertEqual(mapping["type"], "object")
        self.assertEqual(mapping["additionalProperties"]["anyOf"][0]["type"],
                         "number")

    def test_union_projection(self):
        cfg = S.object({"mode": S.union([S.const("a"), S.const("b")])})
        proj = project_native_schema(cfg.to_json(), "config0")
        root = proj.schema["anyOf"][0]
        mode = root["properties"]["mode"]["anyOf"][0]
        # union 本身 anyOf 两分支；每个分支在值位置再包裹 loaderExpression
        branch_any = mode["anyOf"]
        self.assertEqual(len(branch_any), 2)
        self.assertEqual(branch_any[0]["anyOf"][0], {"const": "a"})
        self.assertEqual(branch_any[1]["anyOf"][0], {"const": "b"})

    def test_secret_role_annotated(self):
        cfg = S.object({"apiKey": S.string().role("secret")})
        proj = project_native_schema(cfg.to_json(), "config0")
        root = proj.schema["anyOf"][0]
        api = root["properties"]["apiKey"]
        self.assertEqual(api["x-cordis"], {"role": "secret"})
        self.assertEqual(api["anyOf"][0]["type"], "string")

    def test_transform_limitation(self):
        cfg = S.object({"val": S.transform(S.string(), lambda v: v)})
        proj = project_native_schema(cfg.to_json(), "config0")
        self.assertTrue(proj.limitations)
        self.assertTrue(any("transform" in m for m in proj.limitations))


if __name__ == "__main__":
    unittest.main()