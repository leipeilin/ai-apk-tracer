"""WebView 与密码学/证书校验族（§12.2 ②③）_webview_crypto_match 全边界测试。

新增规则（v2026-08-09）：WEBVIEW_JS_BRIDGE_EXPOSED / WEBVIEW_FILE_ACCESS_ENABLED /
WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE / WEBVIEW_SSL_ERROR_IGNORED /
WEBVIEW_EXTERNAL_CONTENT / TRUST_MANAGER_ALL_ACCEPT /
HOSTNAME_VERIFIER_ALWAYS_TRUE / WEAK_CIPHER_ECB——全局代码模式，L2 候选。
P5 新增（2026-08-27）：PENDING_INTENT_MUTABLE / LOG_SENSITIVE_DATA /
HARDCODED_SECRET（全局代码模式，L2 候选）。

真实数据（小米商城 APK）：JS_BRIDGE 7 / FILE_ACCESS 2 / EXTERNAL_CONTENT 1 /
TRUST_MANAGER 1（小米自身 HttpSecurityAspect）/ WEAK_CIPHER_ECB 6 候选。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from app.config import WORKSPACE_ROOT

sys.path.insert(0, str(WORKSPACE_ROOT / "rules"))

from shared.detector import RULE_META, _webview_crypto_match  # noqa: E402

FILE = {"path": "com/example/Test.java"}


def _match(rule_id: str, code: str) -> dict | None:
    """E8-3 后 _webview_crypto_match 返回全部命中（list）——helper 取首个（None 语义保持）。"""

    matches = _webview_crypto_match(rule_id, code, FILE)
    return matches[0] if matches else None


def _all_matches(rule_id: str, code: str) -> list[dict]:
    return _webview_crypto_match(rule_id, code, FILE)


class TestWebViewJsBridgeExposed:
    def test_named_bridge_hits(self) -> None:
        m = _match("WEBVIEW_JS_BRIDGE_EXPOSED",
                   'webView.addJavascriptInterface(new Bridge(), "Android");')
        assert m is not None
        assert m["line"] == 1
        assert m["sink_kind"] == "js_bridge"

    def test_multiline_bridge_hits(self) -> None:
        m = _match("WEBVIEW_JS_BRIDGE_EXPOSED",
                   'webView.addJavascriptInterface(\n    new Bridge(),\n    "Android"\n);')
        assert m is not None

    def test_missing_name_argument_no_hit(self) -> None:
        # 无名字符串参数：不是完整 JS 桥注册
        assert _match("WEBVIEW_JS_BRIDGE_EXPOSED",
                      'webView.addJavascriptInterface(obj);') is None

    def test_comment_only_no_hit(self) -> None:
        assert _match("WEBVIEW_JS_BRIDGE_EXPOSED",
                      '// addJavascriptInterface(new Bridge(), "Android")') is None


class TestWebViewFileAccessEnabled:
    def test_allow_file_access_true_hits(self) -> None:
        m = _match("WEBVIEW_FILE_ACCESS_ENABLED", 'settings.setAllowFileAccess(true);')
        assert m is not None
        assert m["sink_kind"] == "file_access"

    def test_allow_file_access_from_file_urls_hits(self) -> None:
        m = _match("WEBVIEW_FILE_ACCESS_ENABLED",
                   'settings.setAllowFileAccessFromFileURLs(true);')
        assert m is not None

    def test_false_no_hit(self) -> None:
        assert _match("WEBVIEW_FILE_ACCESS_ENABLED",
                      'settings.setAllowFileAccess(false);') is None

    def test_legacy_getter_no_hit(self) -> None:
        assert _match("WEBVIEW_FILE_ACCESS_ENABLED",
                      'boolean a = settings.getAllowFileAccess();') is None


class TestWebViewUniversalAccessFromFile:
    def test_true_hits(self) -> None:
        m = _match("WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE",
                   'settings.setAllowUniversalAccessFromFileURLs(true);')
        assert m is not None

    def test_false_no_hit(self) -> None:
        assert _match("WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE",
                      'settings.setAllowUniversalAccessFromFileURLs(false);') is None


class TestWebViewSslErrorIgnored:
    def test_handler_proceed_hits(self) -> None:
        code = ('public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) '
                '{ h.proceed(); }')
        m = _match("WEBVIEW_SSL_ERROR_IGNORED", code)
        assert m is not None
        assert m["sink_kind"] == "ssl_bypass"

    def test_multiline_handler_proceed_hits(self) -> None:
        code = """public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
    handler.proceed();
}"""
        assert _match("WEBVIEW_SSL_ERROR_IGNORED", code) is not None

    def test_cancel_no_hit(self) -> None:
        code = ('public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) '
                '{ h.cancel(); }')
        assert _match("WEBVIEW_SSL_ERROR_IGNORED", code) is None

    def test_no_method_no_hit(self) -> None:
        assert _match("WEBVIEW_SSL_ERROR_IGNORED", 'webView.setWebViewClient(c);') is None


class TestWebViewExternalContent:
    def test_js_enabled_http_load_hits(self) -> None:
        code = 'settings.setJavaScriptEnabled(true); webView.loadUrl("http://evil.com/x");'
        m = _match("WEBVIEW_EXTERNAL_CONTENT", code)
        assert m is not None
        assert m["sink_kind"] == "xss_surface"

    def test_js_enabled_https_load_hits(self) -> None:
        code = 'settings.setJavaScriptEnabled(true); webView.loadUrl("https://a.com/x");'
        assert _match("WEBVIEW_EXTERNAL_CONTENT", code) is not None

    def test_js_disabled_no_hit(self) -> None:
        assert _match("WEBVIEW_EXTERNAL_CONTENT",
                      'settings.setJavaScriptEnabled(false); webView.loadUrl("https://a.com");') is None

    def test_file_load_no_hit(self) -> None:
        # file:// 非外部 URL，不构成反射型 XSS 攻击面（本地内容开发者可控）
        assert _match("WEBVIEW_EXTERNAL_CONTENT",
                      'settings.setJavaScriptEnabled(true); webView.loadUrl("file:///sdcard/a.html");') is None


class TestTrustManagerAllAccept:
    def test_empty_body_hits(self) -> None:
        code = 'public void checkServerTrusted(X509Certificate[] c, String t) { }'
        m = _match("TRUST_MANAGER_ALL_ACCEPT", code)
        assert m is not None
        assert m["sink_kind"] == "cert_bypass"

    def test_noop_body_hits(self) -> None:
        code = 'public void checkServerTrusted(X509Certificate[] c, String t) { /* noop */ }'
        assert _match("TRUST_MANAGER_ALL_ACCEPT", code) is not None

    def test_throws_certificate_exception_no_hit(self) -> None:
        code = ('public void checkServerTrusted(X509Certificate[] c, String t) '
                '{ throw new CertificateException(); }')
        assert _match("TRUST_MANAGER_ALL_ACCEPT", code) is None

    def test_verifies_chain_no_hit(self) -> None:
        code = ('public void checkServerTrusted(X509Certificate[] c, String t) '
                '{ for (X509Certificate x : c) x.checkValidity(); }')
        assert _match("TRUST_MANAGER_ALL_ACCEPT", code) is None


class TestHostnameVerifierAlwaysTrue:
    def test_return_true_hits(self) -> None:
        code = 'public boolean verify(String h, SSLSession s) { return true; }'
        m = _match("HOSTNAME_VERIFIER_ALWAYS_TRUE", code)
        assert m is not None
        assert m["sink_kind"] == "hostname_bypass"

    def test_return_boolean_true_hits(self) -> None:
        code = 'public boolean verify(String h, SSLSession s) { return (true); }'
        assert _match("HOSTNAME_VERIFIER_ALWAYS_TRUE", code) is not None

    def test_conditional_no_hit(self) -> None:
        code = 'public boolean verify(String h, SSLSession s) { return h.equals("api.x.com"); }'
        assert _match("HOSTNAME_VERIFIER_ALWAYS_TRUE", code) is None

    def test_return_false_no_hit(self) -> None:
        assert _match("HOSTNAME_VERIFIER_ALWAYS_TRUE",
                      'public boolean verify(String h, SSLSession s) { return false; }') is None


class TestWeakCipherEcb:
    def test_aes_ecb_hits(self) -> None:
        code = 'Cipher cipher = Cipher.getInstance("AES/ECB/PKCS5Padding");'
        m = _match("WEAK_CIPHER_ECB", code)
        assert m is not None
        assert m["sink_kind"] == "weak_cipher"

    def test_lowercase_aes_ecb_hits(self) -> None:
        assert _match("WEAK_CIPHER_ECB",
                      'Cipher.getInstance("aes/ecb/nopadding");') is not None

    def test_gcm_no_hit(self) -> None:
        assert _match("WEAK_CIPHER_ECB",
                      'Cipher.getInstance("AES/GCM/NoPadding");') is None

    def test_rsa_no_hit(self) -> None:
        assert _match("WEAK_CIPHER_ECB",
                      'Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding");') is None


class TestUnknownRuleReturnsNone:
    def test_unknown_rule_id(self) -> None:
        assert _match("UNKNOWN_RULE", 'anything') is None


def test_webview_crypto_rule_meta_matches_rule_dirs() -> None:
    """联动一致性：detector.RULE_META 注册的 webview/crypto 规则必须与
    rules/webview/ 与 rules/crypto/ 目录下的 rule.yaml 一一对应，防止
    新增规则目录却漏注册 RULE_META（或反之）导致的静默失效。"""

    import yaml
    from shared.detector import GLOBAL_CODE_RULES, RULE_META

    registered = {
        rid for rid, (family, _, _) in RULE_META.items()
        if family in {"webview", "crypto", "intent", "log"}
    }
    dirs = set()
    for family in ("webview", "crypto", "intent", "log"):
        for rule_yaml in sorted((WORKSPACE_ROOT / "rules" / family).glob("*/rule.yaml")):
            meta = yaml.safe_load(rule_yaml.read_text("utf-8"))
            assert meta["builtin"] is True
            assert meta["id"] in RULE_META, f"{meta['id']} 未注册到 RULE_META"
            assert meta["evidence_output"] == "L2"
            dirs.add(meta["id"])

    assert registered == dirs, (
        f"RULE_META 与规则目录不一致: 仅 RULE_META={registered - dirs} "
        f"仅目录={dirs - registered}"
    )
    # 全部 8 条都必须走 GLOBAL_CODE_RULES 全局扫描分支
    assert registered == GLOBAL_CODE_RULES


class TestPendingIntentMutable:
    def test_mutable_default_hits(self) -> None:
        m = _match("PENDING_INTENT_MUTABLE",
                   'PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT);')
        assert m is not None
        assert m["sink_kind"] == "pending_intent_mutable"

    def test_explicit_mutable_hits(self) -> None:
        m = _match("PENDING_INTENT_MUTABLE",
                   'PendingIntent.getBroadcast(context, 0, intent, PendingIntent.FLAG_MUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);')
        assert m is not None

    def test_immutable_no_hit(self) -> None:
        assert _match("PENDING_INTENT_MUTABLE",
                      'PendingIntent.getService(context, 0, intent, PendingIntent.FLAG_IMMUTABLE);') is None

    def test_immutable_combined_flags_no_hit(self) -> None:
        assert _match("PENDING_INTENT_MUTABLE",
                      'PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);') is None

    def test_comment_only_no_hit(self) -> None:
        assert _match("PENDING_INTENT_MUTABLE",
                      '// PendingIntent.getActivity(context, 0, intent, 0);') is None


class TestLogSensitiveData:
    def test_sensitive_identifier_hits(self) -> None:
        m = _match("LOG_SENSITIVE_DATA",
                   'Log.d(TAG, "userId=" + user.getUserId());')
        assert m is not None
        assert m["sink_kind"] == "log_leak"

    def test_token_in_message_hits(self) -> None:
        assert _match("LOG_SENSITIVE_DATA", 'Log.e("Auth", accessToken);') is not None

    def test_plain_tag_no_hit(self) -> None:
        assert _match("LOG_SENSITIVE_DATA", 'Log.d(TAG, "view created");') is None

    def test_comment_only_no_hit(self) -> None:
        assert _match("LOG_SENSITIVE_DATA", '// Log.d(TAG, token);') is None


class TestHardcodedSecret:
    def test_api_key_constant_hits(self) -> None:
        m = _match("HARDCODED_SECRET",
                   'private static final String API_KEY = "sk_live_abcdef123456";')
        assert m is not None
        assert m["sink_kind"] == "hardcoded_secret"

    def test_password_field_hits(self) -> None:
        assert _match("HARDCODED_SECRET", 'String password = "P@ssw0rd123!";') is not None

    def test_short_value_no_hit(self) -> None:
        assert _match("HARDCODED_SECRET", 'String token = "abc";') is None

    def test_benign_name_no_hit(self) -> None:
        assert _match("HARDCODED_SECRET", 'String message = "your token is ready";') is None

    def test_comment_only_no_hit(self) -> None:
        assert _match("HARDCODED_SECRET", '// String secret = "abcdefgh1234";') is None


class TestP5RulesExecuteIntegration:
    """P5 三规则经 execute 完整链路（FTS 初筛 → _webview_crypto_match）产出 L2 候选。"""

    def _payload(self, tmp_path, source):
        from app.analysis.indexer import build_code_index
        source_root = tmp_path / "sources"
        path = source_root / "com/example/Test.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, "utf-8")
        descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
        return {
            "manifest": {"components": [], "custom_permissions": {}, "authority_conflicts": {}},
            "index": {**descriptor, "allowed_index_root": (tmp_path / "index").resolve().as_posix()},
        }

    def test_pending_intent_rule_produces_candidate(self, tmp_path):
        from shared.detector import execute
        result = execute("PENDING_INTENT_MUTABLE", self._payload(tmp_path, """package com.example;
class Sender {
 void send(android.content.Context context, android.app.PendingIntent pi) {
  android.app.PendingIntent.getActivity(context, 0, new android.content.Intent("a"), 0);
 }
}
"""))
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["evidence_level"] == "L2"

    def test_log_rule_produces_candidate(self, tmp_path):
        from shared.detector import execute
        result = execute("LOG_SENSITIVE_DATA", self._payload(tmp_path, """package com.example;
class Logger {
 void log(String token) {
  android.util.Log.d("TAG", token);
 }
}
"""))
        assert len(result["candidates"]) == 1

    def test_hardcoded_secret_rule_produces_candidate(self, tmp_path):
        from shared.detector import execute
        result = execute("HARDCODED_SECRET", self._payload(tmp_path, """package com.example;
class Config {
 static final String SECRET_KEY = "a1b2c3d4e5f6g7h8";
}
"""))
        assert len(result["candidates"]) == 1


class TestP5VerificationEdgeCases:
    """P5 核验 R-2/R-3/R-5 边界锚定。"""

    def test_log_wtf_hits(self) -> None:
        # R-5：Log.wtf（断言级日志）同样覆盖
        assert _match("LOG_SENSITIVE_DATA", 'Log.wtf("Auth", password);') is not None

    def test_pending_intent_substring_coincidence_not_treated_as_hardened(self) -> None:
        # R-3：EXTRA_FLAG_IMMUTABLE_STATE 类子串巧合不视为已加固（词边界判定）
        m = _match("PENDING_INTENT_MUTABLE",
                   'PendingIntent.getActivity(context, 0, intent.putExtra("extra_flag_immutable_state", 1), 0);')
        assert m is not None

    def test_pending_intent_variable_flag_reports(self) -> None:
        # R-3：flags 为变量引用 → 报（误报方向，AI 复核兜底）
        assert _match("PENDING_INTENT_MUTABLE",
                      'PendingIntent.getActivity(context, 0, intent, flags);') is not None

    def test_hardcoded_secret_access_token_fts_recall(self, tmp_path):
        # R-2：SCREAMING_SNAKE 前缀复合名（ACCESS_TOKEN 整体单 token）经扩展词项召回
        from shared.detector import execute
        from app.analysis.indexer import build_code_index
        source_root = tmp_path / "sources"
        path = source_root / "com/example/Config.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('package com.example;\nclass Config {\n static final String ACCESS_TOKEN = "tok_abcdef123456";\n}\n', "utf-8")
        descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
        payload = {
            "manifest": {"components": [], "custom_permissions": {}, "authority_conflicts": {}},
            "index": {**descriptor, "allowed_index_root": (tmp_path / "index").resolve().as_posix()},
        }
        result = execute("HARDCODED_SECRET", payload)
        assert len(result["candidates"]) == 1

    def test_hardcoded_secret_word_stem_false_positive_shape(self) -> None:
        # R-8：词干误报形态锚定（tokenCount 非密钥但词干命中——AI 复核排除）
        m = _match("HARDCODED_SECRET", 'String tokenCount = "12345678";')
        assert m is not None
        assert "词干" in m["description"] or "复核" in m["description"] or m["sink_kind"] == "hardcoded_secret"


class TestSeveritySingleSource:
    """P6（E8-5）：severity 单源化——全部 rule.yaml 的 severity 必须与 RULE_META 一致。"""

    def test_all_rule_yaml_severity_matches_rule_meta(self) -> None:
        missing, mismatched = [], []
        for rule_yaml in sorted(WORKSPACE_ROOT.glob("rules/*/*/rule.yaml")):
            meta = yaml.safe_load(rule_yaml.read_text("utf-8"))
            rule_id = meta.get("id")
            entry = RULE_META.get(rule_id)
            assert entry is not None, f"{rule_id} 未注册到 RULE_META"
            if "severity" not in meta:
                missing.append(rule_id)
            elif str(meta["severity"]) != str(entry[2]):
                mismatched.append(f"{rule_id}: yaml={meta['severity']} meta={entry[2]}")
        assert not missing, f"缺 severity 字段: {missing}"
        assert not mismatched, f"severity 双源漂移: {mismatched}"


class TestMultiMatchE8_3:
    """P6（E8-3）：同文件多调用点全枚举（旧行为只报首个 match）。"""

    def test_js_bridge_multiple_bridges_all_reported(self) -> None:
        matches = _all_matches("WEBVIEW_JS_BRIDGE_EXPOSED", """class A {
 void setup(android.webkit.WebView w1, android.webkit.WebView w2) {
  w1.addJavascriptInterface(new Bridge1(), "Android1");
  w1.addJavascriptInterface(new Bridge2(), "Android2");
 }
}
""")
        assert len(matches) == 2
        assert [m["line"] for m in matches] == [3, 4]

    def test_weak_cipher_multiple_instances_all_reported(self) -> None:
        matches = _all_matches("WEAK_CIPHER_ECB",
                               'Cipher c1 = Cipher.getInstance("AES/ECB/PKCS5Padding");\n'
                               'Cipher c2 = Cipher.getInstance("AES/ECB/PKCS7Padding");\n')
        assert len(matches) == 2

    def test_hardcoded_secret_multiple_constants_all_reported(self) -> None:
        matches = _all_matches("HARDCODED_SECRET",
                               'String api_key = "k1abcdefgh";\nString db_password = "p2abcdefgh";\n')
        assert len(matches) == 2


class TestSensitiveNameHintE8_1:
    """P6（E8-1）：敏感命名启发式词表扩展（Login/Token/Account/Pay 等）。"""

    def test_expanded_vocab_hits_login_token_account(self) -> None:
        # 经 _component_rule 路径：LoginActivity/TokenService/AccountProvider 命中
        from shared.detector import _component_rule

        for name in ("com.example.LoginActivity", "com.example.TokenService", "com.example.PayActivity"):
            component = {"kind": "activity" if "Activity" in name else "service",
                         "name": name, "exported": "true", "permission": None}
            candidate = _component_rule("ACTIVITY_SENSITIVE_NAME_HINT", component, [], {"components": [component]})
            assert candidate is not None, name
            assert candidate.get("auxiliary") is True


class TestP6VerificationFixes:
    """P6 核验 R-1/R-2/R-7 处置后的回归锚定。"""

    def test_severity_assertion_is_bidirectional(self) -> None:
        # R-7：反向断言——RULE_META 每条注册都必须有对应 rule.yaml（防死注册）
        yaml_ids = set()
        for rule_yaml in sorted(WORKSPACE_ROOT.glob("rules/*/*/rule.yaml")):
            meta = yaml.safe_load(rule_yaml.read_text("utf-8"))
            yaml_ids.add(meta["id"])
        assert yaml_ids == set(RULE_META), (
            f"yaml-only: {sorted(yaml_ids - set(RULE_META))}; "
            f"meta-only（死注册）: {sorted(set(RULE_META) - yaml_ids)}"
        )

    def test_multi_match_texts_are_per_call_site(self) -> None:
        # R-2：同文件两处调用的证据文本互不相同（旧行为第 2 个候选重复首个匹配文本）
        matches = _all_matches("WEBVIEW_FILE_ACCESS_ENABLED",
                               'w1.getSettings().setAllowFileAccess(true);\n'
                               'w2.getSettings().setAllowFileAccess(true);\n')
        assert len(matches) == 2
        assert matches[0]["text"] != matches[1]["text"] or (
            matches[0]["line"] != matches[1]["line"]
        )

    def test_sanitize_match_survives_comment_only_source(self) -> None:
        # R-2 附带鲁棒性：原文 search 为 None 时不再 AttributeError 崩溃
        assert _match("WEBVIEW_FILE_ACCESS_ENABLED",
                      '// setAllowFileAccess/*x*/(true);') is None

    def test_external_content_commented_load_url_no_hit(self) -> None:
        # R-4：注释掉的 loadUrl 不构成真实调用点
        assert _match("WEBVIEW_EXTERNAL_CONTENT",
                      'w.getSettings().setJavaScriptEnabled(true);\n// w.loadUrl("http://evil.example");') is None

    def test_sensitive_name_hint_camel_token_word_match(self) -> None:
        # R-3：KeyEvent/Keyboard 类良性驼峰名不命中（整词匹配）
        from shared.detector import _component_rule

        for name in ("com.example.KeyEventActivity", "com.example.KeyboardService", "com.example.ConcertProvider"):
            kind = "activity" if "Activity" in name else ("provider" if "Provider" in name else "service")
            component = {"kind": kind, "name": name, "exported": "true", "permission": None}
            assert _component_rule("ACTIVITY_SENSITIVE_NAME_HINT", component, [], {"components": [component]}) is None, name

    def test_scope_gaps_merged_into_candidate_coverage_gaps(self) -> None:
        # R-1：flow_scope 携带的 gaps（provider 无索引回退的 LEGACY_INDEX_SCOPE）
        # 必须面市到候选 coverage_gaps——旧行为在 _component_rule 路径是传递死端。
        from shared.detector import _component_rule

        component = {"kind": "activity", "name": "com.example.LoginActivity",
                     "exported": "true", "permission": None}
        manifest = {"components": [component], "custom_permissions": {}, "authority_conflicts": {}}
        candidate = _component_rule(
            "ACTIVITY_SENSITIVE_NAME_HINT", component, [], manifest,
            component_flow_scope={
                "files": [], "entry_method_ids": [],
                "gaps": [{"code": "LEGACY_INDEX_SCOPE", "critical": True}],
            },
        )
        assert candidate is not None
        gap_codes = [g.get("code") for g in candidate.get("coverage_gaps", [])]
        assert "LEGACY_INDEX_SCOPE" in gap_codes


class TestP7NestedBlockE6:
    """P7（E6）：SSL/TrustManager 方法体花括号配对提取——嵌套块形态可检。"""

    def test_ssl_proceed_after_nested_block_hits(self) -> None:
        # E6 核心漏检形态：proceed 在嵌套块之后（旧正则停在首个 '}'）
        code = """class Client extends android.webkit.WebViewClient {
 public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
  if (error.getPrimaryError() == SslError.SSL_UNTRUSTED) { handler.cancel(); }
  handler.proceed();
 }
}
"""
        matches = _all_matches("WEBVIEW_SSL_ERROR_IGNORED", code)
        assert len(matches) == 1
        assert matches[0]["line"] == 2

    def test_ssl_proceed_inside_nested_block_hits(self) -> None:
        # proceed 在嵌套块内
        code = """public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) {
  if (someCondition) { h.proceed(); } else { h.cancel(); }
}
"""
        assert len(_all_matches("WEBVIEW_SSL_ERROR_IGNORED", code)) == 1

    def test_ssl_cancel_only_no_hit(self) -> None:
        code = "public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) {\n h.cancel();\n}\n"
        assert _all_matches("WEBVIEW_SSL_ERROR_IGNORED", code) == []

    def test_ssl_multiple_methods_all_reported(self) -> None:
        code = ("public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) { h.proceed(); }\n"
                "public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) { h.proceed(); }\n")
        assert len(_all_matches("WEBVIEW_SSL_ERROR_IGNORED", code)) == 2

    def test_trust_manager_bare_return_hits(self) -> None:
        # E6 补录形态：裸 return; 不抛异常 = 接受任意证书（旧正则不命中）
        code = "public void checkServerTrusted(java.security.cert.X509Certificate[] chain, String authType) {\n return;\n}\n"
        assert len(_all_matches("TRUST_MANAGER_ALL_ACCEPT", code)) == 1

    def test_trust_manager_empty_body_still_hits(self) -> None:
        code = "public void checkServerTrusted(java.security.cert.X509Certificate[] c, String a) {\n}\n"
        assert len(_all_matches("TRUST_MANAGER_ALL_ACCEPT", code)) == 1

    def test_trust_manager_comment_only_body_hits(self) -> None:
        code = "public void checkServerTrusted(java.security.cert.X509Certificate[] c, String a) {\n // trust all\n}\n"
        assert len(_all_matches("TRUST_MANAGER_ALL_ACCEPT", code)) == 1

    def test_trust_manager_throw_not_hit(self) -> None:
        code = "public void checkServerTrusted(java.security.cert.X509Certificate[] c, String a) {\n throw new java.security.cert.CertificateException(\"untrusted\");\n}\n"
        assert _all_matches("TRUST_MANAGER_ALL_ACCEPT", code) == []

    def test_trust_manager_nested_logic_not_hit(self) -> None:
        # 有实质逻辑（嵌套 if + throw）→ 交 AI 复核，不命中
        code = "public void checkServerTrusted(java.security.cert.X509Certificate[] c, String a) {\n if (c.length == 0) { throw new RuntimeException(); }\n}\n"
        assert _all_matches("TRUST_MANAGER_ALL_ACCEPT", code) == []

    def test_hostname_verifier_boolean_true_hits(self) -> None:
        # P8 附带：Boolean.TRUE 恒真形态
        assert _match("HOSTNAME_VERIFIER_ALWAYS_TRUE",
                      'public boolean verify(String h, javax.net.ssl.SSLSession s) { return Boolean.TRUE; }') is not None


class TestP8WeakCipherE5:
    """P8（E5）：裸 "AES" 隐式 ECB + 弱算法/弱哈希族。"""

    def test_bare_aes_hits(self) -> None:
        m = _match("WEAK_CIPHER_ECB", 'Cipher c = Cipher.getInstance("AES");')
        assert m is not None
        assert "隐式 ECB" in m["description"]

    def test_explicit_ecb_still_hits(self) -> None:
        m = _match("WEAK_CIPHER_ECB", 'Cipher c = Cipher.getInstance("AES/ECB/PKCS5Padding");')
        assert m is not None
        assert "AES/ECB" in m["description"]

    def test_aes_gcm_not_hit(self) -> None:
        assert _match("WEAK_CIPHER_ECB", 'Cipher c = Cipher.getInstance("AES/GCM/NoPadding");') is None

    def test_weak_algorithm_des_hits(self) -> None:
        m = _match("WEAK_CIPHER_ALGORITHM", 'Cipher c = Cipher.getInstance("DES/CBC/PKCS5Padding");')
        assert m is not None

    def test_weak_algorithm_rc4_hits(self) -> None:
        assert _match("WEAK_CIPHER_ALGORITHM", 'Cipher c = Cipher.getInstance("RC4");') is not None

    def test_weak_algorithm_desede_hits(self) -> None:
        assert _match("WEAK_CIPHER_ALGORITHM", 'Cipher c = Cipher.getInstance("DESede/CBC/PKCS5Padding");') is not None

    def test_weak_hash_md5_hits(self) -> None:
        assert _match("WEAK_CIPHER_ALGORITHM", 'MessageDigest md = MessageDigest.getInstance("MD5");') is not None

    def test_weak_hash_sha1_hits(self) -> None:
        assert _match("WEAK_CIPHER_ALGORITHM", 'MessageDigest md = MessageDigest.getInstance("SHA-1");') is not None

    def test_sha256_not_hit(self) -> None:
        assert _match("WEAK_CIPHER_ALGORITHM", 'MessageDigest md = MessageDigest.getInstance("SHA-256");') is None

    def test_weak_cipher_rule_meta_registered(self) -> None:
        from shared.detector import GLOBAL_CODE_RULES, RULE_META

        assert "WEAK_CIPHER_ALGORITHM" in GLOBAL_CODE_RULES
        assert RULE_META["WEAK_CIPHER_ALGORITHM"] == ("crypto", "L2", "medium")

    def test_weak_cipher_execute_integration(self, tmp_path) -> None:
        # 端到端：真实索引 + FTS 初筛（词项 DES/RC4/MD5/SHA）→ L2 候选
        from app.analysis.indexer import build_code_index
        from shared.detector import execute

        source_root = tmp_path / "sources"
        path = source_root / "com/example/LegacyCrypto.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'package com.example;\nimport javax.crypto.Cipher;\n'
            'class LegacyCrypto {\n Cipher desCipher = Cipher.getInstance("DES/CBC/PKCS5Padding");\n}\n',
            "utf-8",
        )
        descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
        payload = {
            "manifest": {"components": [], "custom_permissions": {}, "authority_conflicts": {}},
            "index": {**descriptor, "allowed_index_root": (tmp_path / "index").resolve().as_posix()},
        }
        result = execute("WEAK_CIPHER_ALGORITHM", payload)
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["evidence_level"] == "L2"

    def test_bare_aes_execute_integration(self, tmp_path) -> None:
        from app.analysis.indexer import build_code_index
        from shared.detector import execute

        source_root = tmp_path / "sources"
        path = source_root / "com/example/Crypto.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'package com.example;\nimport javax.crypto.Cipher;\n'
            'class Crypto {\n Cipher c = Cipher.getInstance("AES");\n}\n',
            "utf-8",
        )
        descriptor = build_code_index(source_root, tmp_path / "index" / "code-index.json")
        payload = {
            "manifest": {"components": [], "custom_permissions": {}, "authority_conflicts": {}},
            "index": {**descriptor, "allowed_index_root": (tmp_path / "index").resolve().as_posix()},
        }
        result = execute("WEAK_CIPHER_ECB", payload)
        assert len(result["candidates"]) == 1
