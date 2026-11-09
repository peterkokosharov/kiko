/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kt_test.h - minimal assertion macros; every test file is its own executable (CTest runs them).
 */
#ifndef KT_TEST_H
#define KT_TEST_H

#include <stdio.h>
#include <string.h>

static int kt_failures = 0;

#define CHECK(cond) \
    do { if (!(cond)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); kt_failures++; } } while (0)

#define CHECK_STR(a, b) \
    do { const char *a_ = (a); const char *b_ = (b); \
         if (!a_ || !b_ || strcmp(a_, b_) != 0) { \
             printf("FAIL %s:%d: \"%s\" != \"%s\"\n", __FILE__, __LINE__, a_ ? a_ : "(null)", b_ ? b_ : "(null)"); \
             kt_failures++; } } while (0)

#define CHECK_INT(a, b) \
    do { long a_ = (long)(a); long b_ = (long)(b); \
         if (a_ != b_) { printf("FAIL %s:%d: %ld != %ld (%s)\n", __FILE__, __LINE__, a_, b_, #a); kt_failures++; } } while (0)

#define KT_TEST_DONE() \
    do { if (kt_failures) { printf("%d check(s) failed\n", kt_failures); return 1; } printf("ok\n"); return 0; } while (0)

#endif
