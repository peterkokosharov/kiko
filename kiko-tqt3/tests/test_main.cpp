/*
 * kiko-tqt3 Test Main Entry Point
 * Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
 * LLM: Nemotron Ultra
 * Licensed under MIT License
 */

#include "test_runner.h"

void kiko_process_state_machine_register_all();

int main(int argc, char **argv) {
    // Register all test suites
    kiko_process_state_machine_register_all();

    // Run all tests
    return run_all_tests(argc, argv);
}