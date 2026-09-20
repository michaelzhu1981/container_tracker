// Native Chrome automation. Always address a process, window and tab by ID.
function run(argv) {
    const request = JSON.parse(argv[0]);
    let stage = request.action;
    function fail(code, message) { return {ok: false, code: code, error: message}; }
    function tabInfo(t) { return {id: Number(t.id()), url: t.url() || ""}; }
    try {
        const app = Application(request.pid);
        if (!app.running()) return JSON.stringify(fail("BROWSER_CLOSED", "Chrome process has exited."));
        const windows = app.windows();
        if (request.action === "inventory") {
            return JSON.stringify({ok: true, value: windows.map(w => ({
                id: Number(w.id()), tabs: w.tabs().map(tabInfo)
            }))});
        }
        if (request.action === "new_window") {
            const before = new Set(windows.map(w => Number(w.id())));
            // Chrome can create the window but return an invalid object
            // specifier to JXA. Recover by observing IDs, never by creating twice.
            let createError = "";
            try { app.windows.push(app.Window()); }
            catch (e) { createError = String(e); }
            stage = "new_window.locate";
            const deadline = Date.now() + 3000;
            while (Date.now() < deadline) {
                const created = app.windows().find(w => !before.has(Number(w.id())));
                if (created && created.tabs().length) {
                    const wid = Number(created.id());
                    const w = app.windows.byId(String(wid));
                    const t = w.tabs.byId(String(w.tabs()[0].id()));
                    stage = "new_window.navigate";
                    t.url = request.url;
                    return JSON.stringify({ok: true, value: {id: wid, tab: tabInfo(t)}});
                }
                delay(0.05);
            }
            return JSON.stringify(fail("BROWSER_CLOSED", "Chrome did not create a tracking tab. " + createError));
        }
        if (!windows.some(w => Number(w.id()) === request.window_id))
            return JSON.stringify(fail("BROWSER_CLOSED", "The tracking Chrome window was closed."));
        const w = app.windows.byId(String(request.window_id));
        let value = null;
        if (request.action === "tabs") value = w.tabs().map(tabInfo);
        else if (request.action === "close_window") { w.close(); value = true; }
        else if (request.action === "new_tab") {
            const before = new Set(w.tabs().map(t => Number(t.id())));
            try { w.tabs.push(app.Tab({url: request.url})); } catch (e) { /* Check the created ID below. */ }
            const deadline = Date.now() + 3000;
            while (Date.now() < deadline) {
                const t = w.tabs().find(t => !before.has(Number(t.id())));
                if (t) { value = tabInfo(t); break; }
                delay(0.05);
            }
            if (!value) return JSON.stringify(fail("TAB_NOT_FOUND", "Chrome did not create a result tab."));
        } else {
            const tabs = w.tabs();
            const index = tabs.findIndex(t => Number(t.id()) === request.tab_id);
            if (index < 0) return JSON.stringify(fail("TAB_NOT_FOUND", "The tracking tab was closed or is no longer available."));
            const t = w.tabs.byId(String(request.tab_id));
            if (request.action === "evaluate") value = t.execute({javascript: request.script});
            else if (request.action === "tab") value = tabInfo(t);
            else if (request.action === "navigate") { t.url = request.url; value = tabInfo(t); }
            else if (request.action === "close_tab") { t.close(); value = true; }
            else if (request.action === "activate" || request.action === "bounds") {
                w.activeTabIndex = index + 1;
                w.index = 1;
                app.activate();
                value = request.action === "bounds" ? w.bounds() : true;
            } else return JSON.stringify(fail("NAVIGATION", "Unknown Chrome bridge action."));
        }
        return JSON.stringify({ok: true, value: value === undefined ? null : value});
    } catch (e) {
        // Localized JXA errors can collapse an Apple Events denial to only
        // "Error: 发生错误。".  Preserve the numeric field so -1743 is
        // classified correctly on every macOS language.
        const errorNumber = Number(e && e.errorNumber);
        const suffix = Number.isFinite(errorNumber) ? " (" + errorNumber + ")" : "";
        const message = stage + ": " + String(e) + suffix;
        // Only an explicit Chrome/OS permission error is a permission failure.
        const denied = errorNumber === -1743 || errorNumber === -10004
            || /javascript.*apple\s*(events?|事件|script)|apple\s*(events?|事件|script).*javascript|not authorized|not permitted|未获授权|不允许访问|(-1743)|(-10004)/i.test(message);
        return JSON.stringify(fail(denied ? "BROWSER_PERMISSION" : "NAVIGATION", message));
    }
}
