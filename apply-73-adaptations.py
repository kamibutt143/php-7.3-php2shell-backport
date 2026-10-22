#!/usr/bin/env python3
"""7.3-specific adaptations for porting php-src commit 712e60f
(PHP-7.4-CVE-2026-109618-109619, "Avoid state corruption via bailout")
to stock PHP 7.3.33. Run AFTER `patch -p1 < cve-74-commit.patch`
in the 7.3.33 tree. The upstream patch applies cleanly except for
the pieces handled here:

1. Zend/zend_alloc.c    - failed hunk: #include "zend_execute.h"
                          (7.3 wraps <signal.h> differently, context mismatch only)
2. Zend/zend_vm_def.h   - failed hunk: ZEND_CONCAT in-place-extend branch has
                          NO overflow guard at all in 7.3; add the guard in
                          patched-7.4 form instead of converting one.
3. Zend/zend_string.h   - ZSTR_MAX_LEN / ZSTR_MAX_OVERHEAD do not exist in 7.3
                          (added upstream in 7.4); the fix's concat guards use
                          them. Definitions copied verbatim from PHP 7.4.
4. Zend/zend_execute_API.c - failed hunk: 7.3 shutdown_executor() layout differs
                          (no weakrefs; handler cleanup happens in a later phase).
                          Same intent, relocated: (a) EH_NORMAL/exception reset
                          right after zend_objects_store_free_object_storage(),
                          as in 7.4; (b) handler neutralization inside 7.3's
                          fast-shutdown branch, where handlers are skipped.

Zend/zend_vm_execute.h is NOT edited here: it is generated from
zend_vm_def.h. After a stage-1 build, regenerate it with:
    sapi/cli/php Zend/zend_vm_gen.php
then rebuild (stage 2).
"""
import sys

def patch(path, old, new, count=1):
    s = open(path).read()
    assert s.count(old) == count, f"{path}: anchor found {s.count(old)}x, expected {count}"
    open(path, "w").write(s.replace(old, new, count))
    print(f"OK {path}")

# 1) zend_alloc.c include
patch("Zend/zend_alloc.c",
      '#include "zend_bitset.h"\n',
      '#include "zend_bitset.h"\n#include "zend_execute.h"\n')

# 2) zend_vm_def.h CONCAT guard (absent in 7.3)
patch("Zend/zend_vm_def.h",
      """		    size_t len = ZSTR_LEN(op1_str);

			str = zend_string_extend(op1_str, len + ZSTR_LEN(op2_str), 0);""",
      """		    size_t len = ZSTR_LEN(op1_str);

			if (UNEXPECTED(len > ZSTR_MAX_LEN - ZSTR_LEN(op2_str))) {
				zend_mm_safe_error_ex("Integer overflow in memory allocation");
			}
			str = zend_string_extend(op1_str, len + ZSTR_LEN(op2_str), 0);""")

# 3) zend_string.h ZSTR_MAX_LEN macros (from PHP 7.4)
patch("Zend/zend_string.h",
      "#define _ZSTR_STRUCT_SIZE(len) (_ZSTR_HEADER_SIZE + len + 1)\n",
      """#define _ZSTR_STRUCT_SIZE(len) (_ZSTR_HEADER_SIZE + len + 1)

/* Backported from PHP 7.4 (required by the CVE-2026-109618/109619 fix,
 * which references ZSTR_MAX_LEN in the string-concat overflow guards). */
#define ZSTR_MAX_OVERHEAD (ZEND_MM_ALIGNED_SIZE(_ZSTR_HEADER_SIZE + 1))
#define ZSTR_MAX_LEN (SIZE_MAX - ZSTR_MAX_OVERHEAD)
""")

# 4a) zend_execute_API.c: EH reset after free_object_storage (as in 7.4)
patch("Zend/zend_execute_API.c",
      """	zend_objects_store_free_object_storage(&EG(objects_store), fast_shutdown);
""",
      """	zend_objects_store_free_object_storage(&EG(objects_store), fast_shutdown);

	/* Backport of PHP-7.4-CVE-2026-109618-109619: may be used by
	 * zend_error_zstr_at before init_executor. */
	EG(error_handling) = EH_NORMAL;
	EG(exception) = NULL;
""")

# 4b) zend_execute_API.c: neutralize handlers in fast-shutdown branch
patch("Zend/zend_execute_API.c",
      """		zend_cleanup_internal_classes();
		EG(objects_store).object_buckets = NULL;""",
      """		zend_cleanup_internal_classes();

		/* Backport of PHP-7.4-CVE-2026-109618-109619: in fast shutdown the
		 * handlers are not destroyed below, so neutralize them here.
		 * They may be used by zend_error_zstr_at before init_executor. */
		ZVAL_UNDEF(&EG(user_error_handler));
		ZVAL_UNDEF(&EG(user_exception_handler));
		memset(&EG(user_error_handlers_error_reporting), 0, sizeof(EG(user_error_handlers_error_reporting)));
		memset(&EG(user_error_handlers), 0, sizeof(EG(user_error_handlers)));
		memset(&EG(user_exception_handlers), 0, sizeof(EG(user_exception_handlers)));

		EG(objects_store).object_buckets = NULL;""")

print("All 7.3 adaptations applied.")
