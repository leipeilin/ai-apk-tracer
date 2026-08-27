# 提交审查报告：`84c7647` — P7/P8 补齐 E5/E6 遗漏

> **审查对象**：`feat(rules): P7/P8 补齐 E5/E6 遗漏——SSL/TrustManager 方法体花括号提取（嵌套块可检）+ 裸 AES 隐式 ECB + WEAK_CIPHER_ALGORITHM 弱算法族（33→34）`
> **审查方式**：逐层代码 diff 核验 + 正则语义正确性 + 与上轮审查发现的 E5/E6 缺口对应性 + 全量回归
> **审查时间**：2026-08-28

---

## 一、总体结论

**结论：✅ 通过。** 本提交精确补上了上轮审查发现的 E5/E6 覆盖缺口，实现质量高、正则语义正确、测试覆盖完整（22 个新增用例）。全量 **1370 passed / 0 failed**（实测）。

---

## 二、与上轮审查缺口的对应关系

上轮审查（`2026-08-28-ruleset-fix-chain-review.md`）发现 **E5/E6 未纳入 P1~P6 修复清单**。本提交（P7/P8）正是对这两项缺口的补齐：

| 上轮缺口 | 本提交修复 | 结论 |
|---|---|---|
| E5：弱加密规则族覆盖窄（裸 `AES` 默认 ECB、DES/3DES/RC4/MD5/SHA1 无规则） | P8：裸 AES 隐式 ECB 补录到 WEAK_CIPHER_ECB + 新增 WEAK_CIPHER_ALGORITHM 规则（33→34） | ✅ 闭合 |
| E6：SSL/TrustManager 正则不跨嵌套块（`[^}]{0,800}?` / `([^{}]{0,400}?)`） | P7：`_matching_brace_end` 花括号配对提取完整方法体 | ✅ 闭合 |

---

## 三、实现正确性核验

### 3.1 P7（E6）—— `_matching_brace_end` 花括号深度计数

```python
def _matching_brace_end(content: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None
```

**正确**：经典花括号深度配对，能正确跨过嵌套块（`if`/`try` 块），精确返回方法体的配对 `}`。

**关键正确性前提**：docstring 明确"sanitized 文本已剔除注释与字符串字面量（花括号已保真），深度计数安全"——若在原始 `code` 上做深度计数，字符串字面量或注释里的 `{`/`}` 会破坏配对。实现确实在 `sanitized` 上调用（`detector.py:3155/3195`），前提成立。

### 3.2 SSL 规则（WEBVIEW_SSL_ERROR_IGNORED）—— 正确

```python
proceed_re = re.compile(r"\w+\s*\.\s*proceed\s*\(")
header_re = re.compile(r"onReceivedSslError\s*\([^)]*\)\s*\{", re.I)
for header in header_re.finditer(sanitized):
    closing = _matching_brace_end(sanitized, header.end() - 1)
    body = sanitized[header.end():closing]
    if proceed_re.search(body):
        ...  # 命中
```

- 方法头定位 → 花括号配对提取完整方法体 → 在方法体内搜 `\w+.proceed(`；
- 正确处理了评审指出的 `if (cond) { handler.cancel(); } handler.proceed();` 形态（proceed 在嵌套块之后）；
- `\w+\s*\.\s*proceed` 词边界匹配，避免误配（如 `proceedHandler` 之类）。

### 3.3 TrustManager 规则（TRUST_MANAGER_ALL_ACCEPT）—— 正确（含保守取舍）

```python
body = sanitized[header.end():closing].strip()
if not body or re.fullmatch(r"return\s*;", body):
    ...  # 命中
```

- `not body`：空体或纯注释体（sanitized 已剔除注释）命中；
- `fullmatch(return;)`：裸 `return;` 形态命中（不抛异常 = 接受任意证书，评审指出的"空实现 return; 形式"）；
- **含任何实质语句不命中**（`if (cond) {}` 这类嵌套空块也不命中，交 AI 复核）。

**轻微观察（不阻塞）**：注释第 3191-3192 行"含嵌套空块**之外**的语句、throw 则不命中"措辞略歧义，字面上可理解为"嵌套空块本身应命中"，但代码实际是"嵌套块一律不命中"。测试 `test_trust_manager_nested_logic_not_hit` 明确了语义（嵌套逻辑不命中）。这是**保守方向**（宁可漏报交 AI 复核，避免误报），符合项目"fail-closed"哲学，仅建议注释措辞后续统一。

### 3.4 P8（E5）—— 裸 AES 隐式 ECB + 弱算法族

**裸 AES**（WEAK_CIPHER_ECB 扩展）：
```python
(re.compile(r"Cipher\s*\.\s*getInstance\s*\(\s*[\"']AES[\"']\s*\)", re.I), ...)
```
正确匹配 `Cipher.getInstance("AES")`（默认 provider 即 AES/ECB/PKCS5Padding）。`test_bare_aes_hits` / `test_aes_gcm_not_hit` 验证了"AES"命中而"AES/GCM/..."不命中（正则是 `[\"']AES[\"']\s*\)`，紧跟闭引号，不会误配 "AES/GCM"）。

**弱算法族**（WEAK_CIPHER_ALGORITHM 新规则）：
- `Cipher.getInstance` 前缀匹配 `DES|DESede|3DES|RC4`（`DES/CBC/PKCS5Padding` 完整形态也命中）；
- `MessageDigest.getInstance` 匹配 `MD5|SHA-?1`（SHA1/SHA-1 都命中）；
- **SHA-256 不命中**（`SHA-?1` 精确到 1，`SHA-256` 的 `2` 不匹配）——`test_sha256_not_hit` 验证。

### 3.5 索引词项（index_reader.py）—— 正确

`WEAK_CIPHER_ALGORITHM` 词项 `["Cipher.getInstance", "MessageDigest.getInstance", "DES", "RC4", "MD5", "SHA"]`，注释正确说明了 SHA-1 在 tokenizer 下切为 `SHA` 与 `1` 两个 token、词项用 `SHA` 由正则精判——这是 FTS 初筛 + 正则精判的既有分层模式，正确。

---

## 四、测试覆盖核验

22 个新增测试用例，覆盖完整边界：

| 分类 | 测试 | 覆盖点 |
|---|---|---|
| E6 SSL | `test_ssl_proceed_after_nested_block_hits` / `..._inside_nested_block_hits` / `test_ssl_cancel_only_no_hit` / `test_ssl_multiple_methods_all_reported` | 嵌套块后放行（核心缺陷）、块内放行、cancel-only 不误报、多方法全枚举 |
| E6 TrustManager | `test_trust_manager_bare_return_hits` / `..._empty_body_still_hits` / `..._comment_only_body_hits` / `..._throw_not_hit` / `..._nested_logic_not_hit` | 裸 return、空体、纯注释体命中；throw/嵌套逻辑不误报 |
| E6 HostnameVerifier | `test_hostname_verifier_boolean_true_hits` | Boolean.TRUE 恒真形态 |
| E5 裸 AES | `test_bare_aes_hits` / `test_explicit_ecb_still_hits` / `test_aes_gcm_not_hit` | 裸 AES 命中、显式 ECB 不回归、GCM 不误报 |
| E5 弱算法 | `test_weak_algorithm_des/rc4/desede_hits` / `test_weak_hash_md5/sha1_hits` / `test_sha256_not_hit` | DES/RC4/DESede/MD5/SHA-1 命中、SHA-256 不误报 |
| 集成 | `test_weak_cipher_rule_meta_registered` / `test_weak_cipher_execute_integration` / `test_bare_aes_execute_integration` | RULE_META 注册、execute 全链路 |

**全量回归：1370 passed / 0 failed**（实测，42.01s），较 P6 的 1348 增加 22 个测试，无回归。

---

## 五、验收声明核对

提交声称"全量 1370 passed"——实测一致。提交无单独 audit.md（标题"方案+验收一体"），文档 `2026-08-28-p7-p8-e5-e6-fix.md` 承载方案与验收。与前六个提交（P1~P6 均附 deepseek-v4-pro 独立审计）不同，本提交**未附独立审计报告**——但这是任务规模差异（E5/E6 是小改），且测试覆盖充分（22 用例），可接受。

---

## 六、总结

| 维度 | 评价 |
|---|---|
| 缺口闭合 | ✅ E5/E6 精确补上（与上轮审查缺口一一对应） |
| 正则正确性 | ✅ 花括号深度计数前提（sanitize 保真）正确；裸 AES/弱算法/SHA-256 边界全部正确 |
| 保守取舍 | ✅ TrustManager 嵌套块不命中交 AI 复核，符合 fail-closed 哲学 |
| 测试覆盖 | ✅ 22 用例覆盖完整边界，含误报防回归（GCM/SHA-256/throw/cancel-only） |
| 回归 | ✅ 1370 passed 无失败 |

**结论：✅ 通过。** 唯一轻微观察是 TrustManager 注释措辞（第 3191-3192 行）与代码语义的轻微歧义，建议后续统一措辞，不影响合入。

---

## 附：审查数据源

- `git show 84c7647` 完整 diff
- `rules/shared/detector.py` / `index_reader.py` 实读核对
- 全量 pytest 实测（1370 passed）
- 22 个新增测试用例语义逐一核对
