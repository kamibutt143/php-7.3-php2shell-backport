# How to apply this patch

Two deployment methods: **(A) VM / bare metal / EC2** and **(B) Docker**.
Both build PHP 7.3.33 from the official php.net source with
`php-7.3.33-php2shell-backport.patch` applied.

## Read first — applies to both methods

- **Back up / snapshot first** (AMI, VM snapshot, or keep the previous
  Docker image tagged). Roll back if anything looks wrong; do not debug
  on the only copy.
- **Canary first**: patch a dev/staging instance, run the app's smoke
  tests, then production. Never start with prod.
- **`php -v` does not change.** The patched build still reports
  `PHP 7.3.33` — there is no version bump for a backport. Proof of
  patching is, in order:
  1. the build record / marker file created below
     (`/usr/local/php-backport-7.3.txt` on a VM, an image label in Docker),
  2. the runtime check:
     `php -r 'var_dump(ini_get("max_multipart_body_parts"));'`
     must print `string(2) "-1"` — that INI setting only exists in the
     patched build,
  3. your app working normally.
- **Defense in depth**: also set `session.upload_progress.enabled=0`
  at `PHP_INI_SYSTEM` level (php.ini / FPM pool config / Apache
  `php_admin_value` — *not* `ini_set()` or `.user.ini`). The patch
  already disables the upload-progress callback in code; the setting
  costs nothing and protects any unpatched binary still on the box.
- **No `buildconf` needed.** The release tarball ships a ready
  `configure`; the patch touches only `.c`/`.h` files.
- **Extension ABI is unchanged** (20180731). Extensions built for stock
  7.3 keep loading — provided the new build uses the **same**
  `extension_dir` as the old one (see the warning in method A — this
  bit us in practice on 7.4).

---

## Method A — VM / bare metal / EC2 (side-by-side build)

Recommended pattern: build into `/opt/php73-patched` next to the
existing install, verify, then point the service at it. The distro
package stays untouched, so rollback is a symlink/unit flip.

```bash
# 0. Prep (Debian/Ubuntu names; use your distro's equivalents)
sudo apt-get install -y build-essential pkg-config patch curl \
  libxml2-dev libssl-dev libcurl4-openssl-dev libonig-dev \
  libreadline-dev libsqlite3-dev libzip-dev
#    ^ adjust to YOUR current build: the patched build must support the
#      same extensions as the one it replaces (check: php -m)

# 1. Get stock source + this patch, verify the tarball
cd /usr/local/src
curl -LO https://www.php.net/distributions/php-7.3.33.tar.xz
echo "166eaccde933381da9516a2b70ad0f447d7cec4b603d07b9a916032b215b90cc  php-7.3.33.tar.xz" | sha256sum -c -
curl -LO https://github.com/kamibutt143/php-7.3-php2shell-backport/raw/main/php-7.3.33-php2shell-backport.patch
tar xf php-7.3.33.tar.xz && cd php-7.3.33

# 2. Apply the patch (must complete with no FAILED hunks / no .rej files)
patch -p1 < ../php-7.3.33-php2shell-backport.patch
find . -name '*.rej' | grep . && echo "STOP: rejects present" || echo "patch clean"

# 3. Configure: reuse YOUR current build's own flags, change only the prefix
php-config --configure-options        # review these — this is your baseline
./configure $(php-config --configure-options) --prefix=/opt/php73-patched
#    For mod_php also keep: --with-apxs2=/usr/bin/apxs2 (Debian path)
#    and your existing --with-config-file-path / --with-config-file-scan-dir
#    so the patched build reads the same php.ini / conf.d as production.

# 4. Build + quick self-test
make -j"$(nproc)"
./sapi/cli/php -v
./sapi/cli/php -r 'var_dump(ini_get("max_multipart_body_parts"));'   # string(2) "-1"

# 5. Install side-by-side + leave a build record
sudo make install
sha256sum ../php-7.3.33-php2shell-backport.patch | sudo tee /opt/php73-patched/php-backport-7.3.txt
echo "base: php-7.3.33 | upstream fix: php/php-src@712e60f (PHP-7.4-CVE-2026-109618-109619)" | sudo tee -a /opt/php73-patched/php-backport-7.3.txt
```

### ⚠️ extension_dir warning (learned the hard way on 7.4)
A source build's default `extension_dir` points under its own prefix.
If your production extensions live in the distro directory
(e.g. `/usr/lib/php/20180731`), the patched SAPI will silently load
**none** of them. Either configure with the production `extension_dir`,
or add it to the patched build's php.ini / conf.d drop-in:

```ini
extension_dir = "/usr/lib/php/20180731"
```
Verify with `/opt/php73-patched/bin/php -m` — the module list must
match production's `php -m` exactly before you swap anything.

### Swap — FPM
```bash
# Point the pool at the patched binary (new systemd unit cloned from
# the current one, or repoint the existing unit's ExecStart), plus:
#   php_admin_value[session.upload_progress.enabled] = 0  in the pool
sudo systemctl daemon-reload
sudo systemctl restart php73-patched-fpm        # (your unit name)
/opt/php73-patched/sbin/php-fpm -t              # config test
curl -sI https://your-app.example/ | head -3    # app responds
```
### Swap — Apache mod_php
Back up the current module first (`libphp7.3.so`), install the patched
one from `libs/` in the build tree, add a conf.d drop-in with
`session.upload_progress.enabled=0` (+ `extension_dir` if needed),
`apachectl configtest`, then a **full restart** (not graceful, so no
old-code children survive).

### Rollback
Stop the patched service, start the original one (or restore the
backed-up module / the snapshot). Nothing in this method modifies the
distro install, so rollback is minutes, not a rebuild.

---

## Method B — Docker

The official `php:7.3` images are frozen at 7.3.33 and will never be
updated — rebuild the core inside the image from the source the image
itself carries (`docker-php-source`).

### B1. Debian variants (`php:7.3.33-fpm`, `-cli`, `-apache`)

```dockerfile
# Keep your existing base image line; pin the exact 7.3.33 tag.
FROM php:7.3.33-fpm

# 1. This repo's patch file must sit next to the Dockerfile,
#    or download it in the build instead (next line, commented).
COPY php-7.3.33-php2shell-backport.patch /tmp/backport.patch
# RUN curl -fsSL -o /tmp/backport.patch \
#   https://github.com/kamibutt143/php-7.3-php2shell-backport/raw/main/php-7.3.33-php2shell-backport.patch

# 2. Rebuild the core from the image's own source + configure flags
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends patch $PHPIZE_DEPS; \
    docker-php-source extract; \
    cd /usr/src/php; \
    patch -p1 < /tmp/backport.patch; \
    ./configure $(php-config --configure-options); \
    make -j"$(nproc)"; \
    make install; \
    cd /; docker-php-source delete; \
    rm /tmp/backport.patch; \
    apt-get purge -y --auto-remove $PHPIZE_DEPS; \
    rm -rf /var/lib/apt/lists/*

# 3. Defense in depth + build record inside the image
RUN { echo 'session.upload_progress.enabled=0'; } > /usr/local/etc/php/conf.d/99-php2shell.ini; \
    echo 'php-7.3.33 + backport of php/php-src@712e60f (CVE-2026-109618/109619)' > /usr/local/php-backport-7.3.txt

LABEL org.opencontainers.image.description="PHP 7.3.33 with unofficial php2shell backport (CVE-2026-109618/109619)" \
      php.backport.source="php/php-src@712e60f9edfd03f0960fc4fc3c2c375f42b90aa2"
```

Notes:
- Put this block **after** your `docker-php-ext-install` steps; the
  extensions already installed keep working (same ABI), and
  `make install` does not remove them.
- For the `-apache` variant the same block works; the stock image's
  configure options already carry its apxs2 setting — reusing
  `php-config --configure-options` preserves it.

### B2. Alpine variant (`php:7.3.33-fpm-alpine`)
Same shape; swap the package steps:
```dockerfile
RUN set -eux; \
    apk add --no-cache patch; \
    apk add --no-cache --virtual .build-deps $PHPIZE_DEPS; \
    docker-php-source extract; \
    cd /usr/src/php; \
    patch -p1 < /tmp/backport.patch; \
    ./configure $(php-config --configure-options); \
    make -j"$(nproc)"; make install; \
    cd /; docker-php-source delete; \
    apk del .build-deps
```

### B3. Verify the image before deploying
```bash
docker build -t myapp-php:7.3.33-backport1 .
docker run --rm myapp-php:7.3.33-backport1 php -v
docker run --rm myapp-php:7.3.33-backport1 php -r 'var_dump(ini_get("max_multipart_body_parts"));'
#  -> string(2) "-1"   (patched; stock prints bool(false))
docker run --rm myapp-php:7.3.33-backport1 php -m   # compare with your old image
```
Compose: point the service's `build:` at the Dockerfile and deploy the
new tag to **one** replica first (canary), watch logs, then roll out.
Rollback = redeploy the previous image tag.

---

## Behavior change (both methods)
Session **upload progress** stops populating
(`$_SESSION['upload_progress_…']` stays empty) — the official fix
disables that callback to remove the CVE-2026-109619 vector. If your
app uses upload progress bars backed by it, they will silently stop
updating; everything else is unchanged. See `BUILD-AND-VERIFY.md` for
the full test evidence (2727 passed; the only new failures are the
8 upload-progress tests, failing by upstream design).
