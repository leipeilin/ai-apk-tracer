"""WebView 与密码学/证书校验族（§12.2 ②③）_webview_crypto_match 全边界测试。

新增规则（v2026-08-09）：WEBVIEW_JS_BRIDGE_EXPOSED / WEBVIEW_FILE_ACCESS_ENABLED /
WEBVIEW_UNIVERSAL_ACCESS_FROM_FILE / WEBVIEW_SSL_ERROR_IGNORED /
WEBVIEW_EXTERNAL_CONTENT / TRUST_MANAGER_ALL_ACCEPT /
HOSTNAME_VERIFIER_ALWAYS_TRUE / WEAK_CIPHER_ECB——全局代码模式，L2 候选。

真实数据（小米商城 APK）：JS_BRIDGE 7 / FILE_ACCESS 2 / EXTERNAL_CONTENT 1 /
TRUST_MANAGER 1（小米自身 HttpSecurityAspect）/ WEAK_CIPHER_ECB 6 候选。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import WORKSPACE_ROOT

sys.path.insert(0, str(WORKSPACE_ROOT / "rules"))

from shared.detector import _webview_crypto_match  # noqa: E402

FILE = {"path": "com/example/Test.java"}


def _match(rule_id: str, code: str) -> dict | None:
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
        rid for rid, (family, _, _) in RULE_META.items() if family in {"webview", "crypto"}
    }
    dirs = set()
    for family in ("webview", "crypto"):
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
