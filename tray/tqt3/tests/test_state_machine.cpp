// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Nemotron 3 Ultra
/*
 * kiko-tqt3 KikoProcess State Machine Tests
 */

#include "test_runner.h"
#include <tqstring.h>
#include <string.h>

#include "../src/kiko_process.h"

static bool test_initial_state_is_stopped() {
    KikoProcess *proc = kiko_process_new("test-service");
    ASSERT_EQ(kiko_process_get_state(proc), KIKO_STATE_STOPPED);
    ASSERT_STR_EQ(kiko_process_get_service_name(proc), "test-service");
    kiko_process_free(proc);
    return true;
}

static bool test_state_transitions() {
    KikoProcess *proc = kiko_process_new("test-service");

    proc->state = KIKO_STATE_STARTING;
    ASSERT_EQ(kiko_process_get_state(proc), KIKO_STATE_STARTING);

    proc->state = KIKO_STATE_RUNNING;
    ASSERT_EQ(kiko_process_get_state(proc), KIKO_STATE_RUNNING);

    proc->state = KIKO_STATE_STOPPING;
    ASSERT_EQ(kiko_process_get_state(proc), KIKO_STATE_STOPPING);

    proc->state = KIKO_STATE_STOPPED;
    ASSERT_EQ(kiko_process_get_state(proc), KIKO_STATE_STOPPED);

    proc->state = KIKO_STATE_ERROR;
    ASSERT_EQ(kiko_process_get_state(proc), KIKO_STATE_ERROR);

    kiko_process_free(proc);
    return true;
}

static bool test_state_to_string() {
    ASSERT_STR_EQ(kiko_state_to_string(KIKO_STATE_STOPPED), "Stopped");
    ASSERT_STR_EQ(kiko_state_to_string(KIKO_STATE_STARTING), "Starting");
    ASSERT_STR_EQ(kiko_state_to_string(KIKO_STATE_RUNNING), "Running");
    ASSERT_STR_EQ(kiko_state_to_string(KIKO_STATE_STOPPING), "Stopping");
    ASSERT_STR_EQ(kiko_state_to_string(KIKO_STATE_ERROR), "Error");
    ASSERT_STR_EQ(kiko_state_to_string((KikoState)99), "Unknown");
    return true;
}

static bool test_service_name_handling() {
    KikoProcess *proc = kiko_process_new("my-custom-service");
    ASSERT_STR_EQ(kiko_process_get_service_name(proc), "my-custom-service");
    kiko_process_free(proc);

    KikoProcess *proc2 = kiko_process_new("");
    ASSERT_STR_EQ(kiko_process_get_service_name(proc2), "");
    kiko_process_free(proc2);

    return true;
}

TEST_SUITE(kiko_process_state_machine);

void kiko_process_state_machine_register_all() {
    test_suite_init(&kiko_process_state_machine, "kiko_process_state_machine");
    test_suite_add(&kiko_process_state_machine, "initial_state_is_stopped", test_initial_state_is_stopped);
    test_suite_add(&kiko_process_state_machine, "state_transitions", test_state_transitions);
    test_suite_add(&kiko_process_state_machine, "state_to_string", test_state_to_string);
    test_suite_add(&kiko_process_state_machine, "service_name_handling", test_service_name_handling);
}