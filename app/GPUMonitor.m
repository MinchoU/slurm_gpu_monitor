// Dock app wrapper around gpu-monitor: owns the python server as a child process
// and renders the dashboard in a native window.
//
// Objective-C rather than Swift because this machine's CommandLineTools ships a
// duplicate SwiftBridging modulemap that makes `import Cocoa` fail to build.
//
// Per-user SSH settings live in ~/.config/gpumonitor/config.json. On first
// launch (no config) the app shows a setup window instead of the dashboard.
//
// Build: see build_app.sh
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

@interface AppDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property(strong) NSWindow *window;
@property(strong) WKWebView *webView;
@property(strong) NSTextField *status;
@property(strong) NSTask *server;
@property(assign) NSInteger port;
// setup window
@property(strong) NSWindow *setupWindow;
@property(strong) NSTextField *hostField;
@property(strong) NSTextField *intervalField;
@property(strong) NSTextField *portField;
@property(strong) NSTextField *testResult;
@property(assign) BOOL initialSetup;
@end

@implementation AppDelegate

- (NSString *)configPath {
    NSString *env = NSProcessInfo.processInfo.environment[@"GPUMON_CONFIG"];
    if (env.length) return env;
    return [NSHomeDirectory() stringByAppendingPathComponent:
        @".config/gpumonitor/config.json"];
}

- (NSDictionary *)readConfig {
    id data = [NSData dataWithContentsOfFile:[self configPath]];
    if (!data) return @{};
    id obj = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    return [obj isKindOfClass:[NSDictionary class]] ? obj : @{};
}

- (void)writeConfig:(NSDictionary *)updates {
    NSMutableDictionary *cfg = [NSMutableDictionary dictionary];
    NSDictionary *old = [self readConfig];
    if ([old isKindOfClass:[NSDictionary class]]) [cfg addEntriesFromDictionary:old];
    [cfg addEntriesFromDictionary:updates];
    NSString *path = [self configPath];
    NSString *dir = [path stringByDeletingLastPathComponent];
    [[NSFileManager defaultManager] createDirectoryAtPath:dir
                              withIntermediateDirectories:YES attributes:nil error:NULL];
    NSData *data = [NSJSONSerialization dataWithJSONObject:cfg options:0 error:NULL];
    [data writeToFile:path atomically:YES];
    [[NSFileManager defaultManager] setAttributes:@{NSFilePosixPermissions: @0600}
                                   ofItemAtPath:path error:NULL];
}

- (NSString *)expandTilde:(NSString *)p {
    if ([p hasPrefix:@"~"])
        return [NSHomeDirectory() stringByAppendingPathComponent:[p substringFromIndex:1]];
    return p;
}

- (NSString *)serverDir {
    id repo = [self readConfig][@"repo"];
    if ([repo isKindOfClass:[NSString class]] && [(NSString *)repo length])
        return [self expandTilde:repo];
    return [NSHomeDirectory() stringByAppendingPathComponent:@"gpu-monitor"];
}

- (NSString *)configuredHost {
    id host = [self readConfig][@"host"];
    return ([host isKindOfClass:[NSString class]] && [(NSString *)host length])
        ? (NSString *)host : @"";
}

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    self.port = 0;
    [self buildMenu];
    [self buildWindow];
    if ([self configuredHost].length == 0) {
        self.initialSetup = YES;
        [self showSetup:nil];
    } else {
        [self startServer];
    }
}

// Quitting the app must not leave an orphaned poller behind.
- (void)applicationWillTerminate:(NSNotification *)note {
    [self.server terminate];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app { return NO; }

// Closing the window parks the app in the Dock; clicking the icon brings it back.
- (BOOL)windowShouldClose:(NSWindow *)sender {
    if (sender == self.setupWindow) { [self.setupWindow orderOut:nil]; return NO; }
    [self.window orderOut:nil];
    return NO;
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)flag {
    [self showWindow];
    return YES;
}

- (void)showWindow {
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)buildWindow {
    self.window = [[NSWindow alloc]
        initWithContentRect:NSMakeRect(0, 0, 1180, 900)
                  styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                             NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = @"SLURM GPU Monitor";
    self.window.minSize = NSMakeSize(720, 480);
    [self.window center];
    [self.window setFrameAutosaveName:@"GPUMonitorWindow"];
    self.window.delegate = self;

    NSView *content = self.window.contentView;

    self.webView = [[WKWebView alloc] initWithFrame:content.bounds
                                      configuration:[[WKWebViewConfiguration alloc] init]];
    self.webView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    [content addSubview:self.webView];

    self.status = [NSTextField labelWithString:@"서버를 시작하는 중…"];
    self.status.alignment = NSTextAlignmentCenter;
    self.status.textColor = NSColor.secondaryLabelColor;
    self.status.font = [NSFont systemFontOfSize:13];
    self.status.selectable = YES;
    self.status.maximumNumberOfLines = 4;
    self.status.translatesAutoresizingMaskIntoConstraints = NO;
    [content addSubview:self.status];
    [NSLayoutConstraint activateConstraints:@[
        [self.status.centerXAnchor constraintEqualToAnchor:content.centerXAnchor],
        [self.status.centerYAnchor constraintEqualToAnchor:content.centerYAnchor],
        [self.status.widthAnchor constraintLessThanOrEqualToAnchor:content.widthAnchor
                                                        multiplier:0.8],
    ]];

    [self showWindow];
}

- (void)startServer {
    self.status.hidden = NO;
    self.status.stringValue = @"서버를 시작하는 중…";
    self.port = 0;

    NSString *dir = [self serverDir];
    NSString *serverPy = [dir stringByAppendingPathComponent:@"server.py"];
    if (![[NSFileManager defaultManager] fileExistsAtPath:serverPy]) {
        self.status.stringValue = [NSString stringWithFormat:
            @"코드 폴더를 찾을 수 없습니다 (%@).\n"
             "터미널에서 `git clone https://github.com/MinchoU/slurm_gpu_monitor ~/gpu-monitor`\n"
             "로 설치하세요 (설정 → repo 키로 다른 경로도 지정 가능).", dir];
        return;
    }

    // server.py picks host/interval/port from the config file itself.
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    task.arguments = @[ @"python3", serverPy ];
    task.currentDirectoryURL = [NSURL fileURLWithPath:dir];

    // A GUI app inherits a bare PATH; python may live in /opt/homebrew/bin and
    // ssh/scp in /usr/bin.
    NSMutableDictionary *env = [NSProcessInfo.processInfo.environment mutableCopy];
    env[@"PATH"] = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
    task.environment = env;

    NSPipe *pipe = [NSPipe pipe];
    task.standardOutput = pipe;
    task.standardError = pipe;

    __block NSMutableString *buffer = [NSMutableString string];
    __weak typeof(self) weakSelf = self;
    pipe.fileHandleForReading.readabilityHandler = ^(NSFileHandle *fh) {
        NSData *data = fh.availableData;
        if (!data.length) return;
        NSString *chunk = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        if (!chunk) return;
        [buffer appendString:chunk];

        typeof(self) me = weakSelf;
        if (!me || me.port != 0) return;

        // server.py announces the port it actually bound (the default may be taken).
        NSRange r = [buffer rangeOfString:@"GPUMON_PORT=([0-9]+)"
                                  options:NSRegularExpressionSearch];
        if (r.location == NSNotFound) return;
        NSInteger found = [[buffer substringWithRange:r] substringFromIndex:12].integerValue;
        dispatch_async(dispatch_get_main_queue(), ^{ [me loadPort:found]; });
    };

    task.terminationHandler = ^(NSTask *proc) {
        dispatch_async(dispatch_get_main_queue(), ^{
            typeof(self) me = weakSelf;
            if (!me || me.port != 0) return;
            me.status.stringValue = [NSString stringWithFormat:
                @"서버가 시작되지 못했습니다 (exit %d).\n"
                 "터미널에서 `python3 %@/server.py` 로 원인을 확인하세요.",
                proc.terminationStatus, dir];
        });
    };

    NSError *err = nil;
    if (![task launchAndReturnError:&err]) {
        self.status.stringValue = [NSString stringWithFormat:@"python3 실행 실패: %@",
                                                             err.localizedDescription];
        return;
    }
    self.server = task;
}

- (NSURL *)dashboardURL {
    return [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%ld/",
                                                           (long)self.port]];
}

- (void)loadPort:(NSInteger)port {
    self.port = port;
    self.status.hidden = YES;
    [self.webView loadRequest:[NSURLRequest requestWithURL:[self dashboardURL]]];
}

- (void)reload:(id)sender {
    if (self.port) [self.webView loadRequest:[NSURLRequest requestWithURL:[self dashboardURL]]];
}

- (void)openInBrowser:(id)sender {
    if (self.port) [NSWorkspace.sharedWorkspace openURL:[self dashboardURL]];
}

#pragma mark - setup window

- (NSTextField *)addRow:(NSStackView *)stack label:(NSString *)label
             placeholder:(NSString *)ph {
    NSTextField *f = [[NSTextField alloc] init];
    f.placeholderString = ph;
    f.translatesAutoresizingMaskIntoConstraints = NO;
    [f.widthAnchor constraintEqualToConstant:220].active = YES;
    [stack addArrangedSubview:[NSTextField labelWithString:label]];
    [stack addArrangedSubview:f];
    return f;
}

- (void)showSetup:(id)sender {
    if (self.setupWindow) {
        [self.setupWindow makeKeyAndOrderFront:nil];
        [NSApp activateIgnoringOtherApps:YES];
        return;
    }

    NSDictionary *cfg = [self readConfig];
    self.initialSetup = ([self configuredHost].length == 0);

    self.setupWindow = [[NSWindow alloc]
        initWithContentRect:NSMakeRect(0, 0, 560, 400)
                  styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
                    backing:NSBackingStoreBuffered defer:NO];
    self.setupWindow.title = self.initialSetup ? @"SLURM GPU Monitor — 초기 설정"
                                               : @"SLURM GPU Monitor — 설정";
    self.setupWindow.delegate = self;
    NSView *content = self.setupWindow.contentView;

    NSTextField *intro = [NSTextField labelWithString:
        @"Slurm 로그인 노드에 비밀번호 없이 ssh할 수 있어야 합니다\n"
         "(확인: ssh -o BatchMode=yes <host> echo ok)\n"
         "~/.ssh/config 에 alias가 이미 있으면 그 이름을 쓰세요."];
    intro.font = [NSFont systemFontOfSize:12];
    intro.textColor = NSColor.secondaryLabelColor;
    intro.translatesAutoresizingMaskIntoConstraints = NO;

    NSStackView *rows = [[NSStackView alloc] init];
    rows.orientation = NSUserInterfaceLayoutOrientationVertical;
    rows.alignment = NSLayoutAttributeLeading;
    rows.spacing = 10;
    rows.translatesAutoresizingMaskIntoConstraints = NO;
    self.hostField = [self addRow:rows label:@"SSH host" placeholder:@"예: ai"];
    self.intervalField = [self addRow:rows label:@"폴링 간격(초)"
          placeholder:[NSString stringWithFormat:@"%@", cfg[@"interval"] ?: @15]];
    self.portField = [self addRow:rows label:@"포트"
          placeholder:[NSString stringWithFormat:@"%@", cfg[@"port"] ?: @8777]];
    self.hostField.stringValue = [self configuredHost];

    self.testResult = [NSTextField labelWithString:@""];
    self.testResult.font = [NSFont fontWithName:@"Menlo" size:11] ?: [NSFont systemFontOfSize:11];
    self.testResult.textColor = NSColor.secondaryLabelColor;
    self.testResult.maximumNumberOfLines = 6;
    self.testResult.selectable = YES;
    self.testResult.translatesAutoresizingMaskIntoConstraints = NO;

    NSButton *test = [NSButton buttonWithTitle:@"연결 테스트" target:self
                                       action:@selector(runTest:)];
    NSButton *save = [NSButton buttonWithTitle:@"저장하고 시작" target:self
                                       action:@selector(saveSetup:)];
    save.keyEquivalent = @"\r";
    NSStackView *buttons = [NSStackView stackViewWithViews:@[save, test]];
    buttons.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    buttons.spacing = 10;
    buttons.translatesAutoresizingMaskIntoConstraints = NO;

    [content addSubview:intro];
    [content addSubview:rows];
    [content addSubview:self.testResult];
    [content addSubview:buttons];
    [NSLayoutConstraint activateConstraints:@[
        [intro.topAnchor constraintEqualToAnchor:content.topAnchor constant:20],
        [intro.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:24],
        [intro.trailingAnchor constraintLessThanOrEqualToAnchor:content.trailingAnchor constant:-24],
        [rows.topAnchor constraintEqualToAnchor:intro.bottomAnchor constant:18],
        [rows.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:24],
        [self.testResult.topAnchor constraintEqualToAnchor:rows.bottomAnchor constant:14],
        [self.testResult.leadingAnchor constraintEqualToAnchor:rows.leadingAnchor],
        [self.testResult.trailingAnchor constraintLessThanOrEqualToAnchor:content.trailingAnchor constant:-24],
        [buttons.topAnchor constraintEqualToAnchor:self.testResult.bottomAnchor constant:14],
        [buttons.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-24],
        [buttons.bottomAnchor constraintLessThanOrEqualToAnchor:content.bottomAnchor constant:-20],
    ]];

    [self.setupWindow center];
    [self.setupWindow makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [self.setupWindow makeFirstResponder:self.hostField];
    [self.hostField selectText:nil];
}

- (void)runTest:(id)sender {
    NSString *host = self.hostField.stringValue;
    if (host.length == 0) {
        self.testResult.stringValue = @"host를 먼저 입력하세요";
        return;
    }
    self.testResult.stringValue = @"테스트 중… (최대 90초)";

    NSString *serverPy = [[self serverDir] stringByAppendingPathComponent:@"server.py"];
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    task.arguments = @[ @"python3", serverPy, @"--test", @"--host", host ];
    task.currentDirectoryURL = [NSURL fileURLWithPath:[self serverDir]];
    NSMutableDictionary *env = [NSProcessInfo.processInfo.environment mutableCopy];
    env[@"PATH"] = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
    task.environment = env;

    NSPipe *pipe = [NSPipe pipe];
    task.standardOutput = pipe;
    task.standardError = pipe;

    __weak typeof(self) weakSelf = self;
    task.terminationHandler = ^(NSTask *proc) {
        NSData *out = [pipe.fileHandleForReading readDataToEndOfFile];
        NSString *text = [[NSString alloc] initWithData:out encoding:NSUTF8StringEncoding] ?: @"";
        if (text.length == 0) text = @"(출력 없음)";
        dispatch_async(dispatch_get_main_queue(), ^{
            typeof(self) me = weakSelf;
            if (!me) return;
            me.testResult.stringValue = [text
                stringByReplacingOccurrencesOfString:@"\n" withString:@" · "];
            if (proc.terminationStatus != 0)
                me.testResult.textColor = [NSColor systemRedColor];
            else
                me.testResult.textColor = [NSColor systemGreenColor];
        });
    };
    NSError *err = nil;
    if (![task launchAndReturnError:&err]) {
        self.testResult.stringValue =
            [NSString stringWithFormat:@"테스트 실행 실패: %@", err.localizedDescription];
    }
}

- (void)saveSetup:(id)sender {
    NSString *host = self.hostField.stringValue;
    if (host.length == 0) { [self.hostField selectText:nil]; return; }
    NSInteger interval = self.intervalField.integerValue;
    if (interval < 5) interval = 15;
    NSInteger port = self.portField.integerValue;
    if (port < 1 || port > 65535) port = 8777;

    [self writeConfig:@{ @"host": host,
                         @"interval": @(interval),
                         @"port": @(port) }];
    [self.setupWindow close];
    self.setupWindow = nil;

    if (self.initialSetup) {
        self.initialSetup = NO;
        [self startServer];
    } else if (self.server) {
        [self.server terminate];
        [self startServer];
    }
}

- (void)buildMenu {
    NSMenu *main = [[NSMenu alloc] init];

    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:@"설정…" action:@selector(showSetup:) keyEquivalent:@","];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"브라우저에서 열기" action:@selector(openInBrowser:) keyEquivalent:@"b"];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"가리기" action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItemWithTitle:@"종료" action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;
    [main addItem:appItem];

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"편집"];
    [editMenu addItemWithTitle:@"잘라내기" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"복사" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"붙여넣기" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItem:[NSMenuItem separatorItem]];
    [editMenu addItemWithTitle:@"전체 선택" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;
    [main addItem:editItem];

    NSMenuItem *viewItem = [[NSMenuItem alloc] init];
    NSMenu *viewMenu = [[NSMenu alloc] initWithTitle:@"보기"];
    [viewMenu addItemWithTitle:@"새로고침" action:@selector(reload:) keyEquivalent:@"r"];
    viewItem.submenu = viewMenu;
    [main addItem:viewItem];

    NSApp.mainMenu = main;
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        AppDelegate *delegate = [[AppDelegate alloc] init];
        app.delegate = delegate;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app run];
    }
    return 0;
}
