/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * mac_main.m - kiko-tray for macOS (menu-bar app, LSUIElement).  UNVERIFIED: never compiled.
 * Environment: KIKO_PORT (default 4000), KIKO_API_TOKEN or the file ~/.kiko/api-token.
 */
#import <Cocoa/Cocoa.h>
#include <string.h>

#include "kt_util.h"
#include "mac_internal.h"

#ifndef KT_VERSION
#define KT_VERSION "0.1.0"
#endif

/* First line of ~/.kiko/api-token, or NULL.  The value is never logged. */
static char *read_token_file(void)
{
    NSString *path = [NSHomeDirectory() stringByAppendingPathComponent:@".kiko/api-token"];
    NSString *s = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil];
    if (!s) return NULL;
    NSString *line = [[s componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet] firstObject];
    line = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    return line.length ? kt_strdup(line.UTF8String) : NULL;
}

int main(int argc, const char *argv[])
{
    (void)argc; (void)argv;
    @autoreleasepool {
        NSDictionary *env = NSProcessInfo.processInfo.environment;
        int port = 4000;
        char *token = NULL;
        const char *token_source = "none";
        kt_view view;
        kt_host host;
        kt_config cfg;

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];   /* no Dock icon */
        kt_net_startup();

        NSString *p = env[@"KIKO_PORT"];
        if (p.intValue > 0 && p.intValue < 65536) port = p.intValue;
        NSString *t = env[@"KIKO_API_TOKEN"];
        if (t.length) { token = kt_strdup(t.UTF8String); token_source = "env KIKO_API_TOKEN"; }
        else { token = read_token_file(); if (token) token_source = "file ~/.kiko/api-token"; }

        if (mac_view_create(&view) != 0) return 1;
        if (!view.claim_single_instance(view.ctx)) {
            fprintf(stderr, "kiko-tray is already running\n");
            return 0;
        }
        memset(&cfg, 0, sizeof cfg);
        cfg.port = port;
        cfg.api_token = token;
        cfg.token_source = token_source;
        cfg.command_text = "launchctl (LaunchAgent com.kiko.lite)";
        cfg.version = KT_VERSION;
        cfg.poll_interval_ms = 5000;
        cfg.start_grace_ms = 30000;
        host.ctx = NULL;
        host.service = mac_host_service;

        kt_presenter *pr = kt_presenter_new(&view, &host, &cfg);
        if (!pr || kt_presenter_start(pr) != 0) {
            fprintf(stderr, "kiko-tray: could not start\n");
            kt_presenter_free(pr);
            mac_view_destroy();
            return 1;
        }
        view.run(view.ctx);
        kt_presenter_stop(pr);
        kt_presenter_free(pr);
        mac_view_destroy();
        if (token) { kt_secure_zero(token, strlen(token)); free(token); }
    }
    return 0;
}
