# FPM validation — PHP 7.3.33 php2shell backport vs stock

**Date:** 2026-10-22 · **Lab:** `~/workspace/office-php2shell/php73-backport/fpm-lab/`
**Question:** does the patched 7.3.33 behave correctly under PHP-FPM —
the persistent-worker SAPI these CVEs actually target — especially
around the OOM path, worker lifecycle, multipart parsing, and session
upload progress? Behavior validation only; no exploit payloads were used
(a normal memory-hog script and ordinary multipart forms).

## Builds

Two **fresh** extracts of the official php-7.3.33 tarball
(sha256 `166eaccd…b90cc`):

| | stock | patched |
|---|---|---|
| Tree | `fpm-lab/fpm-stock` (pristine) | `fpm-lab/fpm-patched` (pristine + patch) |
| Patch apply | — | `patch -p1 < php-7.3.33-php2shell-backport.patch` → **exit 0, no FAILED hunks, no .rej** (`patch-apply.log`) |
| Configure (identical) | `--disable-all --disable-libxml --enable-cli --enable-session --enable-json --enable-ctype --enable-tokenizer --enable-fpm` | same |
| Prefix | `fpm-lab/stock-install` | `fpm-lab/patched-install` |
| `php-fpm -v` | `PHP 7.3.33 (fpm-fcgi)` | `PHP 7.3.33 (fpm-fcgi)` |

## Harness

- No nginx / `cgi-fcgi` on this box → requests were sent with a
  purpose-written **stdlib-only Python FastCGI client**
  (`fcgi_client.py`) directly to each pool's unix socket. It records
  HTTP status, body, FCGI_STDERR, and transport failures (reset /
  missing `FCGI_END_REQUEST`) — the last two are what expose a worker
  dying mid-request.
- Driver: `run_lab.py` — runs the **identical** sequence against a
  build and writes `run/results-<build>.json`. Pool configs are
  generated per variant in `run/*.conf`; FPM error logs in `run/*.log`.
- Pool (all variants): `pm=static`, `pm.max_children=2`,
  `catch_workers_output=yes`, `display_errors=on`,
  `session.save_path` in the lab. Main runs use `pm.max_requests=0`;
  OOM variant sets `php_admin_value[memory_limit]=24M`; parts variant
  sets `php_admin_value[max_multipart_body_parts]=5`; recycle variant
  sets `pm.max_requests=5`.
- Lab-only choices, not findings: pool user is `root` (started with
  `-R`) to skip docroot permission plumbing, and
  `session.upload_progress.cleanup=0` is set on **both** builds purely
  so the upload-progress session entry remains observable after the
  POST completes (with the default `cleanup=1` it is deleted at request
  end on stock too).

Docroot scripts (`docroot/`): `hello.php` (marker, version, the new
INI, and a `$GLOBALS['hits']` array that must contain exactly 1 entry
per request — the state-bleed detector), `oom.php` (appends 1 MB
strings until `memory_limit` fatal), `start.php` (session starter),
`upload.php` (dumps `$_FILES`, `$_POST`, session + upload-progress
keys), `parts.php` (counts parsed POST fields / files).

## Results — stock vs patched

| # | Test | Stock 7.3.33 | Patched 7.3.33 | Verdict |
|---|---|---|---|---|
| 1 | 20× `hello.php` sequential | All 200, `global_hits=1` every time, served by both workers (117930/117931) | All 200, `global_hits=1` every time, both workers (117962/117963) | ✅ Identical, fresh state per request |
| 2 | Identity check in `hello.php` | `max_multipart_body_parts` → `false` (INI does not exist) | `max_multipart_body_parts` → `'-1'` (registered by the fix) | ✅ Patch provably live under FPM, not just CLI |
| 3 | `oom.php` response (`memory_limit=24M`) | **Complete** response: status 200, body contains `Fatal error: Allowed memory size of 25165824 bytes exhausted…`, proper FastCGI end | **Truncated** response: 200 headers + first flushed line (`OOM start pid=117968`), then **connection reset**, no `FCGI_END_REQUEST` | ⚠️ Different by design — see test 4 |
| 4 | OOM worker lifecycle | Worker **survives** (bailout): workers before = after = [117935, 117936]; the OOM worker itself serves later requests | Worker **exits**: master log `child 117968 exited with code 1 after 0.320s`, `child 117973 started`; workers [117967, 117968] → [117967, 117973] | ✅ Patched behaves as designed (`zend_mm_safe_error_ex` → `_exit(1)`); stock's survival is exactly the bailout-through-corruptible-state behavior the fix removes |
| 5 | OOM error text | Via normal error handler: `PHP message: PHP Fatal error: Allowed memory size…` (HTML-formatted copy in body) | Written **directly to stderr**, bypassing the error handler: `Fatal error: Allowed memory size of 25165824 bytes exhausted (tried to allocate 1052672 bytes) in …/oom.php on line 8` | ✅ Matches the fix's intent (no reliance on possibly corrupt engine state to format the error) |
| 6 | FPM master after OOM | Alive (pid unchanged) | Alive (pid 117966 unchanged), replacement worker spawned automatically | ✅ Both |
| 7 | 5× `hello.php` immediately after OOM | All 200, `global_hits=1` | All 200, `global_hits=1`, served by survivor 117967 + replacement 117973 | ✅ **No corrupted or inherited state on either**; patched pool self-heals |
| 8 | Multipart upload (`upload.php`, 1 small file + progress field + session cookie) | `$_FILES[userfile]` (hello.txt, 12 B, error 0); session contains `upload_progress_testid123` | `$_FILES` **identical**; progress entries: **NONE** (session empty) | ✅ Expected difference: uploads work; the upload-progress callback is disabled by the fix (`session.c \|\| true`) |
| 9 | Multipart, 61 parts (60 fields + 1 file), default limit | `post_count=60, files_count=1` | `post_count=60, files_count=1` | ✅ Patched default (`-1` → `max_input_vars + max_file_uploads`) accepts large forms |
| 10 | Same 61-part POST, pool `max_multipart_body_parts=5` | **Setting ignored** (INI doesn't exist): still `post_count=60, files_count=1`, no warning | **Limit enforced**: warning `Multipart body parts limit exceeded 5. To increase the limit change max_multipart_body_parts in php.ini.`, truncated to `post_count=5, files_count=0` | ✅ New INI is functional under FPM via `php_admin_value` (PHP_INI_SYSTEM) |
| 11 | Recycle run, `pm.max_requests=5`, 12× `hello.php` | All 200, `global_hits=1`; both original workers recycled (117948/117949 → 117951/117952 observed serving), master alive | All 200, `global_hits=1`; both workers recycled (117984/117985 → 117987/117988), clean exits (code 0), master alive | ✅ Identical lifecycle behavior; the `shutdown_executor` adaptation does not disturb normal worker recycling |

## Master-log excerpts (patched, OOM variant)

```
[22-Oct-2026 10:02:40] WARNING: [pool www] child 117968 said into stderr:
  "Fatal error: Allowed memory size of 25165824 bytes exhausted
   (tried to allocate 1052672 bytes) in .../docroot/oom.php on line 8"
[22-Oct-2026 10:02:40] WARNING: [pool www] child 117968 exited with code 1 after 0.320036 seconds from start
[22-Oct-2026 10:02:40] NOTICE: [pool www] child 117973 started
```

Stock equivalent is a single line — `PHP message: PHP Fatal error:
Allowed memory size…` — and no exit/start lines at all: the worker
carried on.

## Verdict

**The backport passes FPM validation.** Every behavioral difference
from stock is one the upstream fix intends, and each was observed
end-to-end through a real FPM pool:

1. **OOM now kills the worker** (exit code 1 via
   `zend_mm_safe_error_ex` → `_exit(1)`) instead of bailing out and
   reusing it; the master survives and respawns a replacement, and
   subsequent requests on the pool are clean. The cost is visible and
   acceptable: the single in-flight request that hit the OOM gets a
   truncated response (behind nginx this surfaces as a 502 for that
   one request) rather than a formatted fatal page — the deliberate
   trade for not trusting a possibly corrupted allocator/engine state.
2. **Multipart limits work under FPM**, default and explicit, through
   `php_admin_value` — relevant because multipart parsing is the
   CVE-2026-109618/109619 entry point.
3. **Uploads still work; session upload progress is inert**, exactly
   as on the official 7.4/8.0 backports.
4. **Normal lifecycle is untouched**: 20-request runs, post-OOM
   traffic, and `pm.max_requests` recycling all behave identically
   to stock — the sensitive `shutdown_executor` adaptation shows no
   side effects at this level.

**Remaining gaps (not covered here):** the third-party app itself on
the patched build with its real extension set and SAPI config (the
canary step in PATCHING.md), `mod_php` (not tested; Gripp uses FPM),
and long-duration soak. This lab validates the patch's FPM behavior;
it is not a substitute for the app-level canary.
