/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * mac_view.m - macOS menu-bar layer (Objective-C, ARC).  NSStatusItem with a coloured dot drawn in
 * code, NSMenu, NSWindow dialogs (NSTableView / read-only NSTextView), post_to_ui through GCD.
 * UNVERIFIED: written without a Mac and never compiled.  All kt_view calls except post_to_ui and
 * quit run on the main thread.
 */
#import <Cocoa/Cocoa.h>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include "kt_icon.h"
#include "mac_internal.h"

/* NSString stringWithUTF8String: returns nil for invalid UTF-8 and then crashes callers; fall back to
 * Latin-1, and to an empty string for NULL. */
@interface NSString (KTSafe)
+ (NSString *)ktSafeString:(const char *)s;
@end

@implementation NSString (KTSafe)
+ (NSString *)ktSafeString:(const char *)s
{
    if (!s) return @"";
    NSString *r = [NSString stringWithUTF8String:s];
    if (!r) r = [NSString stringWithCString:s encoding:NSISOLatin1StringEncoding];
    return r ?: @"";
}
@end

static const kt_events *g_events;
static NSMutableDictionary *g_dialogs;   /* kind -> KTDialog, removed when the window closes */
static NSStatusItem *g_item;
static NSMutableDictionary<NSString *, NSWindow *> *g_windows;
static int g_lock_fd = -1;

static NSImage *dot_image(kt_state st)
{
    uint32_t rgb = kt_icon_state_rgb(st);
    NSColor *c = [NSColor colorWithSRGBRed:((rgb >> 16) & 0xFF) / 255.0 green:((rgb >> 8) & 0xFF) / 255.0
                                      blue:(rgb & 0xFF) / 255.0 alpha:1.0];
    NSImage *img = [NSImage imageWithSize:NSMakeSize(18, 18) flipped:NO drawingHandler:^BOOL(NSRect r) {
        [c setFill];
        [[NSBezierPath bezierPathWithOvalInRect:NSInsetRect(r, 3, 3)] fill];
        return YES;
    }];
    img.template = NO;   /* keep the colour; a template image would be drawn monochrome */
    return img;
}

/* ---------------------------------------------------------------- menu target */

@interface KTMenuTarget : NSObject
- (void)itemClicked:(NSMenuItem *)sender;
- (void)dialogButton:(NSButton *)sender;
@end

@implementation KTMenuTarget
- (void)itemClicked:(NSMenuItem *)sender
{
    NSString *ident = sender.representedObject;
    if (ident.length && g_events && g_events->on_menu_item) g_events->on_menu_item(g_events->ctx, ident.UTF8String);
}
- (void)dialogButton:(NSButton *)sender
{
    NSArray *pair = sender.cell.representedObject;   /* @[kind, buttonId] */
    if (pair.count == 2 && g_events && g_events->on_dialog_button)
        g_events->on_dialog_button(g_events->ctx, [pair[0] UTF8String], [pair[1] UTF8String]);
}
@end

static KTMenuTarget *g_target;

/* ---------------------------------------------------------------- kt_view: tray */

static void v_bind(void *ctx, const kt_events *e) { (void)ctx; g_events = e; }

static int v_claim_single_instance(void *ctx)
{
    (void)ctx;
    NSString *path = [NSTemporaryDirectory() stringByAppendingPathComponent:@"kiko-tray.lock"];
    g_lock_fd = open(path.fileSystemRepresentation, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (g_lock_fd < 0) return 1;
    if (flock(g_lock_fd, LOCK_EX | LOCK_NB) != 0) { close(g_lock_fd); g_lock_fd = -1; return 0; }
    return 1;
}

static void v_set_state(void *ctx, kt_state st, const char *tooltip)
{
    (void)ctx;
    if (!g_item) g_item = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    g_item.button.image = dot_image(st);
    g_item.button.toolTip = [NSString ktSafeString:tooltip ? tooltip : "KIKO"];
}

static void v_set_menu(void *ctx, const kt_menu_item *items, int n)
{
    (void)ctx;
    NSMenu *menu = [[NSMenu alloc] initWithTitle:@"kiko"];
    menu.autoenablesItems = NO;
    for (int i = 0; i < n; i++) {
        if (items[i].separator) { [menu addItem:[NSMenuItem separatorItem]]; continue; }
        NSMenuItem *mi = [[NSMenuItem alloc] initWithTitle:[NSString ktSafeString:items[i].label ?: ""]
                                                    action:@selector(itemClicked:) keyEquivalent:@""];
        mi.target = g_target;
        mi.enabled = items[i].enabled ? YES : NO;
        mi.representedObject = [NSString ktSafeString:items[i].id ?: ""];
        [menu addItem:mi];
    }
    if (!g_item) g_item = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    g_item.menu = menu;
}

static void v_notify(void *ctx, const char *text)
{
    (void)ctx;
    /* escape backslash and double quote for the AppleScript string literal */
    NSString *s = [NSString ktSafeString:text ?: ""];
    s = [s stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
    s = [s stringByReplacingOccurrencesOfString:@"\"" withString:@"\\\""];
    NSString *src = [NSString stringWithFormat:@"display notification \"%@\" with title \"kiko\"", s];
    [[[NSAppleScript alloc] initWithSource:src] executeAndReturnError:nil];
}

static void v_open_key_store(void *ctx)
{
    (void)ctx;
    NSTask *t = [[NSTask alloc] init];   /* open -a "Keychain Access" */
    t.executableURL = [NSURL fileURLWithPath:@"/usr/bin/open"];
    t.arguments = @[@"-a", @"Keychain Access"];
    [t launchAndReturnError:nil];
}

static void v_post_to_ui(void *ctx, kt_ui_fn fn, void *arg)
{
    (void)ctx;
    dispatch_async(dispatch_get_main_queue(), ^{ fn(arg); });   /* thread safe */
}

static void v_run(void *ctx) { (void)ctx; [NSApp run]; }

static void v_quit(void *ctx)
{
    (void)ctx;
    dispatch_async(dispatch_get_main_queue(), ^{
        [NSApp stop:nil];
        /* stop: only takes effect after an event has been processed; send a dummy one */
        NSEvent *e = [NSEvent otherEventWithType:NSEventTypeApplicationDefined location:NSZeroPoint modifierFlags:0
                                       timestamp:0 windowNumber:0 context:nil subtype:0 data1:0 data2:0];
        [NSApp postEvent:e atStart:YES];
    });
}

/* ---------------------------------------------------------------- dialogs */

@interface KTDialog : NSObject <NSWindowDelegate, NSTableViewDataSource>
@property (strong) NSWindow *window;
@property (copy) NSString *kind;
@property (strong) NSArray<NSArray<NSString *> *> *rows;
@property (weak) NSTableView *table;   /* nil for text dialogs */
@end

@implementation KTDialog
- (NSInteger)numberOfRowsInTableView:(NSTableView *)tv { return (NSInteger)self.rows.count; }
- (id)tableView:(NSTableView *)tv objectValueForTableColumn:(NSTableColumn *)col row:(NSInteger)row
{
    NSUInteger c = (NSUInteger)[col.identifier integerValue];
    NSArray<NSString *> *r = self.rows[(NSUInteger)row];
    return c < r.count ? r[c] : @"";
}
- (void)windowWillClose:(NSNotification *)n
{
    NSString *kind = self.kind;
    KTDialog *keep = self;   /* stay alive until this method returns */
    self.window.delegate = nil;        /* no callbacks into a dialog that is going away */
    self.table.dataSource = nil;
    [g_windows removeObjectForKey:kind];
    [g_dialogs removeObjectForKey:kind];
    (void)keep;
    if (g_events && g_events->on_dialog_closed) g_events->on_dialog_closed(g_events->ctx, kind.UTF8String);
}
@end



static NSTextField *label(NSString *s)
{
    NSTextField *l = [NSTextField wrappingLabelWithString:s];
    l.selectable = YES;
    return l;
}

static NSView *content_for(KTDialog *d, const kt_dialog *m, NSSize size)
{
    NSView *root = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, size.width, size.height)];
    CGFloat margin = 12, y = size.height - margin;
    NSString *summary = [NSString ktSafeString:m->summary];
    NSString *footer = [NSString ktSafeString:m->footer];
    CGFloat w = size.width - 2 * margin;

    if (summary.length) {
        NSTextField *l = label(summary);
        l.frame = NSMakeRect(margin, y - 36, w, 36);
        l.autoresizingMask = NSViewWidthSizable | NSViewMinYMargin;
        [root addSubview:l];
        y -= 44;
    }
    CGFloat bottom = margin + 30 + 8 + (footer.length ? 36 : 0);
    NSScrollView *sv = [[NSScrollView alloc] initWithFrame:NSMakeRect(margin, bottom, w, y - bottom)];
    sv.hasVerticalScroller = YES;
    sv.borderType = NSBezelBorder;
    sv.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    if (m->ncols > 0) {
        NSTableView *tv = [[NSTableView alloc] initWithFrame:sv.bounds];
        NSMutableArray *rows = [NSMutableArray array];
        for (int r = 0; r < m->nrows; r++) {
            NSMutableArray *row = [NSMutableArray array];
            for (int c = 0; c < m->ncols; c++) [row addObject:[NSString ktSafeString:m->cells[r * m->ncols + c]]];
            [rows addObject:row];
        }
        d.rows = rows;
        for (int c = 0; c < m->ncols; c++) {
            NSTableColumn *col = [[NSTableColumn alloc] initWithIdentifier:[NSString stringWithFormat:@"%d", c]];
            col.title = [NSString ktSafeString:m->columns[c]];
            col.width = w / m->ncols;
            [tv addTableColumn:col];
        }
        tv.dataSource = d;
        d.table = tv;
        sv.documentView = tv;
    } else {
        /* scrollableTextView returns a scroll view with a correctly configured text view inside */
        sv = [NSTextView scrollableTextView];
        sv.frame = NSMakeRect(margin, bottom, w, y - bottom);
        sv.borderType = NSBezelBorder;
        sv.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
        NSTextView *tx = (NSTextView *)sv.documentView;
        tx.editable = NO;
        tx.font = [NSFont monospacedSystemFontOfSize:12 weight:NSFontWeightRegular];
        tx.string = [NSString ktSafeString:m->text];
        [tx scrollToEndOfDocument:nil];   /* newest line (logs) */
    }
    [root addSubview:sv];
    if (footer.length) {
        NSTextField *l = label(footer);
        l.frame = NSMakeRect(margin, margin + 30 + 8, w, 30);
        l.autoresizingMask = NSViewWidthSizable | NSViewMaxYMargin;
        [root addSubview:l];
    }
    CGFloat x = margin;
    for (int i = 0; i < m->nbuttons; i++) {
        NSButton *b = [NSButton buttonWithTitle:[NSString ktSafeString:m->buttons[i].label]
                                         target:g_target action:@selector(dialogButton:)];
        b.cell.representedObject = @[d.kind, [NSString ktSafeString:m->buttons[i].id]];
        b.frame = NSMakeRect(x, margin, 170, 30);
        b.autoresizingMask = NSViewMaxXMargin | NSViewMaxYMargin;
        [root addSubview:b];
        x += 178;
    }
    NSButton *close = [NSButton buttonWithTitle:@"Close" target:d.window action:@selector(performClose:)];
    close.frame = NSMakeRect(size.width - margin - 100, margin, 100, 30);
    close.autoresizingMask = NSViewMinXMargin | NSViewMaxYMargin;
    close.keyEquivalent = @"\r";
    [root addSubview:close];
    return root;
}

static void v_show_dialog(void *ctx, const kt_dialog *m)
{
    (void)ctx;
    NSString *kind = [NSString ktSafeString:m->kind];
    if (!g_dialogs) g_dialogs = [NSMutableDictionary dictionary];
    if (!g_windows) g_windows = [NSMutableDictionary dictionary];
    KTDialog *d = g_dialogs[kind];
    if (!d) {   /* closed dialogs are removed in windowWillClose, so a minimised one is reused */
        d = [[KTDialog alloc] init];
        d.kind = kind;
        d.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 680, 460)
                                               styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                                                          | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable
                                                 backing:NSBackingStoreBuffered defer:NO];
        d.window.releasedWhenClosed = NO;
        d.window.delegate = d;
        [d.window center];
        g_dialogs[kind] = d;
        g_windows[kind] = d.window;
    }
    d.window.title = [NSString ktSafeString:m->title];
    d.window.contentView = content_for(d, m, d.window.contentView.frame.size);   /* update = rebuild */
    if (d.window.isMiniaturized) [d.window deminiaturize:nil];
    [d.window makeKeyAndOrderFront:nil];
#if defined(__MAC_14_0) && __MAC_OS_X_VERSION_MAX_ALLOWED >= 140000
    if (@available(macOS 14.0, *)) [NSApp activate];
    else [NSApp activateIgnoringOtherApps:YES];
#else
    [NSApp activateIgnoringOtherApps:YES];   /* an agent app has no Dock icon to click */
#endif
}

/* ---------------------------------------------------------------- create / destroy */

int mac_view_create(kt_view *out)
{
    g_target = [[KTMenuTarget alloc] init];
    memset(out, 0, sizeof *out);
    out->key_store_label = "Open Keychain Access";
    out->bind = v_bind;
    out->claim_single_instance = v_claim_single_instance;
    out->set_state = v_set_state;
    out->set_menu = v_set_menu;
    out->show_dialog = v_show_dialog;
    out->notify = v_notify;
    out->open_key_store = v_open_key_store;
    out->post_to_ui = v_post_to_ui;
    out->run = v_run;
    out->quit = v_quit;
    return 0;
}

void mac_view_destroy(void)
{
    g_events = NULL;   // the presenter is already freed: no callbacks while closing
    for (NSString *k in [g_windows allKeys]) [g_windows[k] close];
    if (g_item) { [[NSStatusBar systemStatusBar] removeStatusItem:g_item]; g_item = nil; }
    if (g_lock_fd >= 0) { close(g_lock_fd); g_lock_fd = -1; }
}
