# Apache mod_php validation

This document describes the automated Apache `mod_php` validation for
the PHP 7.3.33 php2shell backport: what the CI job tests, how the
build works, and — just as important — what it does not prove.

The backport is validated under three runtimes:

| Runtime | Validation |
|---|---|
| PHP CLI | Zend + session regression suites, exact known-failure set (`verify` job) |
| PHP-FPM | FastCGI smoke tests incl. OOM worker recovery (`fpm-smoke` job, see FPM-VALIDATION.md) |
| Apache mod_php | HTTP smoke tests against a real httpd (`apache-smoke` job, this document) |

## The apache-smoke CI job

`.github/workflows/verify-backport.yml`, job `apache-smoke`:

1. Downloads the pristine PHP 7.3.33 tarball and verifies its SHA-256.
2. Verifies the backport patch SHA-256 (the patch itself is unchanged
   by this work — same hash as v1.0.0).
3. Extracts and applies the patch; any rejected hunk fails the job.
4. Installs `apache2` and `apache2-dev` from the Ubuntu 24.04 archive
   (provides `apxs2` and the APR development files). The distribution
   Apache configuration in `/etc/apache2` is never used.
5. Configures PHP with `--with-apxs2=/usr/bin/apxs2` (plus the same
   minimal extension set as the other jobs) and builds. The build must
   produce `libs/libphp7.so`.
6. Runs `ci/apache_smoke.py /usr/sbin/apache2 <path to libphp7.so>`.
7. On failure, uploads the Apache error log and generated config as a
   CI artifact (`apache-smoke-logs`).

## How the smoke test works

`ci/apache_smoke.py` (Python standard library only) creates a fully
temporary ServerRoot, config and document root, starts httpd in the
foreground on `127.0.0.1:<ephemeral port>`, and loads **only** the
`libphp7.so` built in the previous step — no system PHP module is
loaded, and the identity checks below cannot pass against an
unpatched or system PHP. Checks, over real HTTP:

**Runtime identity**
- `PHP_SAPI === 'apache2handler'`
- `PHP_VERSION === '7.3.33'`
- `ini_get('max_multipart_body_parts') === '-1'` — stock 7.3.33 does
  not have this INI setting at all (`ini_get` returns `false`), so
  this proves the loaded module is the patched build.
- `apache_get_version()` exists (mod_php-only function).

**Request correctness**
- HTTP 200 for a simple PHP request; string concatenation and common
  string functions produce exact expected output.
- GET and POST parameters are parsed correctly.
- Three sequential requests each see fresh per-request state
  (`$GLOBALS` counter is 1 on every request — mod_php resets request
  state between requests, nothing leaks).

**Multipart behaviour** (semantics verified against the patched
source, `main/rfc1867.c`)
- A normal multipart form (2 fields + 1 file) parses completely at
  the default limit of `-1`.
- A per-directory `php_admin_value max_multipart_body_parts 5` scopes
  a stricter limit to a test directory: 3 parts pass intact.
- 8 parts against a limit of 5: PHP raises
  `E_WARNING: Multipart body parts limit exceeded 5...`, stops parsing
  further parts, keeps the first 5, and the request still completes
  with **HTTP 200**. PHP does not guarantee an error status here, and
  the test asserts none — only the truncation, the warning text, and
  continued server health. (Multipart parsing happens during request
  startup, so this warning is a startup-phase error: the test config
  enables `display_startup_errors` to observe it over HTTP. It is
  written to the Apache error log either way.)

**Stability**
- A malformed multipart body (wrong boundary, garbage payload) is
  answered and the next request is served normally.
- One prefork child process is killed with SIGKILL; the master must
  survive, spawn a replacement, and serve clean requests. This is
  deliberately *not* an OOM test and does not reuse FPM semantics:
  under prefork, a child serves one request at a time and the parent
  replaces dead children.

The script fails closed: if httpd does not start, PHP identity is
wrong, or any assertion fails, it exits non-zero and prints the tail
of the Apache error log.

## Build configuration and MPM / thread safety

PHP is built **non-thread-safe (NTS)**, the default. A non-thread-safe
`mod_php` must only run under the **prefork MPM** (one process per
request, no threads). The smoke configuration therefore loads
`mpm_prefork_module` explicitly. It never loads the event or worker
MPMs, and neither should a production deployment of this build:
running an NTS mod_php under a threaded MPM is unsupported and can
corrupt request state — the class of bug this security fix is about.

`apxs2` supplies the Apache/APR include paths and the module install
layout at PHP configure time. The build does *not* run
`make install` for PHP: the smoke test loads the module directly from
the build tree, so the CI job cannot accidentally test (or be
confused by) a distribution PHP module.

## Running it locally

Requirements: an Apache httpd with development files (`apxs`), a C
toolchain, and the same packages the CI job installs
(`apache2 apache2-dev` on Debian/Ubuntu).

```sh
curl -fsSLO https://www.php.net/distributions/php-7.3.33.tar.xz
echo "166eaccde933381da9516a2b70ad0f447d7cec4b603d07b9a916032b215b90cc  php-7.3.33.tar.xz" | sha256sum -c -
tar xf php-7.3.33.tar.xz && cd php-7.3.33
patch -p1 < /path/to/php-7.3.33-php2shell-backport.patch
./configure --disable-all --disable-libxml --enable-session \
  --enable-json --enable-ctype --enable-tokenizer \
  --with-apxs2=$(command -v apxs2 || command -v apxs)
make -j"$(nproc)"
python3 /path/to/repo/ci/apache_smoke.py "$(command -v apache2 || command -v httpd)" \
  "$PWD/libs/libphp7.so"
```

Expected: every check prints PASS and the script ends with
`APACHE SMOKE: PASS` (exit code 0).

## What remains untested

- **Your application.** This job tests a generic httpd + the patched
  module with synthetic scripts. It does not run any real application
  code, framework, extension set, or production Apache configuration.
- Long-running load/soak behaviour, TLS termination, proxy setups,
  `.htaccess`-driven configuration, and threaded MPMs (unsupported
  with this build, see above).
- Other SAPIs beyond CLI, FPM and mod_php.
- Performance regression comparisons against stock 7.3.33.

## Why this does not replace application staging tests

Passing generic Apache CI proves the patched interpreter behaves
correctly under mod_php in a clean-room configuration. An application
deployment adds its own Dockerfile, extension set (in the consuming
project's case e.g. mcrypt, imagick, redis, apcu built via PECL),
Apache configuration and workload — none of which exist here, by
design: this repository stays application-independent. A canary/staging
build and run of the actual application image remains a separate,
required validation step before production use.
