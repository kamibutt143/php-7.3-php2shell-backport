#!/usr/bin/env python3
"""Apache mod_php smoke test for the patched PHP 7.3.33 (CI).

Starts a throwaway Apache httpd (prefork MPM — required for this
non-thread-safe PHP build) on 127.0.0.1:<ephemeral port> with a fully
temporary ServerRoot, config and docroot, loading ONLY the libphp7.so
built from the patched tree (never a system PHP module), and checks
over real HTTP:

  1. runtime identity: SAPI is apache2handler, version is 7.3.33 and
     ini_get('max_multipart_body_parts') === '-1' (a stock 7.3.33 has
     no such INI setting and returns false — this cannot pass against
     an unpatched or system PHP),
  2. request correctness: concat/string ops, GET and POST parameters,
     sequential requests, no cross-request state leakage,
  3. multipart handling: a normal multipart form succeeds; with a
     per-directory php_admin_value max_multipart_body_parts=5, three
     parts pass intact and eight parts are truncated at the limit
     with the upstream warning, still HTTP 200 (verified against
     main/rfc1867.c of the patched tree: on limit excess PHP raises
     E_WARNING "Multipart body parts limit exceeded N..." and stops
     parsing further parts; already-parsed parts are kept. Multipart
     parsing happens during request startup, so that warning is a
     startup-phase error: the test config enables
     display_startup_errors so it is observable over HTTP — it is
     also always written to the Apache error log),
  4. stability: a malformed multipart body does not break the server,
     and after one prefork child is killed the master respawns a
     replacement and requests stay clean (no FPM assumptions — under
     prefork each child serves one request at a time and the parent
     replaces dead children).

Usage: apache_smoke.py /path/to/httpd /path/to/libphp7.so
Exit 0 = PASS, 1 = FAIL. Python stdlib only.

On failure the tail of the Apache error log is printed, and if the
APACHE_SMOKE_ARTIFACT_DIR environment variable is set, the error log
is copied there for CI artifact upload.
"""
import http.client, os, shutil, signal, socket, subprocess, sys, time

BOUNDARY = '----apache-smoke-boundary'

def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p

def fetch(port, method, path, body=None, headers=None, timeout=15):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read().decode('utf-8', 'replace')
        return resp.status, data
    finally:
        conn.close()

def multipart_body(fields, files=None):
    """fields: list of (name, value); files: list of (name, filename, content)."""
    out = []
    for name, value in fields:
        out.append(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
    for name, filename, content in (files or []):
        out.append(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                   f'Content-Type: text/plain\r\n\r\n{content}\r\n')
    out.append(f'--{BOUNDARY}--\r\n')
    return ''.join(out).encode()

PHP_WHOAMI = r"""<?php
printf("SAPI=%s\n", PHP_SAPI);
printf("VERSION=%s\n", PHP_VERSION);
printf("PARTS=%s\n", var_export(ini_get('max_multipart_body_parts'), true));
printf("APACHEFN=%s\n", function_exists('apache_get_version') ? 'yes' : 'no');
"""
PHP_HELLO = r"""<?php $GLOBALS['hits'][] = 1;
printf("SMOKE_OK pid=%d hits=%d\n", getmypid(), count($GLOBALS['hits']));
"""
PHP_CONCAT = r"""<?php $a = "foo"; $a .= "bar"; $b = $a . "baz" . strrev("cba");
printf("CONCAT=%s UPPER=%s LEN=%d\n", $b, strtoupper($b), strlen($b));
"""
PHP_PARAMS = r"""<?php
printf("A=%s B=%s\n", isset($_GET['a']) ? $_GET['a'] : '-', isset($_POST['b']) ? $_POST['b'] : '-');
"""
PHP_MULTIPART = r"""<?php
$keys = array_keys($_POST); sort($keys);
printf("PARTS=%s POST_COUNT=%d POST_KEYS=%s FILE_COUNT=%d\n",
    var_export(ini_get('max_multipart_body_parts'), true),
    count($_POST), implode(',', $keys), count($_FILES));
"""

def find_modules_dir(httpd):
    """Locate the directory holding Apache's own .so modules."""
    candidates = []
    prefix = os.path.dirname(os.path.dirname(os.path.abspath(httpd)))
    candidates.append(os.path.join(prefix, 'modules'))
    candidates.append('/usr/lib/apache2/modules')
    for cand in candidates:
        if os.path.exists(os.path.join(cand, 'mod_authz_core.so')) or \
           os.path.exists(os.path.join(cand, 'mod_mpm_prefork.so')):
            return cand
    return None

def builtin_modules(httpd):
    """Module source names compiled statically into this httpd (httpd -l)."""
    try:
        out = subprocess.run([httpd, '-l'], capture_output=True, text=True,
                             timeout=15).stdout
        return set(line.strip() for line in out.splitlines())
    except Exception:
        return set()

def children_of(pid):
    kids = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/stat') as f:
                parts = f.read().rsplit(') ', 1)[1].split()
            if int(parts[1]) == pid:
                kids.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return kids

def main():
    httpd = os.path.abspath(sys.argv[1])
    php_module = os.path.abspath(sys.argv[2])
    if not os.path.exists(php_module):
        print('FAIL php module not found:', php_module)
        sys.exit(1)
    mods = find_modules_dir(httpd)
    builtins = builtin_modules(httpd)
    # (directive name, .so basename, compiled-in source name in `httpd -l`)
    # Note: a statically linked prefork MPM shows up as "prefork.c",
    # not "mod_mpm_prefork.c".
    LOADABLE = [('mpm_prefork_module', 'mod_mpm_prefork', 'prefork.c'),
                ('authz_core_module', 'mod_authz_core', 'mod_authz_core.c'),
                ('dir_module', 'mod_dir', 'mod_dir.c'),
                ('log_config_module', 'mod_log_config', 'mod_log_config.c'),
                ('unixd_module', 'mod_unixd', 'mod_unixd.c')]
    def load_lines():
        out = []
        for name, so, src in LOADABLE:
            if src in builtins:
                continue
            if mods is None or not os.path.exists(os.path.join(mods, so + '.so')):
                raise SystemExit(f'{so} is not built in and was not found as a .so')
            out.append(f'LoadModule {name} {mods}/{so}.so')
        return '\n'.join(out) + ('\n' if out else '')
    import tempfile
    work = tempfile.mkdtemp(prefix='apache-smoke-')
    # mkdtemp is 0700; when the tests run as root, httpd children run as
    # an unprivileged user and must be able to traverse into the docroot.
    os.chmod(work, 0o755)
    docroot = os.path.join(work, 'docroot')
    limited = os.path.join(docroot, 'limited')
    os.makedirs(limited)
    os.chmod(docroot, 0o755)
    os.chmod(limited, 0o755)
    for name, code in [('whoami.php', PHP_WHOAMI), ('hello.php', PHP_HELLO),
                       ('concat.php', PHP_CONCAT), ('params.php', PHP_PARAMS),
                       ('multipart.php', PHP_MULTIPART)]:
        path = os.path.join(docroot, name)
        with open(path, 'w') as f:
            f.write(code)
        os.chmod(path, 0o644)  # a restrictive umask must not lock httpd children out
    limited_script = os.path.join(limited, 'multipart.php')
    with open(limited_script, 'w') as f:
        f.write(PHP_MULTIPART)
    os.chmod(limited_script, 0o644)

    port = free_port()
    pidfile = os.path.join(work, 'httpd.pid')
    errlog = os.path.join(work, 'error.log')
    extra_conf = ''
    if os.geteuid() == 0:
        extra_conf = 'User nobody\nGroup nogroup\n'
    conf = os.path.join(work, 'httpd.conf')
    with open(conf, 'w') as f:
        f.write(f"""ServerRoot {work}
PidFile {pidfile}
ErrorLog {errlog}
LogLevel warn
Listen 127.0.0.1:{port}
ServerName 127.0.0.1
{load_lines()}LoadModule php7_module {php_module}
{extra_conf}StartServers 3
MinSpareServers 2
MaxSpareServers 4
ServerLimit 6
MaxRequestWorkers 6
DocumentRoot {docroot}
<Directory {docroot}>
  Require all granted
</Directory>
<FilesMatch "\\.php$">
  SetHandler application/x-httpd-php
</FilesMatch>
php_admin_value display_errors On
php_admin_value display_startup_errors On
php_admin_value upload_max_filesize 2M
<Directory {limited}>
  php_admin_value max_multipart_body_parts 5
</Directory>
""")

    failures = []
    def check(name, cond, detail=''):
        print(('PASS' if cond else 'FAIL'), name, detail, flush=True)
        if not cond:
            failures.append(name)

    httpd_out = open(os.path.join(work, 'httpd-stdout.log'), 'wb')
    proc = subprocess.Popen([httpd, '-f', conf, '-DFOREGROUND'],
                            stdout=httpd_out, stderr=subprocess.STDOUT)
    try:
        # --- startup: wait for the port, fail closed if httpd dies ---
        up = False
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                socket.create_connection(('127.0.0.1', port), timeout=2).close()
                up = True
                break
            except OSError:
                time.sleep(0.25)
        check('apache starts and listens', up, f'port={port}')
        if not up:
            raise RuntimeError('apache did not start')

        # --- 1. runtime identity ---
        st, body = fetch(port, 'GET', '/whoami.php')
        check('whoami: 200 + apache2handler + 7.3.33 + patched INI + apache fn',
              st == 200 and 'SAPI=apache2handler' in body and 'VERSION=7.3.33' in body
              and "PARTS='-1'" in body and 'APACHEFN=yes' in body,
              f'-> {body.strip()[:110]!r}')

        # --- 2. request correctness ---
        ok = True
        for _ in range(3):
            st, body = fetch(port, 'GET', '/hello.php')
            ok = ok and st == 200 and 'SMOKE_OK' in body and 'hits=1' in body
        check('hello x3: 200 + fresh per-request state (no leakage)', ok)

        st, body = fetch(port, 'GET', '/concat.php')
        check('concat/string ops', st == 200 and 'CONCAT=foobarbazabc' in body
              and 'UPPER=FOOBARBAZABC' in body and 'LEN=12' in body, f'-> {body.strip()[:60]!r}')

        st, body = fetch(port, 'GET', '/params.php?a=hello')
        check('GET parameter', st == 200 and 'A=hello' in body, f'-> {body.strip()[:40]!r}')
        st, body = fetch(port, 'POST', '/params.php', body='b=world',
                        headers={'Content-Type': 'application/x-www-form-urlencoded'})
        check('POST parameter', st == 200 and 'B=world' in body, f'-> {body.strip()[:40]!r}')

        # --- 3. multipart behavior ---
        mp_headers = {'Content-Type': f'multipart/form-data; boundary={BOUNDARY}'}
        body3 = multipart_body([('f1', 'v1'), ('f2', 'v2')], [('up', 'a.txt', 'hello-file')])
        st, body = fetch(port, 'POST', '/multipart.php', body=body3, headers=mp_headers)
        check('multipart (default -1): 3 parts parsed',
              st == 200 and 'POST_COUNT=2' in body and 'FILE_COUNT=1' in body
              and 'limit exceeded' not in body, f'-> {body.strip()[:90]!r}')

        st, body = fetch(port, 'POST', '/limited/multipart.php', body=body3, headers=mp_headers)
        check('multipart (limit=5): 3 parts pass intact',
              st == 200 and "PARTS='5'" in body and 'POST_COUNT=2' in body
              and 'FILE_COUNT=1' in body and 'limit exceeded' not in body,
              f'-> {body.strip()[:90]!r}')

        body8 = multipart_body([(f'k{i}', f'v{i}') for i in range(1, 9)])
        st, body = fetch(port, 'POST', '/limited/multipart.php', body=body8, headers=mp_headers)
        check('multipart (limit=5): 8 parts truncated at limit + warning, still 200',
              st == 200 and "PARTS='5'" in body and 'POST_COUNT=5' in body
              and 'Multipart body parts limit exceeded 5' in body,
              f'-> {body.strip()[:110]!r}')

        # --- 4. stability ---
        st, body = fetch(port, 'POST', '/multipart.php',
                        body=b'--wrong-boundary\r\n\r\ngarbage-not-a-form\r\n',
                        headers=mp_headers)
        st2, body2 = fetch(port, 'GET', '/hello.php')
        check('malformed multipart: answered, server healthy after',
              st is not None and st2 == 200 and 'hits=1' in body2, f'status={st}')

        # kill one prefork child; master must replace it and stay up
        master_pid = int(open(pidfile).read().strip())
        kids = children_of(master_pid)
        if kids:
            os.kill(kids[0], signal.SIGKILL)
            time.sleep(1.0)
            try:
                os.kill(master_pid, 0)
                alive = True
            except OSError:
                alive = False
            st, body = fetch(port, 'GET', '/hello.php')
            check('prefork child killed: master alive, requests clean',
                  alive and st == 200 and 'hits=1' in body, f'killed child {kids[0]}')
        else:
            print('INFO no prefork children found; child-recovery check skipped', flush=True)

        ok = True
        for _ in range(3):
            st, body = fetch(port, 'GET', '/hello.php')
            ok = ok and st == 200 and 'hits=1' in body and 'SMOKE_OK' in body
        check('final hello x3: all clean', ok)
    except Exception as e:  # fail closed with diagnostics
        print('ERROR', e)
        failures.append(f'exception: {e}')

    # Diagnostics and the summary are emitted BEFORE shutdown: in CI the
    # step died with SIGTERM (exit 143) during shutdown cleanup, after
    # all checks had passed but before the summary printed, which made
    # a fully-passing run report as a failure. Print first, stop after.
    if failures and os.path.exists(errlog):
        print('--- apache error.log (tail) ---')
        with open(errlog, errors='replace') as f:
            print(''.join(f.readlines()[-40:]))
        dest = os.environ.get('APACHE_SMOKE_ARTIFACT_DIR')
        if dest:
            os.makedirs(dest, exist_ok=True)
            shutil.copy(errlog, os.path.join(dest, 'apache-error.log'))
            shutil.copy(conf, os.path.join(dest, 'httpd.conf'))
    print('APACHE SMOKE:', 'FAIL ' + ', '.join(failures) if failures else 'PASS', flush=True)
    result = 1 if failures else 0

    # Stop the master directly with a signal to the pidfile pid (an
    # earlier version used `httpd -k stop`, which re-parses the config
    # in a fresh process — an unnecessary moving part at this point).
    try:
        master = int(open(pidfile).read().strip())
        os.kill(master, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    sys.exit(result)

if __name__ == '__main__':
    main()
