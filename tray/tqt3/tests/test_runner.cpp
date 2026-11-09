// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Nemotron 3 Ultra
/*
 * kiko-tqt3 Test Runner - Minimal TQt3 test framework
 */

#include "test_runner.h"
#include <tqapplication.h>
#include <tqstring.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

TQValueList<TestSuite *> g_test_suites;

static int g_passed = 0;
static int g_failed = 0;
static int g_total = 0;
const char *current_suite = "";
const char *current_test = "";

void test_suite_init(TestSuite *suite, const char *name) {
    suite->name = name;
    suite->cases = NULL;
    suite->count = 0;
    suite->capacity = 0;
    g_test_suites.append(suite);
}

void test_suite_add(TestSuite *suite, const char *name, bool (*func)()) {
    if (suite->count >= suite->capacity) {
        int new_cap = suite->capacity ? suite->capacity * 2 : 4;
        suite->cases = (TestCase*)realloc(suite->cases, new_cap * sizeof(TestCase));
        suite->capacity = new_cap;
    }
    suite->cases[suite->count].name = name;
    suite->cases[suite->count].func = func;
    suite->count++;
}

void test_suite_free(TestSuite *suite) {
    free(suite->cases);
    suite->cases = NULL;
    suite->count = 0;
    suite->capacity = 0;
}

void print_result(const char *suite, const char *test, bool passed, const char *msg) {
    if (passed) {
        printf("  [PASS] %s::%s\n", suite, test);
        g_passed++;
    } else {
        printf("  [FAIL] %s::%s", suite, test);
        if (msg) printf(" - %s", msg);
        printf("\n");
        g_failed++;
    }
    g_total++;
}

int run_all_tests(int argc, char **argv) {
    TQApplication app(argc, argv);

    printf("=== kiko-tqt3 Test Suite ===\n\n");

    TQValueList<TestSuite *>::Iterator it = g_test_suites.begin();
    while (it != g_test_suites.end()) {
        TestSuite *suite = *it;
        current_suite = suite->name;
        printf("[SUITE] %s\n", suite->name);

        for (int i = 0; i < suite->count; i++) {
            current_test = suite->cases[i].name;
            bool result = suite->cases[i].func();
            if (result) {
                print_result(current_suite, current_test, true, NULL);
            }
        }
        printf("\n");
        ++it;
    }

    printf("=== Summary ===\n");
    printf("Total: %d, Passed: %d, Failed: %d\n", g_total, g_passed, g_failed);

    return g_failed == 0 ? 0 : 1;
}