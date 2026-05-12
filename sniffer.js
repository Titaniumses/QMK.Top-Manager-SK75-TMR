// QMK Profile Sniffer v3
// Captures BOTH outbound writes (sendFeatureReport / sendReport)
// AND inbound responses (receiveFeatureReport return values + inputreport events).
// Paste into DevTools at https://qmk.top, then exercise the UI.
// Use qmkSniffer.armBattery() before clicking the battery refresh in the web UI.

(function () {
    if (window.qmkSniffer) {
        console.warn("Sniffer уже активен. Сброс старого состояния.");
        window.qmkSniffer.stop();
    }

    const captured = []; // unified ordered log: { dir, type, reportId, data, label, ts }
    let armed = false;
    let batteryMode = false;
    let pendingLabel = null;

    const origSendFeature = HIDDevice.prototype.sendFeatureReport;
    const origSendOutput = HIDDevice.prototype.sendReport;
    const origRecvFeature = HIDDevice.prototype.receiveFeatureReport;

    function toHex(arr) {
        return Array.from(arr).map(b => '0x' + b.toString(16).padStart(2, '0')).join(', ');
    }

    function toBytes(data) {
        if (data instanceof DataView) {
            return Array.from(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
        }
        return Array.from(new Uint8Array(data.buffer || data));
    }

    function record(dir, type, reportId, bytes) {
        if (!armed && !batteryMode) return;
        const entry = {
            dir, type, reportId,
            data: bytes,
            label: pendingLabel || `${dir}_${captured.length + 1}`,
            ts: new Date().toISOString()
        };
        captured.push(entry);
        const color = dir === 'tx' ? '#ffaa00' : '#00ff88';
        console.log(
            `%c[#${captured.length}] ${dir.toUpperCase()} ${type} reportId=${reportId} (${entry.label})`,
            `color:${color};font-weight:bold`
        );
        console.log("  Bytes:", toHex(bytes));
        pendingLabel = null;
    }

    HIDDevice.prototype.sendFeatureReport = function (reportId, data) {
        record('tx', 'feature', reportId, toBytes(data));
        return origSendFeature.apply(this, arguments);
    };
    HIDDevice.prototype.sendReport = function (reportId, data) {
        record('tx', 'output', reportId, toBytes(data));
        return origSendOutput.apply(this, arguments);
    };
    HIDDevice.prototype.receiveFeatureReport = function (reportId) {
        const promise = origRecvFeature.apply(this, arguments);
        return promise.then(view => {
            record('rx', 'feature', reportId, toBytes(view));
            return view;
        });
    };

    function attachInputListener(device) {
        if (device.__qmkInputAttached) return;
        device.__qmkInputAttached = true;
        device.addEventListener('inputreport', (event) => {
            record('rx', 'input', event.reportId, toBytes(event.data));
        });
    }

    if (navigator.hid && navigator.hid.getDevices) {
        navigator.hid.getDevices().then(devs => devs.forEach(attachInputListener));
    }

    window.qmkSniffer = {
        arm(label) {
            pendingLabel = label || null;
            armed = true;
            console.log(
                `%c[ARMED] Жду следующий пакет${label ? ` для "${label}"` : ""}.`,
                "color:#ffaa00;font-weight:bold"
            );
        },
        armBattery() {
            batteryMode = true;
            armed = false;
            pendingLabel = null;
            console.log(
                "%c[BATTERY MODE] Логирую ВСЕ tx/rx. Нажми кнопку обновления батареи на qmk.top, потом qmkSniffer.export().",
                "color:#00aaff;font-weight:bold"
            );
        },
        disarm() {
            armed = false;
            batteryMode = false;
            console.log("[DISARMED]");
        },
        list() {
            console.table(captured.map((c, i) => ({
                idx: i, dir: c.dir, type: c.type, reportId: c.reportId,
                label: c.label, firstBytes: toHex(c.data.slice(0, 8))
            })));
        },
        rename(idx, label) {
            if (captured[idx]) { captured[idx].label = label; }
        },
        remove(idx) { captured.splice(idx, 1); },
        clear() { captured.length = 0; },
        export() {
            const outgoing = captured.filter(c => c.dir === 'tx');
            const incoming = captured.filter(c => c.dir === 'rx');
            const result = { outgoing, incoming, all: captured };
            const json = JSON.stringify(result, null, 2);
            console.log("%c=== sniffer export ===", "color:#00aaff;font-weight:bold");
            console.log(json);
            try { navigator.clipboard.writeText(json); console.log("(copied to clipboard)"); }
            catch (e) { }
            return result;
        },
        stop() {
            HIDDevice.prototype.sendFeatureReport = origSendFeature;
            HIDDevice.prototype.sendReport = origSendOutput;
            HIDDevice.prototype.receiveFeatureReport = origRecvFeature;
            delete window.qmkSniffer;
            console.log("Sniffer остановлен.");
        }
    };

    console.log("%cQMK Sniffer v3 готов", "color:#00ff00;font-size:14px;font-weight:bold");
    console.log("Команды:");
    console.log("  qmkSniffer.armBattery()   — режим логирования всех tx/rx (для батареи)");
    console.log("  qmkSniffer.arm('Gaming')  — поймать следующий tx (как раньше)");
    console.log("  qmkSniffer.list()         — таблица пойманного");
    console.log("  qmkSniffer.export()       — JSON {outgoing, incoming} в консоль + буфер");
    console.log("  qmkSniffer.clear()        — очистить");
    console.log("  qmkSniffer.stop()         — снять хуки");
})();
