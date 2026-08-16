import AppKit
import Darwin
import Foundation
import WebKit

private let defaultServerPort = 8765

private final class BackendManager {
    private(set) var process: Process?
    private(set) var serverPort = defaultServerPort
    private var logHandle: FileHandle?
    private var watchdogTimer: Timer?
    private var consecutiveHealthFailures = 0
    private var recoveryInProgress = false
    let projectRoot: URL
    let stateURL: URL
    let logURL: URL

    var serverURL: URL {
        URL(string: "http://127.0.0.1:\(serverPort)/")!
    }

    private var healthURL: URL {
        URL(string: "http://127.0.0.1:\(serverPort)/api/health")!
    }

    init() {
        projectRoot = Self.resolveProjectRoot()
        stateURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/SensUs Workstation", isDirectory: true)
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/SensUs Workstation", isDirectory: true)
        try? FileManager.default.createDirectory(at: stateURL, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("server.log")
    }

    func ensureServer(completion: @escaping (Result<URL, Error>) -> Void) {
        healthCheck { [weak self] available in
            guard let self else { return }
            if available {
                DispatchQueue.main.async {
                    self.beginHealthMonitoring()
                    completion(.success(self.serverURL))
                }
                return
            }
            DispatchQueue.main.async {
                do {
                    self.serverPort = try Self.findAvailablePort(preferred: defaultServerPort)
                    try self.launchServer()
                    self.pollUntilReady(attempt: 0, completion: completion)
                } catch {
                    completion(.failure(error))
                }
            }
        }
    }

    private func launchServer() throws {
        let bundledBackend = Bundle.main.resourceURL?
            .appendingPathComponent("backend/SensUsBackend")
        let usingBundledBackend = bundledBackend.map {
            FileManager.default.isExecutableFile(atPath: $0.path)
        } ?? false
        let executable: URL
        let arguments: [String]
        if let bundledBackend, usingBundledBackend {
            executable = bundledBackend
            arguments = ["gui", "--host", "127.0.0.1", "--port", String(serverPort)]
        } else {
            let serverModule = projectRoot
                .appendingPathComponent("software/host/pa_host/gui_server.py")
            guard FileManager.default.fileExists(atPath: serverModule.path) else {
                throw BackendError.projectNotFound(projectRoot.path)
            }
            let pythonCandidates = [
                projectRoot.appendingPathComponent(".venv/bin/python3").path,
                projectRoot.appendingPathComponent(".venv/bin/python").path,
                "/opt/homebrew/bin/python3",
                "/usr/local/bin/python3",
                "/usr/bin/python3",
            ]
            guard let python = pythonCandidates.first(where: {
                FileManager.default.isExecutableFile(atPath: $0)
            }) else {
                throw BackendError.pythonNotFound
            }
            executable = URL(fileURLWithPath: python)
            arguments = [
                "-m", "pa_host.gui_server",
                "--host", "127.0.0.1",
                "--port", String(serverPort),
            ]
        }

        try? logHandle?.close()
        logHandle = nil
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let handle = try FileHandle(forWritingTo: logURL)
        try handle.seekToEnd()
        logHandle = handle

        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = usingBundledBackend ? stateURL : projectRoot
        var environment = ProcessInfo.processInfo.environment
        environment["SENSUS_PROJECT_DIR"] = projectRoot.path
        environment["SENSUS_RESOURCE_DIR"] = projectRoot.path
        environment["SENSUS_STATE_DIR"] = stateURL.path
        environment["PYTHONUNBUFFERED"] = "1"
        if usingBundledBackend {
            if let resources = Bundle.main.resourceURL {
                let openocd = resources.appendingPathComponent("tools/openocd/bin/openocd")
                let scripts = resources.appendingPathComponent("tools/openocd/share/openocd/scripts")
                if FileManager.default.isExecutableFile(atPath: openocd.path) {
                    environment["SENSUS_OPENOCD_EXE"] = openocd.path
                    environment["SENSUS_OPENOCD_SCRIPTS"] = scripts.path
                }
            }
        } else {
            environment["PYTHONPATH"] = projectRoot
                .appendingPathComponent("software/host").path
        }
        process.environment = environment
        process.standardOutput = handle
        process.standardError = handle
        try process.run()
        self.process = process
    }

    private func pollUntilReady(
        attempt: Int,
        completion: @escaping (Result<URL, Error>) -> Void
    ) {
        if let process, !process.isRunning {
            completion(.failure(BackendError.serverExited(logURL.path)))
            return
        }
        healthCheck { [weak self] available in
            guard let self else { return }
            DispatchQueue.main.async {
                if available {
                    self.beginHealthMonitoring()
                    completion(.success(self.serverURL))
                } else if attempt >= 80 {
                    completion(.failure(BackendError.serverTimeout(self.logURL.path)))
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                        self.pollUntilReady(attempt: attempt + 1, completion: completion)
                    }
                }
            }
        }
    }

    private func healthCheck(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 0.7
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [projectRoot] data, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode
            guard error == nil, status == 200, let data,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let project = payload["project"] as? String else {
                completion(false)
                return
            }
            let reportedRoot = URL(fileURLWithPath: project, isDirectory: true)
                .standardizedFileURL.path
            completion(reportedRoot == projectRoot.standardizedFileURL.path)
        }.resume()
    }

    private static func findAvailablePort(preferred: Int) throws -> Int {
        let candidates = [preferred] + Array(49152...65535)
        for port in candidates where canBind(port: port) {
            return port
        }
        throw BackendError.noPortAvailable
    }

    private static func canBind(port: Int) -> Bool {
        let descriptor = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }
        defer { Darwin.close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(UInt16(port).bigEndian)
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(
                    descriptor,
                    $0,
                    socklen_t(MemoryLayout<sockaddr_in>.size)
                )
            }
        }
        return result == 0
    }

    func stopServer() {
        watchdogTimer?.invalidate()
        watchdogTimer = nil
        if let process, process.isRunning {
            process.terminate()
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
    }

    private func beginHealthMonitoring() {
        guard watchdogTimer == nil else { return }
        let timer = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.checkForRecovery()
        }
        RunLoop.main.add(timer, forMode: .common)
        watchdogTimer = timer
    }

    private func checkForRecovery() {
        healthCheck { [weak self] available in
            guard let self else { return }
            DispatchQueue.main.async {
                if available {
                    self.consecutiveHealthFailures = 0
                    return
                }
                self.consecutiveHealthFailures += 1
                guard self.consecutiveHealthFailures >= 2 else { return }
                self.recoverServer()
            }
        }
    }

    private func recoverServer() {
        guard !recoveryInProgress else { return }
        recoveryInProgress = true
        if let process, process.isRunning {
            process.terminate()
        }
        process = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self else { return }
            do {
                self.serverPort = try Self.findAvailablePort(preferred: self.serverPort)
                try self.launchServer()
                self.pollUntilReady(attempt: 0) { [weak self] _ in
                    self?.consecutiveHealthFailures = 0
                    self?.recoveryInProgress = false
                }
            } catch {
                self.recoveryInProgress = false
            }
        }
    }

    private static func resolveProjectRoot() -> URL {
        let manager = FileManager.default
        let environment = ProcessInfo.processInfo.environment
        var candidates: [URL] = []

        if let resources = Bundle.main.resourceURL {
            candidates.append(resources.appendingPathComponent("workstation", isDirectory: true))
        }

        if let configured = environment["SENSUS_PROJECT_DIR"], !configured.isEmpty {
            candidates.append(URL(fileURLWithPath: configured, isDirectory: true))
        }

        // Prefer the folder shipped with the App so a copied or renamed
        // delivery package never depends on the build machine's path.
        var ancestor = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0..<7 {
            candidates.append(ancestor)
            ancestor.deleteLastPathComponent()
        }

        if let marker = Bundle.main.url(forResource: "project-root", withExtension: "txt"),
           let path = try? String(contentsOf: marker, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !path.isEmpty {
            candidates.append(URL(fileURLWithPath: path, isDirectory: true))
        }
        if let saved = UserDefaults.standard.string(forKey: "projectRoot"), !saved.isEmpty {
            candidates.append(URL(fileURLWithPath: saved, isDirectory: true))
        }

        if let root = candidates.first(where: {
            manager.fileExists(atPath: $0
                .appendingPathComponent("software/host/pa_host/gui_server.py").path)
        }) {
            UserDefaults.standard.set(root.standardizedFileURL.path, forKey: "projectRoot")
            return root.standardizedFileURL
        }
        return candidates.first ?? Bundle.main.bundleURL.deletingLastPathComponent()
    }

    private enum BackendError: LocalizedError {
        case projectNotFound(String)
        case pythonNotFound
        case serverExited(String)
        case serverTimeout(String)
        case noPortAvailable

        var errorDescription: String? {
            switch self {
            case .projectNotFound(let path):
                return "找不到完整项目目录：\(path)"
            case .pythonNotFound:
                return "找不到项目 Python 环境。请先双击“01-首次安装.command”。"
            case .serverExited(let log):
                return "后台服务提前退出。日志：\(log)"
            case .serverTimeout(let log):
                return "后台服务启动超时。日志：\(log)"
            case .noPortAvailable:
                return "找不到可用的本地服务端口，请关闭占用本地端口的程序后重试"
            }
        }
    }
}

private extension NSToolbarItem.Identifier {
    static let showOverlay = NSToolbarItem.Identifier("com.sensus.workstation.overlay")
    static let showMain = NSToolbarItem.Identifier("com.sensus.workstation.main")
    static let pinWindow = NSToolbarItem.Identifier("com.sensus.workstation.pin")
    static let reloadPage = NSToolbarItem.Identifier("com.sensus.workstation.reload")
    static let openBrowser = NSToolbarItem.Identifier("com.sensus.workstation.browser")
}

private final class OverlayPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

private final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
    NSToolbarDelegate, WKNavigationDelegate {

    private let backend = BackendManager()
    private var mainWindow: NSWindow!
    private var mainWebView: WKWebView!
    private var overlayPanel: OverlayPanel!
    private var overlayWebView: WKWebView!
    private weak var pinButton: NSButton?
    private var pinned: Bool = {
        if UserDefaults.standard.object(forKey: "alwaysOnTop") == nil {
            return true
        }
        return UserDefaults.standard.bool(forKey: "alwaysOnTop")
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMenus()
        configureMainWindow()
        configureOverlayPanel()
        showLoadingPage()
        backend.ensureServer { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let url):
                self.mainWebView.load(
                    URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
                )
            case .failure(let error):
                self.showStartupError(error.localizedDescription)
            }
        }
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        if overlayPanel.isVisible {
            overlayPanel.orderFrontRegardless()
        } else {
            showMainWindow()
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        backend.stopServer()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    private func makeWebView() -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.allowsMagnification = true
        return webView
    }

    private func configureMainWindow() {
        mainWebView = makeWebView()
        mainWindow = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        mainWindow.title = "SensUs 电化学工作站"
        mainWindow.delegate = self
        mainWindow.contentView = mainWebView
        mainWindow.minSize = NSSize(width: 940, height: 620)
        mainWindow.isReleasedWhenClosed = false
        mainWindow.tabbingMode = .disallowed
        mainWindow.titlebarAppearsTransparent = false
        mainWindow.toolbarStyle = .unified

        let toolbar = NSToolbar(identifier: "SensUsWorkstationToolbar")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = false
        mainWindow.toolbar = toolbar

        if !mainWindow.setFrameUsingName("SensUsWorkstationMainWindow") {
            mainWindow.center()
        }
        mainWindow.setFrameAutosaveName("SensUsWorkstationMainWindow")
        applyPinnedState()
        mainWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func configureOverlayPanel() {
        overlayWebView = makeWebView()
        overlayPanel = OverlayPanel(
            contentRect: NSRect(x: 0, y: 0, width: 370, height: 500),
            styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        overlayPanel.title = "SensUs 悬浮检测"
        overlayPanel.delegate = self
        overlayPanel.contentView = overlayWebView
        overlayPanel.minSize = NSSize(width: 340, height: 440)
        overlayPanel.maxSize = NSSize(width: 520, height: 720)
        overlayPanel.isReleasedWhenClosed = false
        overlayPanel.isFloatingPanel = true
        overlayPanel.hidesOnDeactivate = false
        overlayPanel.becomesKeyOnlyIfNeeded = true
        overlayPanel.tabbingMode = .disallowed
        overlayPanel.toolbarStyle = .unifiedCompact
        overlayPanel.level = .floating
        overlayPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        overlayPanel.animationBehavior = .utilityWindow

        let toolbar = NSToolbar(identifier: "SensUsOverlayToolbar")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.allowsUserCustomization = false
        overlayPanel.toolbar = toolbar

        if !overlayPanel.setFrameUsingName("SensUsWorkstationOverlayWindow"),
           let visibleFrame = NSScreen.main?.visibleFrame {
            let frame = overlayPanel.frame
            overlayPanel.setFrameOrigin(NSPoint(
                x: visibleFrame.maxX - frame.width - 20,
                y: visibleFrame.maxY - frame.height - 20
            ))
        }
        keepOverlayOnScreen()
        overlayPanel.setFrameAutosaveName("SensUsWorkstationOverlayWindow")
        overlayPanel.orderOut(nil)
    }

    private func keepOverlayOnScreen() {
        let currentFrame = overlayPanel.frame
        let targetScreen = NSScreen.screens.first(where: {
            $0.visibleFrame.intersects(currentFrame)
        }) ?? NSScreen.main
        guard let visibleFrame = targetScreen?.visibleFrame else { return }

        var frame = currentFrame
        frame.size.width = min(frame.width, visibleFrame.width - 16)
        frame.size.height = min(frame.height, visibleFrame.height - 16)
        frame.origin.x = min(
            max(frame.origin.x, visibleFrame.minX + 8),
            visibleFrame.maxX - frame.width - 8
        )
        frame.origin.y = min(
            max(frame.origin.y, visibleFrame.minY + 8),
            visibleFrame.maxY - frame.height - 8
        )
        overlayPanel.setFrame(frame, display: false)
    }

    private func configureMenus() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 SensUs 电化学工作站",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 SensUs 电化学工作站",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 SensUs 电化学工作站",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "显示")
        viewMenu.addItem(withTitle: "重新载入", action: #selector(reloadPage), keyEquivalent: "r")
        let overlay = viewMenu.addItem(withTitle: "切换悬浮检测窗",
                                       action: #selector(toggleOverlayMode), keyEquivalent: "o")
        overlay.keyEquivalentModifierMask = [.command, .shift]
        let pin = viewMenu.addItem(withTitle: "保持窗口置顶", action: #selector(togglePin),
                                   keyEquivalent: "p")
        pin.keyEquivalentModifierMask = [.command, .shift]
        viewItem.submenu = viewMenu
        mainMenu.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)),
                           keyEquivalent: "m")
        windowMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)),
                           keyEquivalent: "")
        windowMenu.addItem(.separator())
        windowMenu.addItem(withTitle: "显示主窗口", action: #selector(showMainWindow),
                           keyEquivalent: "0")
        windowItem.submenu = windowMenu
        mainMenu.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = mainMenu
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        if toolbar.identifier == "SensUsOverlayToolbar" {
            return [.flexibleSpace, .showMain, .reloadPage]
        }
        return [.flexibleSpace, .showOverlay, .pinWindow, .reloadPage, .openBrowser]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarAllowedItemIdentifiers(toolbar)
    }

    func toolbar(
        _ toolbar: NSToolbar,
        itemForItemIdentifier itemIdentifier: NSToolbarItem.Identifier,
        willBeInsertedIntoToolbar flag: Bool
    ) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: itemIdentifier)
        let button = NSButton()
        button.bezelStyle = .texturedRounded
        button.imagePosition = .imageOnly
        button.target = self

        switch itemIdentifier {
        case .showOverlay:
            button.action = #selector(showOverlayWindow)
            button.image = NSImage(systemSymbolName: "pip.enter",
                                   accessibilityDescription: "打开悬浮检测窗")
            button.toolTip = "打开悬浮检测窗"
            item.label = "悬浮检测窗"
        case .showMain:
            button.action = #selector(showMainWindow)
            button.image = NSImage(systemSymbolName: "pip.exit",
                                   accessibilityDescription: "展开完整工作站")
            button.toolTip = "展开完整工作站"
            item.label = "完整工作站"
        case .pinWindow:
            button.action = #selector(togglePin)
            pinButton = button
            updatePinButton()
            item.label = "窗口置顶"
            item.paletteLabel = "窗口置顶"
        case .reloadPage:
            button.action = #selector(reloadPage)
            button.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "重新载入")
            button.toolTip = "重新载入界面"
            item.label = "重新载入"
        case .openBrowser:
            button.action = #selector(openInBrowser)
            button.image = NSImage(systemSymbolName: "safari", accessibilityDescription: "在浏览器打开")
            button.toolTip = "在浏览器中打开"
            item.label = "浏览器"
        default:
            return nil
        }

        button.frame = NSRect(x: 0, y: 0, width: 34, height: 28)
        item.view = button
        return item
    }

    @objc private func togglePin() {
        pinned.toggle()
        UserDefaults.standard.set(pinned, forKey: "alwaysOnTop")
        applyPinnedState()
    }

    private func applyPinnedState() {
        guard mainWindow != nil else { return }
        mainWindow.level = pinned ? .floating : .normal
        mainWindow.collectionBehavior = pinned
            ? [.canJoinAllSpaces, .fullScreenAuxiliary]
            : [.managed, .fullScreenPrimary]
        updatePinButton()
    }

    private func updatePinButton() {
        let symbol = pinned ? "pin.fill" : "pin"
        let description = pinned ? "取消窗口置顶" : "保持窗口置顶"
        pinButton?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: description)
        pinButton?.toolTip = description
        pinButton?.contentTintColor = pinned ? .controlAccentColor : .secondaryLabelColor
    }

    @objc private func reloadPage() {
        if overlayPanel.isVisible {
            loadOverlayPage()
        } else if mainWebView.url == nil {
            mainWebView.load(
                URLRequest(url: backend.serverURL, cachePolicy: .reloadIgnoringLocalCacheData)
            )
        } else {
            mainWebView.reloadFromOrigin()
        }
    }

    @objc private func openInBrowser() {
        NSWorkspace.shared.open(backend.serverURL)
    }

    @objc private func showMainWindow() {
        overlayPanel.orderOut(nil)
        mainWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func showOverlayWindow() {
        loadOverlayPage()
        mainWindow.orderOut(nil)
        keepOverlayOnScreen()
        overlayPanel.level = .floating
        overlayPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        overlayPanel.orderFrontRegardless()
    }

    @objc private func toggleOverlayMode() {
        if overlayPanel.isVisible {
            showMainWindow()
        } else {
            showOverlayWindow()
        }
    }

    private func loadOverlayPage() {
        let compactFile = backend.projectRoot
            .appendingPathComponent("software/host/pa_host/gui/compact.html")
        guard let html = try? String(contentsOf: compactFile, encoding: .utf8) else {
            overlayWebView.loadHTMLString(Self.statusHTML(
                title: "无法打开悬浮检测窗",
                detail: "找不到迷你检测界面：\(compactFile.path)",
                isError: true
            ), baseURL: nil)
            return
        }
        overlayWebView.loadHTMLString(html, baseURL: backend.serverURL)
    }

    private func showLoadingPage() {
        mainWebView.loadHTMLString(Self.statusHTML(
            title: "正在启动工作站",
            detail: "正在连接本地电化学服务…",
            isError: false
        ), baseURL: nil)
    }

    private func showStartupError(_ detail: String) {
        mainWebView.loadHTMLString(Self.statusHTML(
            title: "工作站未能启动",
            detail: detail,
            isError: true
        ), baseURL: nil)
    }

    private static func statusHTML(title: String, detail: String, isError: Bool) -> String {
        let escapedTitle = title.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        let escapedDetail = detail.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        let accent = isError ? "#b7423a" : "#187c78"
        return """
        <!doctype html><meta charset="utf-8">
        <style>
        :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
        body { margin: 0; height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }
        main { width: min(440px, 80vw); text-align: center; }
        i { display: block; width: 28px; height: 28px; margin: 0 auto 22px; border: 3px solid color-mix(in srgb, \(accent) 22%, transparent); border-top-color: \(accent); border-radius: 50%; animation: spin .9s linear infinite; }
        .error i { animation: none; border-radius: 7px; border-color: \(accent); }
        h1 { font-size: 20px; font-weight: 650; letter-spacing: 0; margin: 0 0 9px; }
        p { font-size: 14px; line-height: 1.6; opacity: .7; margin: 0; overflow-wrap: anywhere; }
        @keyframes spin { to { transform: rotate(360deg); } }
        </style>
        <main class="\(isError ? "error" : "")"><i></i><h1>\(escapedTitle)</h1><p>\(escapedDetail)</p></main>
        """
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.host == "127.0.0.1" || url.scheme == "about" {
            decisionHandler(.allow)
        } else if navigationAction.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }
}

let application = NSApplication.shared
private let delegate = AppDelegate()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
