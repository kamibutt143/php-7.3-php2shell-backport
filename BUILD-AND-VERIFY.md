# PHP 7.3.33 backport — CVE-2026-109618 / CVE-2026-109619 (php2shell)

Unofficial backport. **Not** an official PHP release. PHP 7.3 has no upstream
fix and no backport branch; this ports the official EOL mitigation to 7.3.

## Source of the fix
- Upstream commit `712e60f9edfd03f0960fc4fc3c2c375f42b90aa2` —
  "Avoid state corruption via bailout", Arnaud Le Blanc, co-author
  Ilija Tovilo, branch `PHP-7.4-CVE-2026-109618-109619` in php/php-src.
  One commit covering both CVEs, 17 files.
- Applied to stock **php-7.3.33** (php.net tarball sha256
  `166eaccde933381da9516a2b70ad0f447d7cec4b603d07b9a916032b215b90cc`).

## Deliverable
`php-7.3.33-php2shell-backport.patch` — 18 files (17 upstream + Zend/zend_string.h).
Verified: `patch -p1 --dry-run` against a pristine 7.3.33 tree exits 0, no rejects.

Reproduce from scratch:
```sh
tar xf php-7.3.33.tar.xz && cd php-7.3.33
patch -p1 < ../cve-74-commit.patch      # applies with 4 known reject sites
python3 ../apply-73-adaptations.py      # fixes those + ZSTR macros
./configure <your flags> && make        # stage 1
sapi/cli/php Zend/zend_vm_gen.php       # regenerate zend_vm_execute.h
make                                    # stage 2
```
Or simply apply the final `.patch` (already includes the regenerated
`zend_vm_execute.h`) and build once.

## 7.3 adaptations (the only original changes)
1. `Zend/zend_alloc.c` — add `#include "zend_execute.h"` (hunk context differs in 7.3).
2. `Zend/zend_vm_def.h` — ZEND_CONCAT in-place-extend branch has **no** overflow
   guard in 7.3; guard added in patched-7.4 form (a pure conversion was impossible).
3. `Zend/zend_string.h` — `ZSTR_MAX_LEN` / `ZSTR_MAX_OVERHEAD` copied verbatim
   from PHP 7.4 (the fix's concat guards reference them; they don't exist in 7.3).
4. `Zend/zend_execute_API.c` — `shutdown_executor()` layout differs in 7.3
   (no weakrefs; later handler-cleanup phase). Same intent, relocated:
   EH_NORMAL/`exception=NULL` reset after `zend_objects_store_free_object_storage()`,
   handler neutralization inside the fast-shutdown branch.

`Zend/zend_vm_execute.h` was **regenerated** (`zend_vm_gen.php`), not hand-edited:
diff vs stock = 16 added overflow-guard blocks (48 lines), 0 deletions;
34 `zend_mm_safe_error_ex` sites total (same count as the official 7.4-CVE tree).
opcodes/handlers files unchanged.

## Verification (2026-10-22, this workspace)
Build: gcc 13.3, `./configure --disable-all --disable-libxml --enable-cli
--enable-session --enable-json --enable-ctype --enable-tokenizer`, `make` clean.
- Binary: `PHP 7.3.33 (cli) NTS`; `ini_get("max_multipart_body_parts")` = `-1`
  (new INI registered by the fix — runtime proof the patched main.c is live).
- Tests `Zend/tests ext/session/tests`: **2727 passed, 9 failed, 53 skipped.**
- Stock 7.3.33 baseline, identical flags: of those 9 —
  `Zend/tests/bug79668.phpt` fails on stock too (pre-existing, unrelated);
  the other 8 are the session `rfc1867` upload-progress tests.

## Known behavior change (by upstream design, not a porting defect)
The official fix disables the session upload-progress callback in
`ext/session/session.c` (`if (!PS(rfc1867_enabled) || true)`), removing the
109619 vector by removing the feature. Consequences:
- `$_SESSION[<upload_progress prefix>…]` is never populated → the 8 rfc1867
  session tests fail; any app using session upload progress loses that feature.
- Keep `session.upload_progress.enabled=0` in production config anyway
  (PHP_INI_SYSTEM level) as defense in depth, as with the 7.4/8.0 fleet.

## Caveats
- Verified for CLI in this environment. Before production use, build with the
  target app's real configure flags/SAPI (FPM or mod_php) and run the app's
  own test suite on a canary, same discipline as the 7.4/8.0 rollouts.
- Verification is by code identity + runtime behavior + test suites.
  No exploit/PoC testing was performed.
- This is a community backport of an EOL branch: no upstream support.
  Upgrading the app to a supported PHP (8.3.36+) remains the strategic fix.
