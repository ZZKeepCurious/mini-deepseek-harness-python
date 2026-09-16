"""C19 storage 测试：hub + domain 数据形态 + JSON backend（single/per-record）。

对照上游：packages/storage 的三包 spec 测试（registry / domain / json-backend）。
全异步化：domain 单写链与 backend 任务都绑定在各自事件循环上，测试用
IsolatedAsyncioTestCase 保证 setUp / test / tearDown 共享同一 loop。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from miniharness.core.schema import S
from miniharness.core.scope import Context
from miniharness.storage import (
    DomainError,
    Storage,
    StorageError,
    define_domain,
    descriptor_of,
    domain_table,
    install_storage,
)
from miniharness.storage.backend import KvUnitDescriptor
from miniharness.storage.facility import DomainFacility
from miniharness.storage.jsonbackend import JsonStorageBackend, install_storage_json
from miniharness.storage.spec import DomainGlobalSpec, DomainSpec

ACCOUNT_SCHEMA = S.object({
    "name": S.string().required(),
    "balance": S.number().required(),
})


def make_spec(name="payments", version=1, *, layout=None, invalid=None,
              compat=(), global_=None) -> DomainSpec:
    return DomainSpec(
        name=name,
        version=version,
        tables={"accounts": domain_table(ACCOUNT_SCHEMA)},
        layout=layout,
        compatible_versions=compat,
        invalid_records=invalid,
        global_=global_,
    )


class TestDefineDomain(unittest.TestCase):
    def test_valid_spec_returns_same_object(self):
        spec = make_spec()
        self.assertIs(define_domain(spec), spec)

    def test_bad_name_rejected(self):
        with self.assertRaisesRegex(ValueError, "must match"):
            define_domain(make_spec(name="Bad-Name"))

    def test_bad_version_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            define_domain(make_spec(version=-1))
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            define_domain(make_spec(version=1.5))

    def test_compat_entries_must_be_below_version(self):
        with self.assertRaisesRegex(ValueError, "below version"):
            define_domain(make_spec(version=1, compat=(1,)))
        with self.assertRaisesRegex(ValueError, "non-negative integers"):
            define_domain(make_spec(version=2, compat=(-1,)))

    def test_bad_layout_rejected(self):
        with self.assertRaisesRegex(ValueError, "layout"):
            define_domain(make_spec(layout="bogus"))

    def test_bad_invalid_records_rejected(self):
        with self.assertRaisesRegex(ValueError, "backup-and-skip"):
            define_domain(make_spec(invalid="ignore"))

    def test_bad_table_name_rejected(self):
        spec = DomainSpec(name="s", version=1, tables={"Bad Table": domain_table(ACCOUNT_SCHEMA)})
        with self.assertRaisesRegex(ValueError, "table name"):
            define_domain(spec)

    def test_nullable_global_rejected(self):
        spec = make_spec(global_=DomainGlobalSpec(schema=S.any(), initial=0))
        with self.assertRaisesRegex(ValueError, "must not accept null"):
            define_domain(spec)

    def test_non_nullable_global_accepted(self):
        spec = make_spec(global_=DomainGlobalSpec(schema=S.number().required(), initial=0))
        self.assertIs(define_domain(spec), spec)

    def test_descriptor_of_projects_fields(self):
        d = descriptor_of(make_spec(version=3, global_=DomainGlobalSpec(
            schema=S.number().required(), initial=1)))
        self.assertEqual(d.name, "payments")
        self.assertEqual(d.version, 3)
        self.assertEqual(d.tables, ("accounts",))
        self.assertTrue(d.has_global)
        self.assertIsNone(d.layout)
        d2 = descriptor_of(make_spec(layout="per-record", compat=(0,)))
        self.assertEqual(d2.layout, "per-record")
        self.assertEqual(d2.compatible_versions, (0,))


class TestHubAndRegistry(unittest.TestCase):
    def _fake_backend(self):
        return object()

    def test_backend_registry_roundtrip(self):
        storage = Storage(Context(name="hub-test"))
        b = self._fake_backend()
        unregister = storage.backend.register("json", b)
        self.assertIs(storage.backend.get("json"), b)
        self.assertEqual(storage.backend.names(), ["json"])
        unregister()
        with self.assertRaises(StorageError) as cm:
            storage.backend.get("json")
        self.assertEqual(cm.exception.code, "backend-not-found")

    def test_duplicate_backend_rejected(self):
        storage = Storage(Context(name="hub-test"))
        b = self._fake_backend()
        storage.backend.register("json", b)
        with self.assertRaises(StorageError) as cm:
            storage.backend.register("json", object())
        self.assertEqual(cm.exception.code, "duplicate-backend")

    def test_stale_disposer_does_not_remove_successor(self):
        storage = Storage(Context(name="hub-test"))
        first = self._fake_backend()
        unregister = storage.backend.register("json", first)
        unregister()
        storage.backend.register("json", object())  # successor
        unregister()  # stale disposer fires again
        self.assertIn("json", storage.backend.names())

    def test_mount_and_form(self):
        storage = Storage(Context(name="hub-test"))
        facility = object()
        unmount = storage.mount("domain", facility)
        self.assertIs(storage.form("domain"), facility)
        unmount()
        with self.assertRaises(StorageError) as cm:
            storage.form("domain")
        self.assertEqual(cm.exception.code, "form-not-mounted")

    def test_duplicate_mount_rejected(self):
        storage = Storage(Context(name="hub-test"))
        storage.mount("domain", object())
        with self.assertRaises(StorageError) as cm:
            storage.mount("domain", object())
        self.assertEqual(cm.exception.code, "duplicate-mount")

    def test_domain_property(self):
        storage = Storage(Context(name="hub-test"))
        facility = object()
        storage.mount("domain", facility)
        self.assertIs(storage.domain, facility)


class _JsonBackendTestCase(unittest.IsolatedAsyncioTestCase):
    """JSON backend 公共 setUp 挂在每个事件循环测试上。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _unit_descriptor(self, version=1, has_global=True, layout=None, compat=()):
        return KvUnitDescriptor(
            name="payments", version=version, tables=("accounts",),
            has_global=has_global, layout=layout, compatible_versions=compat)


class TestJsonBackendSingle(_JsonBackendTestCase):
    def _open(self, version=1):
        backend = JsonStorageBackend(self.root)
        return backend, backend.kv.open, self._unit_descriptor(version=version)

    async def test_open_creates_empty_shape(self):
        backend, open_, d = self._open()
        unit = await open_(d)
        snap = await unit.load_all()
        self.assertEqual(snap["tables"], {"accounts": {}})
        self.assertIsNone(snap["global"])
        await unit.close()
        await backend.close()

    async def test_put_global_delete_durability(self):
        backend, open_, d = self._open()
        unit = await open_(d)
        await unit.put_record("accounts", "a1", {"name": "alice", "balance": 10})
        await unit.set_global({"active": True})
        await unit.delete_record("accounts", "missing")  # idempotent no-op
        await unit.close()
        # reopen over the same root with a fresh backend instance: durable
        backend2 = JsonStorageBackend(self.root)
        unit2 = await backend2.kv.open(d)
        snap = await unit2.load_all()
        self.assertEqual(snap["tables"]["accounts"]["a1"]["balance"], 10)
        self.assertEqual(snap["global"], {"active": True})
        await unit2.close()
        await backend2.close()

    async def test_version_mismatch_rejected(self):
        backend, open_, d = self._open(version=1)
        unit = await open_(d)
        await unit.put_record("accounts", "a1", {"name": "alice", "balance": 0})
        await unit.close()
        await backend.close()
        backend2 = JsonStorageBackend(self.root)
        with self.assertRaises(StorageError) as cm:
            await backend2.kv.open(self._unit_descriptor(version=2))
        self.assertEqual(cm.exception.code, "version-mismatch")
        await backend2.close()

    async def test_malformed_medium_rejected(self):
        with open(os.path.join(self.root, "payments.json"), "w", encoding="utf-8") as f:
            f.write("not json")
        backend = JsonStorageBackend(self.root)
        with self.assertRaises(StorageError) as cm:
            await backend.kv.open(self._unit_descriptor())
        self.assertEqual(cm.exception.code, "malformed-medium")
        await backend.close()

    async def test_closed_unit_rejects(self):
        backend, open_, d = self._open()
        unit = await open_(d)
        await unit.close()
        with self.assertRaisesRegex(StorageError, "closed"):
            await unit.put_record("accounts", "a", {"name": "x", "balance": 0})
        await backend.close()

    async def test_double_open_rejected(self):
        backend, open_, d = self._open()
        unit = await open_(d)
        with self.assertRaisesRegex(RuntimeError, "already open"):
            await open_(d)
        await unit.close()
        await backend.close()

    async def test_set_global_without_slot_rejected(self):
        backend = JsonStorageBackend(self.root)
        unit = await backend.kv.open(self._unit_descriptor(has_global=False))
        with self.assertRaisesRegex(RuntimeError, "global slot"):
            await unit.set_global(1)
        await unit.close()
        await backend.close()

    async def test_undeclared_table_rejected(self):
        backend, open_, d = self._open()
        unit = await open_(d)
        with self.assertRaisesRegex(RuntimeError, "does not declare table"):
            await unit.put_record("nope", "a", {"name": "x", "balance": 0})
        await unit.close()
        await backend.close()


class TestJsonBackendPerRecord(_JsonBackendTestCase):
    def _open(self, version=1, compat=(), has_global=True):
        backend = JsonStorageBackend(self.root)
        d = self._unit_descriptor(version=version, has_global=has_global,
                                  layout="per-record", compat=compat)
        return backend, d

    async def test_per_record_documents_on_disk(self):
        backend, d = self._open()
        unit = await backend.kv.open(d)
        await unit.put_record("accounts", "key-1", {"name": "alice", "balance": 1})
        await unit.set_global({"active": True})
        doc = os.path.join(self.root, "payments", "accounts", "key-1.json")
        self.assertTrue(os.path.exists(doc))
        self.assertTrue(os.path.exists(os.path.join(self.root, "payments", "global.json")))
        await unit.close()
        await backend.close()

    async def test_durability_across_reopen(self):
        backend, d = self._open()
        unit = await backend.kv.open(d)
        await unit.put_record("accounts", "a1", {"name": "bob", "balance": 5})
        await unit.delete_record("accounts", "missing")
        await unit.close()
        await backend.close()
        backend2 = JsonStorageBackend(self.root)
        unit2 = await backend2.kv.open(d)
        snap = await unit2.load_all()
        self.assertEqual(snap["tables"]["accounts"]["a1"]["name"], "bob")
        await unit2.close()
        await backend2.close()

    async def test_unsafe_key_rejected(self):
        backend, d = self._open()
        unit = await backend.kv.open(d)
        with self.assertRaisesRegex(ValueError, "path-safe"):
            await unit.put_record("accounts", "a/b", {"name": "x", "balance": 0})
        await unit.close()
        await backend.close()

    async def test_foreign_doc_reads_absent(self):
        backend, d = self._open(version=2)
        unit = await backend.kv.open(d)
        pdir = os.path.join(self.root, "payments")
        os.makedirs(os.path.join(pdir, "accounts"), exist_ok=True)
        # stale version stamp
        with open(os.path.join(pdir, "accounts", "stale.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": 1, "record": {"name": "old", "balance": 0}}))
        # malformed doc
        with open(os.path.join(pdir, "accounts", "bad.json"), "w", encoding="utf-8") as f:
            f.write("garbage")
        snap = await unit.load_all()
        self.assertEqual(snap["tables"]["accounts"], {})
        await unit.close()
        await backend.close()

    async def test_legacy_bootstrap_from_single_file(self):
        # seed a legacy single-layout file stamped with an accepted version
        os.makedirs(self.root, exist_ok=True)
        legacy = {
            "unit": {"name": "payments", "version": 1},
            "global": None,
            "tables": {"accounts": {"a1": {"name": "old", "balance": 9}}},
        }
        with open(os.path.join(self.root, "payments.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy))
        backend, d = self._open(version=2, compat=(1,))
        unit = await backend.kv.open(d)
        snap = await unit.load_all()
        self.assertEqual(snap["tables"]["accounts"]["a1"]["balance"], 9)
        # legacy file never deleted or changed
        self.assertTrue(os.path.exists(os.path.join(self.root, "payments.json")))
        await unit.close()
        await backend.close()

    async def test_new_document_suppresses_bootstrap(self):
        legacy = {
            "unit": {"name": "payments", "version": 1},
            "global": None,
            "tables": {"accounts": {"a1": {"name": "old", "balance": 9}}},
        }
        with open(os.path.join(self.root, "payments.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy))
        backend, d = self._open(version=2, compat=(1,))
        unit = await backend.kv.open(d)
        pdir = os.path.join(self.root, "payments")
        os.makedirs(os.path.join(pdir, "accounts"), exist_ok=True)
        with open(os.path.join(pdir, "accounts", "new.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": 2, "record": {"name": "n", "balance": 1}}))
        snap = await unit.load_all()
        self.assertEqual(list(snap["tables"]["accounts"]), ["new"])
        await unit.close()
        await backend.close()

    async def test_backup_record_moves_document_aside(self):
        backend, d = self._open()
        unit = await backend.kv.open(d)
        await unit.put_record("accounts", "k1", {"name": "a", "balance": 1})
        moved = await unit.backup_record("accounts", "k1")
        self.assertIn(".bak.", moved)
        self.assertFalse(os.path.exists(os.path.join(self.root, "payments", "accounts", "k1.json")))
        snap = await unit.load_all()
        self.assertEqual(snap["tables"]["accounts"], {})
        await unit.close()
        await backend.close()


class TestDomainFlow(unittest.IsolatedAsyncioTestCase):
    """对上游 domain.spec / contract 的行为测试（经完整装配链）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = Context(name="storage-domain-test")
        self.storage = install_storage(self.ctx, self.tmp.name)
        self.facility = self.storage.domain

    async def asyncTearDown(self):
        await self.facility.close_all()
        await self.storage.backend.get("json").close()
        self.tmp.cleanup()

    async def _open(self, spec=None):
        return await self.facility.open(define_domain(spec or make_spec()))

    async def test_put_get_update_delete_global(self):
        domain = await self._open()
        accounts = domain.table("accounts")
        await accounts.put("a1", {"name": "alice", "balance": 10})
        self.assertEqual(accounts.get("a1")["balance"], 10)
        self.assertEqual(accounts.size, 1)
        await accounts.put("a1", {"name": "alice", "balance": 11})
        self.assertEqual(len(list(accounts.entries())), 1)
        self.assertTrue(await accounts.delete("a1"))
        self.assertFalse(await accounts.delete("a1"))  # missing → False, no event
        self.assertEqual(accounts.size, 0)
        # global: initial until first set, not materialized on the medium
        spec = make_spec(name="ledger", version=1,
                     global_=DomainGlobalSpec(schema=S.number().required(), initial=0))
        g_domain = await self.facility.open(define_domain(spec))
        self.assertEqual(g_domain.global_.get(), 0)
        await g_domain.global_.set(7)
        self.assertEqual(g_domain.global_.get(), 7)

    async def test_update_atomic_on_write_chain(self):
        domain = await self._open()
        accounts = domain.table("accounts")
        await accounts.put("a1", {"name": "counter", "balance": 0})
        futs = [accounts.update("a1", lambda rec: {**rec, "balance": rec["balance"] + 1})
                for _ in range(50)]
        await asyncio.gather(*futs)
        self.assertEqual(accounts.get("a1")["balance"], 50)

    async def test_update_missing_key_rejected(self):
        domain = await self._open()
        accounts = domain.table("accounts")
        with self.assertRaises(DomainError) as cm:
            await accounts.update("nope", lambda v: v)
        self.assertEqual(cm.exception.code, "missing-key")

    async def test_duplicate_open_rejected(self):
        await self._open()
        with self.assertRaises(DomainError) as cm:
            await self.facility.open(define_domain(make_spec()))
        self.assertEqual(cm.exception.code, "already-open")

    async def test_invalid_record_on_open_rejected(self):
        # write a record that fails the schema through the single-layout unit
        backend = self.storage.backend.get("json")
        d = descriptor_of(make_spec())
        unit = await backend.kv.open(d)
        await unit.put_record("accounts", "bad", {"balance": "not-a-number"})
        await unit.close()
        with self.assertRaises(DomainError) as cm:
            await self.facility.open(define_domain(make_spec()))
        self.assertEqual(cm.exception.code, "invalid-record")

    async def test_invalid_record_detail_located(self):
        backend = self.storage.backend.get("json")
        unit = await backend.kv.open(descriptor_of(make_spec()))
        await unit.close()
        unit = await backend.kv.open(descriptor_of(make_spec()))
        await unit.put_record("accounts", "bad", "garbage")
        await unit.close()
        with self.assertRaises(DomainError) as cm:
            await self.facility.open(define_domain(make_spec()))
        self.assertEqual(cm.exception.code, "invalid-record")
        self.assertEqual(cm.exception.detail, {"table": "accounts", "key": "bad"})

    async def test_backup_and_skip_policy(self):
        backend = self.storage.backend.get("json")
        d = descriptor_of(make_spec(layout="per-record", invalid="backup-and-skip"))
        unit = await backend.kv.open(d)
        await unit.put_record("accounts", "bad", "garbage")
        await unit.put_record("accounts", "good", {"name": "ok", "balance": 1})
        await unit.close()
        domain = await self.facility.open(define_domain(
            make_spec(layout="per-record", invalid="backup-and-skip")))
        self.assertEqual(list(domain.table("accounts").keys()), ["good"])

    async def test_routes_override_backend(self):
        ctx2 = Context(name="routed")
        storage2 = Storage(ctx2)
        other_root = os.path.join(self.tmp.name, "other")
        install_storage_json(ctx2, other_root)
        runs_backend = JsonStorageBackend(other_root)
        runs = storage2.backend.register("runs", runs_backend)
        # 生命周期面：已登记 backend 作为服务可达（上游 domain provider 注入该键）
        from miniharness.storage import storage_backend_service_key
        dispose = ctx2.provide(storage_backend_service_key("runs"), runs_backend)

        class Facetless:
            kv = None

            async def close(self):
                pass

        storage2.backend.register("no-kv", Facetless())
        # domain form mounting on the second hub needs its own facility
        facility = DomainFacility(ctx2, {"backend": "json", "routes": {"payments": "runs"}})
        storage2.mount("domain", facility)
        backend = ctx2.get("storage.backend.runs")
        self.assertIs(backend, runs_backend)
        spec = define_domain(make_spec())
        domain = await facility.open(spec)
        await domain.table("accounts").put("a", {"name": "x", "balance": 0})
        self.assertTrue(os.path.exists(os.path.join(other_root, "payments.json")))
        runs()
        dispose()
        await backend.close()

    async def test_facet_unsupported(self):
        ctx = Context(name="facettest")
        storage = Storage(ctx)
        storage.backend.register("no-kv", FacetlessBackend())
        facility = DomainFacility(ctx, {"backend": "no-kv"})
        storage.mount("domain", facility)
        with self.assertRaises(DomainError) as cm:
            await facility.open(define_domain(make_spec()))
        self.assertEqual(cm.exception.code, "facet-unsupported")

    async def test_domain_changed_events_in_order(self):
        events = []
        self.ctx.on("domain/changed", events.append)
        domain = await self._open()
        accounts = domain.table("accounts")
        await accounts.put("a1", {"name": "alice", "balance": 1})
        await accounts.put("a1", {"name": "alice", "balance": 2})
        await accounts.delete("a1")
        self.assertEqual([(e.operation, e.table, e.key) for e in events],
                         [("put", "accounts", "a1"), ("put", "accounts", "a1"),
                          ("deleted", "accounts", "a1")])
        self.assertEqual(events[0].value["balance"], 1)
        self.assertFalse(events[2].has_value())

    async def test_listener_failure_contained(self):
        def bad(_change):
            raise RuntimeError("boom")

        self.ctx.on("domain/changed", bad)
        domain = await self._open()
        accounts = domain.table("accounts")
        # durable write must not be rejected by a throwing observer
        await accounts.put("a1", {"name": "alice", "balance": 1})
        self.assertEqual(accounts.get("a1")["balance"], 1)

    async def test_close_rejects_new_writes_and_drains(self):
        events = []
        self.ctx.on("domain/changed", events.append)
        domain = await self._open()
        accounts = domain.table("accounts")
        futs = [accounts.put(f"k{i}", {"name": "x", "balance": i}) for i in range(10)]
        await domain.close()
        with self.assertRaises(DomainError) as cm:
            await accounts.put("k100", {"name": "x", "balance": 0})
        self.assertEqual(cm.exception.code, "closed")
        # queued writes landed: events still emitted, unit released
        self.assertEqual(len(events), 10)
        with self.assertRaisesRegex(DomainError, "closed"):
            accounts.get("k0")

    async def test_reopen_after_close_frees_name(self):
        domain = await self._open()
        await domain.close()
        reopened = await self.facility.open(define_domain(make_spec()))
        self.assertEqual(reopened.name, "payments")

    async def test_close_all(self):
        d1 = await self._open()
        d2 = await self._open(make_spec(name="tasks", version=1))
        await self.facility.close_all()
        for d in (d1, d2):
            with self.assertRaisesRegex(DomainError, "closed"):
                d.table("accounts").get("x")


class FacetlessBackend:
    """无 kv facet 的 backend 伪影：证明 facility 在诊断阶段就拒绝。"""

    kv = None

    async def close(self):
        pass


class TestBackendRouteMissing(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_route_backend_fails_loud(self):
        ctx = Context(name="badroute")
        storage = Storage(ctx)
        facility = DomainFacility(ctx, {"backend": "json"})
        storage.mount("domain", facility)
        with self.assertRaises(StorageError) as cm:
            await facility.open(define_domain(make_spec()))
        self.assertEqual(cm.exception.code, "backend-not-found")


if __name__ == "__main__":
    unittest.main()