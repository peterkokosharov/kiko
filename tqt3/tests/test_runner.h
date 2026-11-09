// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Nemotron 3 Ultra
/*
 * kiko-tqt3 Test Runner - Minimal TQt3 test framework
 */

#ifndef TEST_RUNNER_H
#define TEST_RUNNER_H

#include <tqobject.h>
#include <tqstring.h>
#include <tqvaluelist.h>
#include <stdio.h>
#include <string.h>

struct TestCase {
    const char *name;
    bool (*func)();
};

struct TestSuite {
    const char *name;
    TestCase *cases;
    int count;
    int capacity;
};

extern TQValueList<TestSuite *> g_test_suites;

void test_suite_init(TestSuite *suite, const char *name);
void test_suite_add(TestSuite *suite, const char *name, bool (*func)());
void test_suite_free(TestSuite *suite);
int run_all_tests(int argc, char **argv);

extern const char *current_suite;
extern const char *current_test;
void print_result(const char *suite, const char *test, bool passed, const char *msg);

void test_suite_init(TestSuite *suite, const char *name);
void test_suite_add(TestSuite *suite, const char *name, bool (*func)());
void test_suite_free(TestSuite *suite);
int run_all_tests(int argc, char **argv);

// Test macros
#define TEST_SUITE(name) \
    static TestSuite name; \
    void name##_register_all()

#define ASSERT_TRUE(cond, msg) \
    do { \
        if (!(cond)) { \
            print_result(current_suite, current_test, false, msg); \
            return false; \
        } \
    } while(0)

#define ASSERT_FALSE(cond, msg) \
    do { \
        if (cond) { \
            print_result(current_suite, current_test, false, msg); \
            return false; \
        } \
    } while(0)

#define ASSERT_EQ(a, b) \
    do { \
        if ((a) != (b)) { \
            char msg[256]; \
            snprintf(msg, sizeof(msg), "Expected %s == %s", #a, #b); \
            print_result(current_suite, current_test, false, msg); \
            return false; \
        } \
    } while(0)

#define ASSERT_STR_EQ(a, b) \
    do { \
        if (strcmp((a), (b)) != 0) { \
            char msg[256]; \
            snprintf(msg, sizeof(msg), "Expected \"%s\" == \"%s\"", (a), (b)); \
            print_result(current_suite, current_test, false, msg); \
            return false; \
        } \
    } while(0)

extern const char *current_suite;
extern const char *current_test;
void print_result(const char *suite, const char *test, bool passed, const char *msg);

void test_suite_init(TestSuite *suite, const char *name);
void test_suite_add(TestSuite *suite, const char *name, bool (*func)());
void test_suite_free(TestSuite *suite);
int run_all_tests(int argc, char **argv);

#endif // TEST_RUNNER_H