/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * mac_host.m - start/stop kiko through launchctl and the user LaunchAgent com.kiko.lite
 * (sample: docs/macos/com.kiko.lite.plist, installed to ~/Library/LaunchAgents).
 * UNVERIFIED: written without a Mac; the launchctl verbs follow the modern (macOS 11+) syntax.
 */
#import <Foundation/Foundation.h>
#include <unistd.h>

#include "mac_internal.h"

static int run_launchctl(NSArray<NSString *> *args, NSString **err_out)
{
    NSTask *t = [[NSTask alloc] init];
    NSPipe *errp = [NSPipe pipe];
    t.executableURL = [NSURL fileURLWithPath:@"/bin/launchctl"];
    t.arguments = args;
    t.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    t.standardError = errp;
    NSError *e = nil;
    if (![t launchAndReturnError:&e]) {
        if (err_out) *err_out = e.localizedDescription ?: @"cannot run launchctl";
        return -1;
    }
    NSData *d = [errp.fileHandleForReading readDataToEndOfFile];
    [t waitUntilExit];
    if (err_out) {
        NSString *s = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding];
        *err_out = [s stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    }
    return t.terminationStatus;
}

int mac_host_service(void *ctx, kt_action action, char *msg, size_t msgsz)
{
    (void)ctx;
    @autoreleasepool {
        NSString *domain = [NSString stringWithFormat:@"gui/%u", (unsigned)getuid()];
        NSString *target = [domain stringByAppendingString:@"/com.kiko.lite"];
        NSString *plist = [NSHomeDirectory() stringByAppendingPathComponent:
                           @"Library/LaunchAgents/com.kiko.lite.plist"];
        NSString *err = nil;
        const char *word = action == KT_ACT_STOP ? "stop" : (action == KT_ACT_START ? "start" : "restart");
        int rc;

        if (action == KT_ACT_STOP) {
            rc = run_launchctl(@[@"bootout", target], &err);
        } else {
            /* load the agent first (START and RESTART); "already loaded" is an expected error: ignore it */
            run_launchctl(@[@"bootstrap", domain, plist], &err);
            if (action == KT_ACT_START)   /* start without killing a running instance */
                rc = run_launchctl(@[@"kickstart", target], &err);
            else                          /* restart: -k kills the running instance first */
                rc = run_launchctl(@[@"kickstart", @"-k", target], &err);
        }
        if (rc == 0) {
            snprintf(msg, msgsz, "launchctl: %s done", word);
            return 0;
        }
        snprintf(msg, msgsz, "launchctl failed: %s",
                 err.length ? err.UTF8String : "is ~/Library/LaunchAgents/com.kiko.lite.plist installed?");
        return -1;
    }
}
