import AppKit
import Darwin

final class Launcher: NSObject, NSApplicationDelegate {
    private let backend = Process()
    private var workbench: URL?
    private var quitting = false

    @objc func openWorkbench() {
        if let url = workbench { NSWorkspace.shared.open(url) }
    }

    private func showError(_ details: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "工作台启动失败"
        alert.informativeText = details
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = menu.addItem(withTitle: "", action: nil, keyEquivalent: "")
        let actions = NSMenu()
        actions.addItem(withTitle: "打开工作台", action: #selector(openWorkbench), keyEquivalent: "o").target = self
        actions.addItem(.separator())
        actions.addItem(withTitle: "退出 MultiTypes-CrawlSpider", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = actions
        NSApp.mainMenu = menu

        let resources = Bundle.main.resourceURL!
        let fm = FileManager.default
        do {
            let support = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            let data = support.appendingPathComponent("MultiTypes-CrawlSpider", isDirectory: true)
            try fm.createDirectory(at: data, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let logURL = data.appendingPathComponent("desktop.log")
            if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil) }
            let log = try FileHandle(forWritingTo: logURL)
            defer { try? log.close() }
            try log.seekToEnd()
            let stdout = Pipe()
            backend.executableURL = resources.appendingPathComponent("python/bin/python3.12")
            backend.arguments = ["-B", "-s", "-u", resources.appendingPathComponent("collector/serve.py").path, "--desktop"]
            backend.currentDirectoryURL = data
            backend.environment = [
                "HOME": fm.homeDirectoryForCurrentUser.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TMPDIR": NSTemporaryDirectory(), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "LANG": "en_US.UTF-8", "CRAWL_DATA_DIR": data.path
            ]
            backend.standardOutput = stdout
            backend.standardError = log
            backend.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    if self.quitting { NSApp.reply(toApplicationShouldTerminate: true) }
                    else { self.showError("本机服务已退出。请查看 " + logURL.path) }
                }
            }
            try backend.run()
            DispatchQueue.global().async {
                let file = stdout.fileHandleForReading
                guard let stream = fdopen(dup(file.fileDescriptor), "r") else { return }
                defer { fclose(stream) }
                var line = [CChar](repeating: 0, count: 64)
                guard fgets(&line, Int32(line.count), stream) != nil,
                      let port = UInt16(String(cString: line).trimmingCharacters(in: .whitespacesAndNewlines)),
                      port > 0 else { return }
                DispatchQueue.main.async {
                    self.workbench = URL(string: "http://127.0.0.1:\(port)/ui/")
                    self.openWorkbench()
                }
                // Keep draining stdout for the lifetime of the child.
                while fgets(&line, Int32(line.count), stream) != nil {}
            }
        } catch { showError(error.localizedDescription) }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openWorkbench()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard backend.isRunning else { return .terminateNow }
        quitting = true
        backend.terminate()
        return .terminateLater
    }
}

let app = NSApplication.shared
let launcher = Launcher()
app.delegate = launcher
app.setActivationPolicy(.regular)
app.run()
