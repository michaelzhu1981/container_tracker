#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <ScriptingBridge/ScriptingBridge.h>

@protocol ChromeGenericMethods
- (void)close;
- (id)executeJavascript:(NSString *)javascript;
@end

@interface ChromeApplication : SBApplication
- (SBElementArray *)windows;
@end

@interface ChromeWindow : SBObject <ChromeGenericMethods>
- (SBElementArray *)tabs;
- (NSString *)id;
@property NSInteger index;
@property NSRect bounds;
@property NSInteger activeTabIndex;
@property(copy, readonly) id activeTab;
@end

@interface ChromeTab : SBObject <ChromeGenericMethods>
- (NSString *)id;
@property(copy) NSString *URL;
@end

@interface BridgeDelegate : NSObject <SBApplicationDelegate>
@property(strong) NSError *lastError;
@end

@implementation BridgeDelegate
- (id)eventDidFail:(const AppleEvent *)event withError:(NSError *)error {
    self.lastError = error;
    return nil;
}
@end

static NSString *clean(NSString *value) {
    NSString *text = value ?: @"";
    text = [text stringByReplacingOccurrencesOfString:@"\t" withString:@"\\t"];
    text = [text stringByReplacingOccurrencesOfString:@"\r" withString:@"\\r"];
    return [text stringByReplacingOccurrencesOfString:@"\n" withString:@"\\n"];
}

static void ok(NSString *body) {
    printf("OK\n%s\n", [(body ?: @"") UTF8String]);
}

static void fail(NSString *code, NSString *stage, NSString *message, NSInteger number) {
    NSString *body = [NSString stringWithFormat:@"ERROR\t%@\t%@: %@ (%ld)\n",
                      code, stage, clean(message), (long)number];
    fputs([body UTF8String], stdout);
}

static BOOL reportDelegateError(BridgeDelegate *delegate, NSString *stage) {
    NSError *error = delegate.lastError;
    if (!error) return NO;
    NSString *code = @"NAVIGATION";
    if (error.code == -1743 || error.code == -10004) code = @"BROWSER_PERMISSION";
    else if ([stage isEqualToString:@"window"]) code = @"BROWSER_CLOSED";
    else if ([stage isEqualToString:@"tab"]) code = @"TAB_NOT_FOUND";
    fail(code, stage, error.localizedDescription, error.code);
    return YES;
}

static ChromeWindow *findWindow(ChromeApplication *app, long long target) {
    for (ChromeWindow *window in [app windows]) {
        if ([[window id] longLongValue] == target) return window;
    }
    return nil;
}

static ChromeTab *findTab(ChromeWindow *window, long long target, NSInteger *index) {
    NSInteger current = 0;
    for (ChromeTab *tab in [window tabs]) {
        current += 1;
        if ([[tab id] longLongValue] == target) {
            if (index) *index = current;
            return tab;
        }
    }
    return nil;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 6) {
            fail(@"NAVIGATION", @"arguments", @"Missing Chrome bridge arguments.", -1);
            return 0;
        }
        NSString *action = [NSString stringWithUTF8String:argv[1]];
        pid_t pid = (pid_t)strtol(argv[2], NULL, 10);
        long long windowId = strtoll(argv[3], NULL, 10);
        long long tabId = strtoll(argv[4], NULL, 10);
        NSString *payload = [NSString stringWithUTF8String:argv[5]];
        NSString *stage = action;
        @try {
            ChromeApplication *app = (ChromeApplication *)[SBApplication applicationWithProcessIdentifier:pid];
            BridgeDelegate *delegate = [BridgeDelegate new];
            app.delegate = delegate;
            app.timeout = 15 * 60;
            if (!app || !app.running) {
                fail(@"BROWSER_CLOSED", stage, @"The selected Chrome process is not running.", -600);
                return 0;
            }

            if ([action isEqualToString:@"inventory"]) {
                NSMutableArray<NSString *> *lines = [NSMutableArray array];
                for (ChromeWindow *window in [app windows]) {
                    for (ChromeTab *tab in [window tabs]) {
                        [lines addObject:[NSString stringWithFormat:@"%@\t%@\t%@",
                                          [window id], [tab id], clean(tab.URL)]];
                    }
                }
                if (reportDelegateError(delegate, stage)) return 0;
                ok([lines componentsJoinedByString:@"\n"]);
                return 0;
            }

            if ([action isEqualToString:@"new_window"]) {
                Class windowClass = [app classForScriptingClass:@"window"];
                ChromeWindow *window = [[windowClass alloc] initWithProperties:@{}];
                [[app windows] addObject:window];
                window = [window get];
                ChromeTab *tab = window.activeTab;
                tab.URL = payload;
                if (reportDelegateError(delegate, stage)) return 0;
                ok([NSString stringWithFormat:@"%@\t%@\t%@", [window id], [tab id], clean(tab.URL)]);
                return 0;
            }

            stage = @"window";
            ChromeWindow *window = findWindow(app, windowId);
            if (!window) {
                fail(@"BROWSER_CLOSED", stage, @"The tracking Chrome window was closed.", -1728);
                return 0;
            }
            if ([action isEqualToString:@"tabs"]) {
                NSMutableArray<NSString *> *lines = [NSMutableArray array];
                for (ChromeTab *tab in [window tabs]) {
                    [lines addObject:[NSString stringWithFormat:@"%@\t%@", [tab id], clean(tab.URL)]];
                }
                if (reportDelegateError(delegate, stage)) return 0;
                ok([lines componentsJoinedByString:@"\n"]);
                return 0;
            }
            if ([action isEqualToString:@"close_window"]) {
                [window close];
                if (reportDelegateError(delegate, stage)) return 0;
                ok(@"true");
                return 0;
            }
            if ([action isEqualToString:@"new_tab"]) {
                Class tabClass = [app classForScriptingClass:@"tab"];
                ChromeTab *tab = [[tabClass alloc] initWithProperties:@{@"URL": payload}];
                [[window tabs] addObject:tab];
                tab = [tab get];
                if (reportDelegateError(delegate, @"new_tab")) return 0;
                ok([NSString stringWithFormat:@"%@\t%@", [tab id], clean(tab.URL)]);
                return 0;
            }

            stage = @"tab";
            NSInteger tabIndex = 0;
            ChromeTab *tab = findTab(window, tabId, &tabIndex);
            if (!tab) {
                fail(@"TAB_NOT_FOUND", stage, @"The tracking tab was closed or is no longer available.", -1728);
                return 0;
            }
            if ([action isEqualToString:@"evaluate"]) {
                stage = @"evaluate";
                id value = [tab executeJavascript:payload];
                if (reportDelegateError(delegate, stage)) return 0;
                ok(value ? [value description] : @"");
                return 0;
            }
            if ([action isEqualToString:@"tab"]) {
                ok([NSString stringWithFormat:@"%@\t%@", [tab id], clean(tab.URL)]);
                return 0;
            }
            if ([action isEqualToString:@"navigate"]) {
                tab.URL = payload;
                if (reportDelegateError(delegate, stage)) return 0;
                ok([NSString stringWithFormat:@"%@\t%@", [tab id], clean(tab.URL)]);
                return 0;
            }
            if ([action isEqualToString:@"close_tab"]) {
                [tab close];
                if (reportDelegateError(delegate, stage)) return 0;
                ok(@"true");
                return 0;
            }
            if ([action isEqualToString:@"activate"] || [action isEqualToString:@"bounds"]) {
                window.activeTabIndex = tabIndex;
                window.index = 1;
                [app activate];
                if (reportDelegateError(delegate, stage)) return 0;
                if ([action isEqualToString:@"bounds"]) {
                    NSRect rect = window.bounds;
                    ok([NSString stringWithFormat:@"%.0f,%.0f,%.0f,%.0f",
                        rect.origin.x, rect.origin.y,
                        rect.origin.x + rect.size.width, rect.origin.y + rect.size.height]);
                } else {
                    ok(@"true");
                }
                return 0;
            }
            fail(@"NAVIGATION", action, @"Unknown Chrome bridge action.", -1708);
        } @catch (NSException *exception) {
            NSString *code = [stage isEqualToString:@"window"] ? @"BROWSER_CLOSED" :
                             [stage isEqualToString:@"tab"] ? @"TAB_NOT_FOUND" : @"NAVIGATION";
            fail(code, stage, exception.reason, -1);
        }
    }
    return 0;
}
