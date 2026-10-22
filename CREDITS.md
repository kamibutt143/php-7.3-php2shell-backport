# Credits

## The security fix
The fix in this repository — "Avoid state corruption via bailout", covering
**CVE-2026-109618** (GHSA-w8hw-w9fx-j79w) and **CVE-2026-109619**
(GHSA-6wmf-4rv5-p3rp), a.k.a. *php2shell* — was written by the PHP security team:

- **Arnaud Le Blanc** — author
  ([arnaud-lb](https://github.com/arnaud-lb))
- **Ilija Tovilo** — co-author
  ([iluuu1994](https://github.com/iluuu1994))

Upstream source: [php/php-src](https://github.com/php/php-src) commit
[`712e60f9edfd03f0960fc4fc3c2c375f42b90aa2`](https://github.com/php/php-src/commit/712e60f9edfd03f0960fc4fc3c2c375f42b90aa2),
published on the official backport branch `PHP-7.4-CVE-2026-109618-109619`
(and equivalently for PHP 8.0 as commit `82f8f31b16a035c1b67cc253e79f7cc288f4cc20`).

All credit for the design and implementation of the fix belongs to them and
to the PHP Group. PHP itself is copyright © The PHP Group and licensed under
the PHP License 3.01 (see `LICENSE-PHP.txt`).

## This backport (PHP 7.4 → PHP 7.3.33)
Ported, adapted, built and verified by **Kamran Butt**
([kamibutt143](https://github.com/kamibutt143)).

The porting work consists of:
- Applying the upstream commit to PHP 7.3.33 (13 of 17 files apply cleanly).
- Four 7.3-specific adaptations (`apply-73-adaptations.py`):
  1. `Zend/zend_alloc.c` — `zend_execute.h` include (context differs in 7.3).
  2. `Zend/zend_vm_def.h` — adding the ZEND_CONCAT overflow guard that 7.3
     lacks entirely, in patched-7.4 form.
  3. `Zend/zend_string.h` — `ZSTR_MAX_LEN` / `ZSTR_MAX_OVERHEAD` macros,
     copied verbatim from PHP 7.4 (written by the PHP Group; absent in 7.3).
  4. `Zend/zend_execute_API.c` — relocating the `shutdown_executor()` handler
     neutralization to match 7.3's shutdown layout.
- Regenerating `Zend/zend_vm_execute.h` with `zend_vm_gen.php` and verifying
  the regeneration is exactly 16 added overflow-guard blocks.
- Building and test-verifying stock vs patched 7.3.33 (see `BUILD-AND-VERIFY.md`).

## Status
Unofficial community backport. Not affiliated with, endorsed by, or supported
by the PHP Group. Provided under the PHP License 3.01, without warranty —
review and test before any production use.
