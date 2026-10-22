# php2shell fix backported to PHP 7.3.33 (CVE-2026-109618 / CVE-2026-109619)

Unofficial community backport of the official PHP security fix for
**php2shell** — CVE-2026-109618 (GHSA-w8hw-w9fx-j79w) and CVE-2026-109619
(GHSA-6wmf-4rv5-p3rp), CVSS 9.2, unauthenticated remote code execution —
to **PHP 7.3.33**, the last PHP 7.3 release.

PHP 7.3 is end-of-life: upstream published fixed releases only for
8.2–8.5, and minimal backport branches only for 7.4, 8.0 and 8.1.
**The PHP project has not published an official PHP 7.3 backport; this
repository provides an independent community port for PHP 7.3.33.**
(Distributions and LTS vendors may offer their own 7.3 patches — the
PHP advisory recommends checking with your vendor first.)

> ⚠️ Not an official PHP release. Not affiliated with or endorsed by the
> PHP Group. Upgrading to a supported PHP (8.3+) remains the strategic fix;
> this patch is for systems that cannot upgrade yet.

## Credits

The security fix itself was written by the PHP security team:

- **Arnaud Le Blanc** ([@arnaud-lb](https://github.com/arnaud-lb)) — author
- **Ilija Tovilo** ([@iluuu1994](https://github.com/iluuu1994)) — co-author

Upstream: [php/php-src](https://github.com/php/php-src) commit
[`712e60f`](https://github.com/php/php-src/commit/712e60f9edfd03f0960fc4fc3c2c375f42b90aa2)
("Avoid state corruption via bailout"), branch `PHP-7.4-CVE-2026-109618-109619`.
Full attribution in [CREDITS.md](CREDITS.md). PHP is © The PHP Group,
PHP License 3.01 ([LICENSE-PHP.txt](LICENSE-PHP.txt)).

The 7.3 port, adaptations, build and verification are by
**Kamran Butt** ([@kamibutt143](https://github.com/kamibutt143)).

## Use

Full step-by-step for **VM / bare metal / EC2** and for **Docker**
(side-by-side build, swap, verification, rollback):
**[PATCHING.md](PATCHING.md)**


```sh
# on a pristine php-7.3.33 source tree:
patch -p1 < php-7.3.33-php2shell-backport.patch
./configure <your flags> && make && make test
```

`php-7.3.33-php2shell-backport.patch` (18 files) is the complete, ready
patch — it already contains the regenerated `Zend/zend_vm_execute.h`.
For full transparency, the original upstream patch
(`cve-74-commit.patch`) and the scripted 7.3 adaptations
(`apply-73-adaptations.py`) are included so every change can be audited
or reproduced from scratch.

## What the port needed

13 of the 17 upstream files apply cleanly to 7.3.33. Four adaptations
(see patch header / `BUILD-AND-VERIFY.md` for detail):

1. `zend_alloc.c` — `zend_execute.h` include (context differs).
2. `zend_vm_def.h` — 7.3 lacks the ZEND_CONCAT overflow guard entirely; added.
3. `zend_string.h` — `ZSTR_MAX_LEN` macros, verbatim from PHP 7.4.
4. `zend_execute_API.c` — `shutdown_executor()` handler neutralization
   relocated to 7.3's shutdown layout.

`zend_vm_execute.h` was regenerated with `zend_vm_gen.php`, not hand-edited.

## Verification summary

Built with gcc 13.3 (CLI + session + json, minimal flags) and tested:

- Stock 7.3.33 builds and behaves identically except for the fix.
- `Zend/tests` + `ext/session/tests`: **2727 passed, 9 failed** — of the 9,
  one (`bug79668`) fails on stock too; the other 8 are the session
  `rfc1867` upload-progress tests, which fail **by upstream design**:
  the official fix disables the session upload-progress callback
  (the 109619 vector), so upload progress data is no longer populated.
- Runtime proof: the patched build registers the new
  `max_multipart_body_parts` INI setting from the fix.

Full method, numbers and caveats: [BUILD-AND-VERIFY.md](BUILD-AND-VERIFY.md).

## Behavior change to know before deploying

Any application feature relying on **session upload progress**
(`$_SESSION['upload_progress_…']`) stops working — that is inherited from
the official fix, on 7.4/8.0 as well. Everything else is unchanged.
Keep `session.upload_progress.enabled=0` in production config as
defense in depth.

## Artifact verification

```text
php-7.3.33.tar.xz (php.net, input):
  sha256 166eaccde933381da9516a2b70ad0f447d7cec4b603d07b9a916032b215b90cc
php-7.3.33-php2shell-backport.patch (this repo):
  sha256 632f61e8ca409635f57d3a6aec3e0ab710921a292eec630e2190326539c8546a
```
CI (`.github/workflows/verify-backport.yml`) re-checks both hashes,
dry-applies and applies the patch to a pristine tarball, builds it,
runs runtime assertions, and compares the Zend + session test failures
against `ci/expected-failures.txt` (the 9 known results documented in
BUILD-AND-VERIFY.md).
A second CI job runs an automated FPM smoke test (`ci/fcgi_smoke.py`):
healthy requests with fresh state, then a memory-exhaustion request,
verifying the FPM master survives and later requests stay clean — a
small automated subset of `FPM-VALIDATION.md`.

## License

PHP License 3.01 — see [LICENSE-PHP.txt](LICENSE-PHP.txt).
