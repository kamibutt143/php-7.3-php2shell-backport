#!/usr/bin/env python3
"""Minimal automated FPM smoke test for the patched PHP 7.3.33 (CI).

Starts a throwaway PHP-FPM pool (2 workers, memory_limit=24M) from a
built tree and checks, over real FastCGI:
  1. hello requests succeed with fresh per-request state and the
     patched INI present (max_multipart_body_parts == "-1"),
  2. a memory-exhaustion request is absorbed — on the patched build
     the worker is EXPECTED to die here (the fix's allocator error
     path _exit()s instead of bailing out on possibly-corrupt state),
  3. the FPM master survives and subsequent requests are clean.

Usage: fcgi_smoke.py /path/to/php-fpm
Exit 0 = PASS, 1 = FAIL. Python stdlib only.
"""
import os, socket, struct, subprocess, sys, tempfile, time

# --- minimal FastCGI client (GET only) ---
def _rec(t, rid, content):
    pad = (8 - (len(content) % 8)) % 8
    return struct.pack('!BBHHBB', 1, t, rid, len(content), pad, 0) + content + b'\x00' * pad

def _nv(name, value):
    def ln(n):
        return struct.pack('!I', n | 0x80000000) if n > 127 else bytes([n])
    nb, vb = name.encode(), str(value).encode()
    return ln(len(nb)) + ln(len(vb)) + nb + vb

def request(sock_path, script, timeout=30):
    params = {
        'GATEWAY_INTERFACE': 'FastCGI/1.0', 'REQUEST_METHOD': 'GET',
        'SCRIPT_FILENAME': script,
        'SCRIPT_NAME': '/' + script.rsplit('/', 1)[-1],
        'REQUEST_URI': '/' + script.rsplit('/', 1)[-1],
        'DOCUMENT_ROOT': script.rsplit('/', 1)[0],
        'SERVER_PROTOCOL': 'HTTP/1.1', 'SERVER_SOFTWARE': 'ci-smoke/1.0',
        'REMOTE_ADDR': '127.0.0.1', 'SERVER_ADDR': '127.0.0.1',
        'SERVER_PORT': '80', 'SERVER_NAME': 'ci-smoke',
        'REDIRECT_STATUS': '200', 'CONTENT_LENGTH': '0',
    }
    out, err, complete = b'', b'', False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(_rec(1, 1, struct.pack('!HB5x', 1, 0)))
        blob = b''.join(_nv(k, v) for k, v in params.items())
        s.sendall(_rec(4, 1, blob)); s.sendall(_rec(4, 1, b''))
        s.sendall(_rec(5, 1, b''))
        buf = b''
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:
                _v, rtype, _id, clen, plen, _r = struct.unpack('!BBHHBB', buf[:8])
                if len(buf) < 8 + clen + plen:
                    break
                content = buf[8:8 + clen]
                buf = buf[8 + clen + plen:]
                if rtype == 6:
                    out += content
                elif rtype == 7:
                    err += content
                elif rtype == 3:
                    complete = True
    except OSError:
        pass
    finally:
        s.close()
    body = out.split(b'\r\n\r\n', 1)[-1].decode('utf-8', 'replace')
    status = None
    head = out.split(b'\r\n\r\n', 1)[0].decode('utf-8', 'replace')
    for line in head.split('\r\n'):
        if line.lower().startswith('status:'):
            status = int(line.split(':', 1)[1].split()[0])
    if status is None and out:
        status = 200
    return status, body, complete

# --- smoke run ---
def main():
    fpm = os.path.abspath(sys.argv[1])
    work = tempfile.mkdtemp(prefix='fpm-smoke-')
    docroot = os.path.join(work, 'docroot')
    os.makedirs(docroot)
    with open(os.path.join(docroot, 'hello.php'), 'w') as f:
        f.write("<?php $GLOBALS['hits'][] = 1;\n"
                "printf(\"SMOKE_OK pid=%d hits=%d parts=%s\\n\", getmypid(), "
                "count($GLOBALS['hits']), var_export(ini_get('max_multipart_body_parts'), true));\n")
    with open(os.path.join(docroot, 'oom.php'), 'w') as f:
        f.write('<?php echo "OOM_START\\n"; flush(); $a = []; '
                'while (true) { $a[] = str_repeat("x", 1048576); }\n')
    sock = os.path.join(work, 'fpm.sock')
    pidfile = os.path.join(work, 'fpm.pid')
    conf = os.path.join(work, 'fpm.conf')
    with open(conf, 'w') as f:
        f.write(f"""[global]
pid = {pidfile}
error_log = {os.path.join(work, 'fpm.log')}
daemonize = yes
[smoke]
listen = {sock}
pm = static
pm.max_children = 2
catch_workers_output = yes
php_admin_value[memory_limit] = 24M
php_admin_value[display_errors] = On
""")
    args = [fpm, '-p', work, '-y', conf]
    if os.geteuid() == 0:
        args.append('-R')  # allow root pool (local runs); CI is non-root
    subprocess.run(args, check=True)
    failures = []
    def check(name, cond, detail=''):
        print(('PASS' if cond else 'FAIL'), name, detail)
        if not cond:
            failures.append(name)
    try:
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.2)
        master_pid = int(open(pidfile).read().strip())

        st, body, _c = request(sock, docroot + '/hello.php')
        check('hello #1: 200 + patched INI + fresh state',
              st == 200 and 'SMOKE_OK' in body and "parts='-1'" in body and 'hits=1' in body,
              f'-> {body.strip()[:90]}')
        for i in (2, 3):
            st, body, _c = request(sock, docroot + '/hello.php')
            check(f'hello #{i}: 200 + fresh state',
                  st == 200 and 'hits=1' in body, f'-> {body.strip()[:60]}')

        st, body, complete = request(sock, docroot + '/oom.php', timeout=20)
        print(f'INFO oom response: status={st} complete={complete} '
              f'(patched build: worker exit + incomplete response expected)')
        try:
            os.kill(master_pid, 0)
            alive = True
        except OSError:
            alive = False
        check('FPM master alive after OOM', alive, f'master pid={master_pid}')

        ok = True
        for i in range(3):
            st, body, _c = request(sock, docroot + '/hello.php')
            ok = ok and st == 200 and 'hits=1' in body and 'SMOKE_OK' in body
        check('3x hello after OOM: all clean, no inherited state', ok)
    finally:
        if os.path.exists(pidfile):
            try:
                os.kill(int(open(pidfile).read().strip()), 3)  # SIGQUIT
            except OSError:
                pass
    print('FPM SMOKE:', 'FAIL ' + ', '.join(failures) if failures else 'PASS')
    sys.exit(1 if failures else 0)

if __name__ == '__main__':
    main()
