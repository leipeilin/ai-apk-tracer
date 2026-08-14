from __future__ import annotations

import sys
from pathlib import Path

from app.analysis.indexer import build_code_index

RULES_ROOT = Path(__file__).resolve().parents[2] / "rules"
if str(RULES_ROOT) not in sys.path:
    sys.path.insert(0, str(RULES_ROOT))

from shared.detector import execute  # noqa: E402
from shared.index_reader import RuleIndexReader  # noqa: E402


def _component(kind: str, name: str, **values):
    return {
        "kind": kind,
        "name": name,
        "exported": "true",
        "permission": None,
        "permission_protection": None,
        "read_permission": None,
        "read_permission_protection": None,
        "write_permission": None,
        "write_permission_protection": None,
        "grant_uri_permissions": False,
        "grant_uri_patterns": [],
        "path_permissions": [],
        "provider_paths": [],
        "authority_tokens": ["com.example.provider"] if kind == "provider" else [],
        **values,
    }


def _payload(tmp_path: Path, sources: dict[str, str], components: list[dict]):
    source_root = tmp_path / "sources"
    source_root.mkdir(parents=True)
    for relative, content in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    index_root = tmp_path / "index"
    descriptor = build_code_index(source_root, index_root / "code-index.json")
    return {
        "manifest": {
            "analysis_platform_api": 36,
            "components": components,
            "authority_conflicts": {},
            "custom_permissions": {},
        },
        "index": {**descriptor, "allowed_index_root": index_root.resolve().as_posix()},
    }


def test_binder_concrete_override_symbolic_hex_return_and_empty_name(tmp_path: Path) -> None:
    service = _component("service", "com.example.RemoteService")
    payload = _payload(tmp_path, {
        "com/example/RemoteService.java": """package com.example;
class RemoteService {
 private Impl binder = new Impl();
 Object onBind(Intent intent) { return binder; }
}
class Impl extends Api.Stub {
 Helper helper;
 @Override String getToken() { return \"token-value\"; }
 @Override void sensitiveLooking() { }
 @Override void a() { helper.run(); }
}
""",
        "com/example/Api.java": """package com.example;
interface Api {
 abstract class Stub extends Binder {
  static final String DESCRIPTOR = \"com.example.Api\";
  static final int TRANSACTION_getToken = 1;
  static final int TRANSACTION_sensitiveLooking = 0x2;
  static final int TRANSACTION_a = IBinder.FIRST_CALL_TRANSACTION + 2;
  boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
   switch (code) {
    case TRANSACTION_getToken:
     String result = getToken();
     reply.writeString(result);
     return true;
    case 0x2:
     sensitiveLooking();
     return true;
    case TRANSACTION_a:
     a();
     return true;
   }
   return false;
  }
  String getToken() { return null; }
  void sensitiveLooking() { }
  void a() { }
 }
}
""",
        "com/example/Helper.java": """package com.example;
class Helper {
 SQLiteDatabase db;
 void run() { db.delete(\"items\", null, null); }
}
""",
    }, [service])

    result = execute("SERVICE_BINDER_CALLER_CHECK_MISSING", payload)
    transactions = [candidate["binder_transaction"] for candidate in result["candidates"]]
    assert {item["code"] for item in transactions} == {1, 3}
    returned = next(item for item in transactions if item["code"] == 1)
    overridden = next(item for item in transactions if item["code"] == 3)
    assert returned["descriptor"] == "com.example.Api"
    assert returned["case_token"] == "TRANSACTION_getToken"
    assert returned["parcel_writes"] == ["writeString"]
    assert overridden["implementation_class"] == "com.example.Impl"
    assert overridden["implementation_descriptor"] == "()->void"
    assert all(candidate["binder_transaction"]["code"] != 2 for candidate in result["candidates"])


def test_binder_guard_is_transaction_local_and_ambiguous_binding_is_gap(tmp_path: Path) -> None:
    service = _component("service", "com.example.GuardedService")
    payload = _payload(tmp_path, {
        "com/example/GuardedService.java": """package com.example;
class GuardedService {
 Object onBind(Intent intent) { return new Impl(); }
}
class Impl extends Api.Stub {
 SQLiteDatabase db;
 void guarded() { db.delete(\"a\", null, null); }
 void open() { db.delete(\"b\", null, null); }
 void ambiguous(int value) { db.delete(\"c\", null, null); }
 void ambiguous(String value) { db.delete(\"d\", null, null); }
}
""",
        "com/example/Api.java": """package com.example;
class Api {
 static class Stub extends Binder {
  static final int TRANSACTION_guarded=1, TRANSACTION_open=2, TRANSACTION_ambiguous=3;
  boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
   switch(code) {
    case TRANSACTION_guarded:
     enforceCallingPermission(\"sig\", \"denied\");
     guarded(); return true;
    case TRANSACTION_open:
     open(); return true;
    case TRANSACTION_ambiguous:
     ambiguous(data.readInt()); return true;
   }
   return false;
  }
  void guarded() {}
  void open() {}
  void ambiguous(int value) {}
 }
}
""",
    }, [service])

    result = execute("SERVICE_BINDER_CALLER_CHECK_MISSING", payload)
    codes = [candidate["binder_transaction"]["code"] for candidate in result["candidates"]]
    assert 1 not in codes
    assert 2 in codes
    ambiguous = next(candidate for candidate in result["candidates"] if candidate["binder_transaction"]["code"] == 3)
    assert ambiguous["deterministic_chain_verified"] is False
    assert any(gap["code"] == "BINDER_IMPLEMENTATION_AMBIGUOUS" for gap in ambiguous["blocking_gaps"])


def test_binder_implementation_post_guard_identity_clear_and_common_guard(tmp_path: Path) -> None:
    service = _component("service", "com.example.LayerService")
    sources = {
        "com/example/LayerService.java": """package com.example;
class LayerService {
 Object onBind(Intent intent) { return new Impl(); }
}
class Impl extends Api.Stub { SQLiteDatabase db;
 void guarded() { enforceCallingPermission(\"sig\", \"denied\"); db.delete(\"a\", null, null); }
 void post() { db.delete(\"b\", null, null); enforceCallingPermission(\"sig\", \"denied\"); }
 void cleared() { Binder.clearCallingIdentity(); db.delete(\"c\", null, null); }
}
""",
        "com/example/Api.java": """package com.example;
class Api { static class Stub extends Binder {
 static final int TRANSACTION_guarded=1, TRANSACTION_post=2, TRANSACTION_cleared=3;
 boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
  switch(code) {
   case TRANSACTION_guarded: guarded(); return true;
   case TRANSACTION_post: post(); return true;
   case TRANSACTION_cleared: cleared(); return true;
  }
  return false;
 }
 void guarded() {} void post() {} void cleared() {}
}}
""",
    }
    candidates = execute("SERVICE_BINDER_CALLER_CHECK_MISSING", _payload(tmp_path, sources, [service]))["candidates"]
    assert {candidate["binder_transaction"]["code"] for candidate in candidates} == {2, 3}
    cleared = next(candidate for candidate in candidates if candidate["binder_transaction"]["code"] == 3)
    assert any(gap["code"] == "CALLING_IDENTITY_CLEARED_BEFORE_EFFECT" for gap in cleared["blocking_gaps"])

    common_sources = {
        **sources,
        "com/example/Api.java": sources["com/example/Api.java"].replace(
            "boolean onTransact(int code, Parcel data, Parcel reply, int flags) {",
            "boolean onTransact(int code, Parcel data, Parcel reply, int flags) { enforceCallingPermission(\"sig\", \"denied\");",
        ),
    }
    assert execute(
        "SERVICE_BINDER_CALLER_CHECK_MISSING",
        _payload(tmp_path / "common", common_sources, [service]),
    )["candidates"] == []


def test_provider_query_return_and_sql_structure_vs_bound_arguments(tmp_path: Path) -> None:
    provider = _component("provider", "com.example.DataProvider")
    payload = _payload(tmp_path, {
        "com/example/DataProvider.java": """package com.example;
class DataProvider {
 SQLiteDatabase db;
 Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
  return db.rawQuery(\"SELECT token FROM secrets WHERE name=?\", selectionArgs);
 }
}
""",
    }, [provider])
    query = execute("PROVIDER_UNAUTHORIZED_QUERY", payload)
    assert len(query["candidates"]) == 1
    assert query["candidates"][0]["flow_kind"] == "return_disclosure"
    assert query["candidates"][0]["sources"][0]["line"] > 1
    assert execute("PROVIDER_SQL_STRUCTURE_INJECTION", payload)["candidates"] == []

    positive = _payload(tmp_path / "positive", {
        "com/example/DataProvider.java": """package com.example;
class DataProvider { SQLiteDatabase db;
 Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
  return db.rawQuery(selection, selectionArgs);
 }
}
""",
    }, [provider])
    sql = execute("PROVIDER_SQL_STRUCTURE_INJECTION", positive)
    assert len(sql["candidates"]) == 1
    assert sql["candidates"][0]["sources"][0]["kind"] == "entry_parameter"


def test_provider_overloads_have_independent_scopes(tmp_path: Path) -> None:
    provider = _component("provider", "com.example.OverloadProvider")
    payload = _payload(tmp_path, {
        "com/example/OverloadProvider.java": """package com.example;
class OverloadProvider { SQLiteDatabase db;
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) { return null; }
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order, CancellationSignal signal) {
  return db.rawQuery(\"SELECT token FROM secret\", args);
 }
}
""",
    }, [provider])
    reader = RuleIndexReader(payload["index"])
    try:
        scopes = reader.provider_entry_scopes(provider["name"])
    finally:
        reader.close()
    query_scopes = [scope for scope in scopes if scope["entry_name"] == "query"]
    assert len(query_scopes) == 2
    assert len({scope["entry_descriptor"] for scope in query_scopes}) == 2

    candidates = execute("PROVIDER_UNAUTHORIZED_QUERY", payload)["candidates"]
    assert len(candidates) == 1
    assert "CancellationSignal" in candidates[0]["entry_descriptor"]


def test_provider_cross_helper_control_apply_batch_open_mode_and_all_effects(tmp_path: Path) -> None:
    provider = _component("provider", "com.example.EntryProvider")
    payload = _payload(tmp_path, {
        "com/example/EntryProvider.java": """package com.example;
class EntryProvider {
 Helper helper; SQLiteDatabase db; ContentResolver resolver;
 int update(Uri uri, ContentValues values, String selection, String[] args) { helper.apply(values); return 1; }
 int delete(Uri uri, String selection, String[] args) {
  db.delete(\"a\", selection, args); db.delete(\"b\", selection, args);
  return 2;
 }
 ParcelFileDescriptor openFile(Uri uri, String mode) { return ParcelFileDescriptor.open(helper.file(uri), MODE_READ_ONLY); }
 Bundle call(String method, String argument, Bundle extras) { if (method.equals(\"wipe\")) db.delete(\"c\", null, null); return new Bundle(); }
 ContentProviderResult[] applyBatch(ArrayList<ContentProviderOperation> operations) { return resolver.applyBatch(\"com.example.provider\", operations); }
}
""",
        "com/example/Helper.java": """package com.example;
class Helper { SQLiteDatabase db; File root;
 void apply(ContentValues values) { db.update(\"items\", values, null, null); }
 File file(Uri uri) { return new File(root, uri.getPath()); }
}
""",
    }, [provider])

    mutation = execute("PROVIDER_UNAUTHORIZED_MUTATION", payload)["candidates"]
    assert {candidate["entry_method_name"] for candidate in mutation} >= {"update", "delete", "call", "applyBatch"}
    delete_candidates = [candidate for candidate in mutation if candidate["entry_method_name"] == "delete"]
    assert {
        candidate["propagation_paths"][-1]["ordinal"]
        for candidate in delete_candidates
    } == {1, 2}
    assert {candidate["sources"][0]["text"] for candidate in delete_candidates} == {"selection", "args"}
    assert any(candidate["flow_kind"] == "control_to_sink" for candidate in mutation if candidate["entry_method_name"] == "call")

    file_candidates = execute("PROVIDER_URI_TO_FILE", payload)["candidates"]
    assert len(file_candidates) == 1
    assert file_candidates[0]["entry_method_name"] == "openFile"
    assert file_candidates[0]["operation_mode"] == "r"
    assert file_candidates[0]["authorization_matrix"][0]["access"] == "read"


def test_provider_entry_guard_and_read_write_authorization_are_independent(tmp_path: Path) -> None:
    provider = _component(
        "provider",
        "com.example.PermissionProvider",
        read_permission="com.example.READ",
        read_permission_protection="signature",
    )
    payload = _payload(tmp_path, {
        "com/example/PermissionProvider.java": """package com.example;
class PermissionProvider { SQLiteDatabase db;
 Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) {
  return db.rawQuery(\"SELECT token FROM secret\", null);
 }
 int update(Uri uri, ContentValues values, String selection, String[] args) {
  return db.update(\"items\", values, selection, args);
 }
}
""",
    }, [provider])

    assert execute("PROVIDER_UNAUTHORIZED_QUERY", payload)["candidates"] == []
    mutation = execute("PROVIDER_UNAUTHORIZED_MUTATION", payload)["candidates"]
    assert mutation
    assert {candidate["entry_method_name"] for candidate in mutation} == {"update"}
    assert {candidate["sources"][0]["text"] for candidate in mutation} == {"values", "selection", "args"}
    assert len({candidate["sinks"][0]["evidence_id"] for candidate in mutation}) == 1
    assert all(candidate["authorization_matrix"][0]["access"] == "write" for candidate in mutation)



def _binder_facts_with_content(path: str, content: str) -> dict:
    """构造 _binder_rule_candidates 可消费的 mock binder_facts：
    transactions 为空 + BINDER_RETURN_TYPE_AMBIGUOUS（critical gap）→ fallback 分支。"""
    return {
        "files": [{
            "path": path,
            "content": content,
            "methods": [{"id": "onBind-id", "name": "onBind",
                         "qualified_class": "com.example.Svc",
                         "start_line": 2, "end_line": 12}],
            "classes": [],
        }],
        "on_bind": {"id": "onBind-id", "name": "onBind", "start_line": 2, "end_line": 12},
        "transactions": [],
        "gaps": [{"code": "BINDER_RETURN_TYPE_AMBIGUOUS", "critical": True, "candidate_count": 24}],
    }


def test_binder_ambiguous_binding_with_caller_check_suppresses_candidate() -> None:
    """v2026-08-09（Cluster E 修复）：transaction 解析歧义（fallback 分支）时，
    若闭包文件存在调用者身份校验（Binder.getCallingUid + 包名校验），
    必须抑制 "caller check missing" 候选——规则此前不看事务内校验导致
    UploadLogSDKService 误报（MarketCallerVerifier 精确校验 com.xiaomi.market）。"""

    from shared.detector import _binder_rule_candidates

    component = _component("service", "com.example.CallerCheckedService")
    manifest = {"analysis_platform_api": 36, "components": [component], "custom_permissions": {}}
    facts = _binder_facts_with_content("com/example/CallerCheckedService.java", """class Svc {
 Object onBind(Intent intent) {
  return new InterfaceApi.Stub() {
   void handle(Bundle bundle, IReply reply) {
    int uid = Binder.getCallingUid();
    if (!CallerVerifier.check(uid)) { reply.fail(302); return; }
   }
  };
 }
}""")

    candidates = _binder_rule_candidates(component, facts, manifest)
    assert candidates == []


def test_binder_ambiguous_binding_without_caller_check_keeps_candidate() -> None:
    """v2026-08-09（Cluster E 修复，保守侧）：transaction 解析歧义且闭包文件
    **无**任何调用者身份校验时，仍产生候选（guard_status=unknown）——宁可
    保留 unresolved/人工复核，不可误识别。"""

    from shared.detector import _binder_rule_candidates

    component = _component("service", "com.example.UnguardedService")
    manifest = {"analysis_platform_api": 36, "components": [component], "custom_permissions": {}}
    facts = _binder_facts_with_content("com/example/UnguardedService.java", """class Svc {
 Object onBind(Intent intent) {
  return new InterfaceApi.Stub() {
   void handle(Bundle bundle, IReply reply) { doWork(bundle); }
  };
 }
}""")

    candidates = _binder_rule_candidates(component, facts, manifest)
    assert candidates
    assert candidates[0]["binder_remote_interface"] is True
    assert candidates[0]["guard_status"] == "unknown"
    assert any(gap["code"] == "BINDER_RETURN_TYPE_AMBIGUOUS" for gap in candidates[0]["blocking_gaps"])






def test_binder_critical_gap_case_scoped_caller_check_suppresses_only_that_case() -> None:
    """v2026-08-09（Cluster E 修复延伸）：主循环 critical_gap 链的调用者校验
    抑制必须限定在**当前 transaction 的 case 行号范围**——文件其它 case 的
    enforceCallingPermission 不保护本链（不能文件级误伤）。"""

    from shared.detector import _binder_rule_candidates

    component = _component("service", "com.example.ScopedService")
    manifest = {"analysis_platform_api": 36, "components": [component], "custom_permissions": {}}
    # case 1（line 4-6）有 enforceCallingPermission；case 2（line 7-9）无——
    # 若文件级扫描会误伤 case 2；case 作用域限定应只抑制 case 1。
    facts = {
        "files": [{
            "path": "com/example/ScopedService.java",
            "content": """class Svc {
 Object onBind(Intent intent) { return new Impl(); }
 boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
  switch(code) {
   case 1: enforceCallingPermission("sig", "denied"); guarded(); return true;
   case 2: unguarded(); return true;
  }
  return false;
 }
 void guarded() {}
 void unguarded() {}
}""",
            "methods": [{"id": "onBind-id", "name": "onBind",
                         "qualified_class": "com.example.Svc",
                         "start_line": 2, "end_line": 2}],
            "classes": [],
        }],
        "on_bind": {"id": "onBind-id", "name": "onBind", "start_line": 2, "end_line": 2},
        "transactions": [
            {
                "code": 1, "interface_method": "guarded",
                "path": "com/example/ScopedService.java", "case_line": 5, "case_end_line": 5,
                "on_transact_method_id": "onTransact-id", "implementation_method_id": "impl-1",
                "dispatch_ordinal": 3, "gaps": [{"code": "BINDER_IMPLEMENTATION_AMBIGUOUS", "critical": True}],
            },
            {
                "code": 2, "interface_method": "unguarded",
                "path": "com/example/ScopedService.java", "case_line": 6, "case_end_line": 6,
                "on_transact_method_id": "onTransact-id", "implementation_method_id": "impl-2",
                "dispatch_ordinal": 4, "gaps": [{"code": "BINDER_IMPLEMENTATION_AMBIGUOUS", "critical": True}],
            },
        ],
        "gaps": [{"code": "BINDER_IMPLEMENTATION_AMBIGUOUS", "critical": True}],
    }

    candidates = _binder_rule_candidates(component, facts, manifest)
    # case 1（guarded）的 case 行号 6 落在 enforceCallingPermission 行 → 抑制
    # case 2（unguarded）的 case 行号 7 无校验 → 保留
    codes = {candidate["binder_transaction"]["code"] for candidate in candidates}
    assert codes == {2}
    kept = candidates[0]
    assert kept["binder_transaction"]["interface_method"] == "unguarded"
    assert kept["guard_status"] == "unknown"
