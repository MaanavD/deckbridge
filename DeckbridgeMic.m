#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <Foundation/Foundation.h>

static NSString *result_path = nil;

static int complete(NSString *message, int code, BOOL is_error) {
    NSString *text = message ?: @"";
    if (result_path) {
        NSString *payload = [NSString stringWithFormat:@"%d\n%@%@", code, text,
                             text.length ? @"\n" : @""];
        NSError *error = nil;
        if (![payload writeToFile:result_path
                       atomically:YES
                         encoding:NSUTF8StringEncoding
                            error:&error]) {
            fprintf(stderr, "could not write helper result: %s\n",
                    error.localizedDescription.UTF8String);
            return 1;
        }
        // The result file carries the logical status because LaunchServices
        // cannot reliably propagate a short-lived app's process exit status.
        return 0;
    }
    if (text.length) {
        FILE *stream = is_error ? stderr : stdout;
        fprintf(stream, "%s\n", text.UTF8String);
    }
    return code;
}

static void fail(NSString *message, int code) {
    exit(complete(message, code, YES));
}

static CGKeyCode parse_key_code(NSString *value) {
    NSScanner *scanner = [NSScanner scannerWithString:value];
    unsigned int code = 0;
    if (![scanner scanHexInt:&code] || !scanner.isAtEnd || code > UINT16_MAX) {
        // scanHexInt accepts ordinary decimal digits but interprets them as
        // hex, so use integerValue unless the caller explicitly says 0x.
        NSInteger decimal = value.integerValue;
        if (decimal < 0 || decimal > UINT16_MAX ||
            ![[NSString stringWithFormat:@"%ld", (long)decimal] isEqualToString:value]) {
            fail([NSString stringWithFormat:@"invalid key code: %@", value], 2);
        }
        code = (unsigned int)decimal;
    } else if (![value hasPrefix:@"0x"] && ![value hasPrefix:@"0X"]) {
        code = (unsigned int)value.integerValue;
    }
    return (CGKeyCode)code;
}

static CGEventFlags parse_flags(NSString *value) {
    if (value.length == 0 || [value isEqualToString:@"none"]) return 0;
    CGEventFlags flags = 0;
    for (NSString *part in [value componentsSeparatedByString:@","]) {
        if ([part isEqualToString:@"control"]) flags |= kCGEventFlagMaskControl;
        else if ([part isEqualToString:@"shift"]) flags |= kCGEventFlagMaskShift;
        else if ([part isEqualToString:@"option"]) flags |= kCGEventFlagMaskAlternate;
        else if ([part isEqualToString:@"command"]) flags |= kCGEventFlagMaskCommand;
        else if ([part isEqualToString:@"function"]) flags |= kCGEventFlagMaskSecondaryFn;
        else fail([NSString stringWithFormat:@"invalid key flag: %@", part], 2);
    }
    return flags;
}

static CGEventFlags modifier_flag_for_key(CGKeyCode code) {
    switch (code) {
        case 54: case 55: return kCGEventFlagMaskCommand;
        case 56: case 60: return kCGEventFlagMaskShift;
        case 58: case 61: return kCGEventFlagMaskAlternate;
        case 59: case 62: return kCGEventFlagMaskControl;
        case 63: return kCGEventFlagMaskSecondaryFn;
        default: return 0;
    }
}

static CGEventType event_type_for_key(CGKeyCode code, bool down) {
    if (modifier_flag_for_key(code)) return kCGEventFlagsChanged;
    return down ? kCGEventKeyDown : kCGEventKeyUp;
}

static CGEventFlags event_flags_for_key(CGKeyCode code, bool down,
                                        CGEventFlags flags) {
    CGEventFlags own_flag = modifier_flag_for_key(code);
    return down ? flags : (flags & ~own_flag);
}

static void post_key(CGKeyCode code, bool down, CGEventFlags flags) {
    CGEventSourceRef source = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
    if (!source) fail(@"could not construct keyboard event source", 1);
    CGEventRef event = CGEventCreateKeyboardEvent(source, code, down);
    CFRelease(source);
    if (!event) fail(@"could not construct keyboard event", 1);
    CGEventSetType(event, event_type_for_key(code, down));
    CGEventSetFlags(event, event_flags_for_key(code, down, flags));
    CGEventPost(kCGHIDEventTap, event);
    CFRelease(event);
}

static BOOL press_dictation_command(AXUIElementRef element,
                                    BOOL start,
                                    NSUInteger depth,
                                    NSUInteger *visited) {
    if (!element || depth > 20 || *visited >= 2000) return NO;
    (*visited)++;

    CFTypeRef identifier_value = NULL;
    NSString *identifier = nil;
    if (AXUIElementCopyAttributeValue(element, kAXIdentifierAttribute,
                                      &identifier_value) == kAXErrorSuccess &&
            identifier_value &&
            CFGetTypeID(identifier_value) == CFStringGetTypeID()) {
        identifier = [(__bridge NSString *)identifier_value copy];
    }
    if (identifier_value) CFRelease(identifier_value);

    CFTypeRef title_value = NULL;
    NSString *title = nil;
    if (AXUIElementCopyAttributeValue(element, kAXTitleAttribute,
                                      &title_value) == kAXErrorSuccess &&
            title_value && CFGetTypeID(title_value) == CFStringGetTypeID()) {
        title = [(__bridge NSString *)title_value copy];
    }
    if (title_value) CFRelease(title_value);

    NSString *expected_identifier = start ? @"startDictation:" : @"stopDictation:";
    NSString *expected_title = start ? @"Start Dictation" : @"Stop Dictation";
    NSString *expected_title_ellipsis = start ? @"Start Dictation…" : @"Stop Dictation…";
    BOOL matches = [identifier isEqualToString:expected_identifier] ||
        [title isEqualToString:expected_title] ||
        [title isEqualToString:expected_title_ellipsis];
    if (matches && AXUIElementPerformAction(element, kAXPressAction)
            == kAXErrorSuccess) {
        return YES;
    }

    CFTypeRef children_value = NULL;
    if (AXUIElementCopyAttributeValue(element, kAXChildrenAttribute,
                                      &children_value) != kAXErrorSuccess ||
            !children_value) {
        return NO;
    }
    BOOL pressed = NO;
    if (CFGetTypeID(children_value) == CFArrayGetTypeID()) {
        CFArrayRef children = (CFArrayRef)children_value;
        for (CFIndex index = 0; index < CFArrayGetCount(children); index++) {
            CFTypeRef child = CFArrayGetValueAtIndex(children, index);
            if (child && CFGetTypeID(child) == AXUIElementGetTypeID() &&
                    press_dictation_command((AXUIElementRef)child, start,
                                             depth + 1, visited)) {
                pressed = YES;
                break;
            }
        }
    }
    CFRelease(children_value);
    return pressed;
}

static BOOL run_frontmost_dictation_command(BOOL start) {
    NSRunningApplication *front = NSWorkspace.sharedWorkspace.frontmostApplication;
    if (!front) return NO;
    AXUIElementRef app = AXUIElementCreateApplication(front.processIdentifier);
    if (!app) return NO;
    CFTypeRef menu_bar_value = NULL;
    BOOL pressed = NO;
    if (AXUIElementCopyAttributeValue(app, kAXMenuBarAttribute,
                                      &menu_bar_value) == kAXErrorSuccess &&
            menu_bar_value &&
            CFGetTypeID(menu_bar_value) == AXUIElementGetTypeID()) {
        NSUInteger visited = 0;
        pressed = press_dictation_command((AXUIElementRef)menu_bar_value,
                                           start, 0, &visited);
    }
    if (menu_bar_value) CFRelease(menu_bar_value);
    CFRelease(app);
    return pressed;
}

static NSString *string_from_ax_value(CFTypeRef value) {
    if (!value) return nil;
    if (CFGetTypeID(value) == CFStringGetTypeID()) {
        return [(__bridge NSString *)value copy];
    }
    if (CFGetTypeID(value) == CFURLGetTypeID()) {
        return [(__bridge NSURL *)value absoluteString];
    }
    return nil;
}

static NSString *web_url_in_element(AXUIElementRef element,
                                    NSUInteger depth,
                                    NSUInteger *visited) {
    if (!element || depth > 40 || *visited >= 20000) return nil;
    (*visited)++;

    // Electron exposes the active renderer route on its AX web area. URL is
    // the authoritative value on current Claude/Codex builds; Document is a
    // compatibility fallback used by some Chromium accessibility versions.
    const CFStringRef url_attributes[] = {kAXURLAttribute, kAXDocumentAttribute};
    for (NSUInteger index = 0; index < 2; index++) {
        CFTypeRef value = NULL;
        if (AXUIElementCopyAttributeValue(element, url_attributes[index], &value)
                == kAXErrorSuccess && value) {
            NSString *url = string_from_ax_value(value);
            CFRelease(value);
            if (url.length > 0) return url;
        }
    }

    const CFStringRef child_attributes[] = {kAXChildrenAttribute,
                                             kAXContentsAttribute};
    for (NSUInteger attribute_index = 0; attribute_index < 2; attribute_index++) {
        CFTypeRef value = NULL;
        if (AXUIElementCopyAttributeValue(element,
                                          child_attributes[attribute_index],
                                          &value) != kAXErrorSuccess || !value) {
            continue;
        }
        if (CFGetTypeID(value) == CFArrayGetTypeID()) {
            CFArrayRef children = (CFArrayRef)value;
            CFIndex count = CFArrayGetCount(children);
            for (CFIndex index = 0; index < count; index++) {
                CFTypeRef child = CFArrayGetValueAtIndex(children, index);
                if (child && CFGetTypeID(child) == AXUIElementGetTypeID()) {
                    NSString *url = web_url_in_element((AXUIElementRef)child,
                                                       depth + 1, visited);
                    if (url.length > 0) {
                        CFRelease(value);
                        return url;
                    }
                }
            }
        }
        CFRelease(value);
    }
    return nil;
}

static NSString *selected_web_url(NSString *bundle_id) {
    NSArray<NSRunningApplication *> *apps =
        [NSRunningApplication runningApplicationsWithBundleIdentifier:bundle_id];
    if (apps.count != 1) {
        fail([NSString stringWithFormat:@"expected one running app for %@; found %lu",
              bundle_id, (unsigned long)apps.count], 5);
    }
    AXUIElementRef app = AXUIElementCreateApplication(apps[0].processIdentifier);
    if (!app) fail(@"could not construct application accessibility element", 5);

    NSUInteger visited = 0;
    NSString *url = nil;
    CFTypeRef focused_window = NULL;
    if (AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute,
                                      &focused_window) == kAXErrorSuccess &&
            focused_window &&
            CFGetTypeID(focused_window) == AXUIElementGetTypeID()) {
        url = web_url_in_element((AXUIElementRef)focused_window, 0, &visited);
    }
    if (focused_window) CFRelease(focused_window);
    if (!url.length) url = web_url_in_element(app, 0, &visited);
    CFRelease(app);

    if (!url.length) {
        fail([NSString stringWithFormat:@"no selected web URL exposed by %@",
              bundle_id], 5);
    }
    return url;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSMutableArray<NSString *> *arguments = [NSMutableArray array];
        for (int index = 1; index < argc; index++) {
            [arguments addObject:[NSString stringWithUTF8String:argv[index]]];
        }
        if (arguments.count >= 2 && [arguments[0] isEqualToString:@"--result"]) {
            result_path = arguments[1];
            [arguments removeObjectsInRange:NSMakeRange(0, 2)];
        }
        NSString *command = arguments.count ? arguments[0] : @"request-access";
        if ([command isEqualToString:@"version"]) {
            return complete(@"6", 0, NO);
        }
        if ([command isEqualToString:@"event-shape"]) {
            if (arguments.count != 4) {
                fail(@"usage: deckbridge-mic event-shape <key-code> <down|up> <flags|none>", 2);
            }
            CGKeyCode code = parse_key_code(arguments[1]);
            BOOL down;
            if ([arguments[2] isEqualToString:@"down"]) down = YES;
            else if ([arguments[2] isEqualToString:@"up"]) down = NO;
            else fail(@"event direction must be down or up", 2);
            CGEventFlags flags = event_flags_for_key(
                code, down, parse_flags(arguments[3]));
            NSString *kind = event_type_for_key(code, down) == kCGEventFlagsChanged
                ? @"flags-changed" : (down ? @"key-down" : @"key-up");
            return complete(
                [NSString stringWithFormat:@"%@|%llu", kind,
                 (unsigned long long)flags], 0, NO);
        }
        if ([command isEqualToString:@"frontmost"]) {
            NSRunningApplication *app = NSWorkspace.sharedWorkspace.frontmostApplication;
            if (!app) fail(@"frontmost app unavailable", 1);
            NSString *name = [app.localizedName ?: @"Unknown" stringByReplacingOccurrencesOfString:@"|" withString:@" "];
            NSString *bundle = [app.bundleIdentifier ?: @"unknown" stringByReplacingOccurrencesOfString:@"|" withString:@" "];
            NSString *value = [NSString stringWithFormat:@"%@|%@|%d", name,
                               bundle, app.processIdentifier];
            return complete(value, 0, NO);
        }
        if ([command isEqualToString:@"check"]) {
            if (!AXIsProcessTrusted()) {
                fail(@"Deckbridge Mic is not trusted. Open System Settings > Privacy & Security > Accessibility and enable Deckbridge Mic.", 4);
            }
            return complete(@"ready=yes", 0, NO);
        }
        if ([command isEqualToString:@"start-dictation"] ||
                [command isEqualToString:@"stop-dictation"]) {
            if (!AXIsProcessTrusted()) {
                fail(@"Deckbridge Mic is not trusted. Open System Settings > Privacy & Security > Accessibility and enable Deckbridge Mic.", 4);
            }
            BOOL start = [command isEqualToString:@"start-dictation"];
            if (!run_frontmost_dictation_command(start)) {
                NSString *verb = start ? @"Start" : @"Stop";
                fail([NSString stringWithFormat:
                      @"The focused app exposes no enabled Edit > %@ Dictation action.",
                      verb], 5);
            }
            return complete(@"", 0, NO);
        }
        if ([command isEqualToString:@"request-access"]) {
            NSDictionary *options = @{
                (__bridge NSString *)kAXTrustedCheckOptionPrompt: @YES
            };
            BOOL trusted = AXIsProcessTrustedWithOptions(
                (__bridge CFDictionaryRef)options
            );
            if (!trusted) {
                // Apple documents prompting as asynchronous. Keep a direct
                // Finder/`open` launch alive briefly so the consent sheet has
                // time to attach to the named app identity.
                if (!result_path) usleep(500000);
                fail(@"Enable Deckbridge Mic in System Settings > Privacy & Security > Accessibility.", 4);
            }
            return complete(@"ready=yes", 0, NO);
        }
        if ([command isEqualToString:@"web-url"]) {
            if (arguments.count != 2) {
                fail(@"usage: deckbridge-mic web-url <bundle-id>", 2);
            }
            if (!AXIsProcessTrusted()) {
                fail(@"Deckbridge Mic is not trusted. Open System Settings > Privacy & Security > Accessibility and enable Deckbridge Mic.", 4);
            }
            return complete(selected_web_url(arguments[1]), 0, NO);
        }
        if (![command isEqualToString:@"tap"] &&
            ![command isEqualToString:@"key-down"] &&
            ![command isEqualToString:@"key-up"]) {
            fail([NSString stringWithFormat:@"unknown command: %@", command], 2);
        }
        if (arguments.count != 3) {
            fail([NSString stringWithFormat:@"usage: deckbridge-mic %@ <key-code> <flags|none>", command], 2);
        }
        if (!AXIsProcessTrusted()) {
            fail(@"Deckbridge Mic is not trusted. Open System Settings > Privacy & Security > Accessibility and enable Deckbridge Mic.", 4);
        }
        CGKeyCode code = parse_key_code(arguments[1]);
        CGEventFlags flags = parse_flags(arguments[2]);
        if ([command isEqualToString:@"tap"]) {
            post_key(code, true, flags);
            usleep(20000);
            post_key(code, false, flags);
        } else {
            post_key(code, [command isEqualToString:@"key-down"], flags);
        }
        return complete(@"", 0, NO);
    }
}
