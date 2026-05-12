// QMK Profile Sniffer v2
// Вставь в DevTools на странице qmk.topdriver, затем переключай профили.
// В конце вызови: qmkSniffer.export()  — получишь готовый JSON для profiles_config.json

(function () {
    if (window.qmkSniffer) {
        console.warn("Sniffer уже активен. Сброс старого состояния.");
        window.qmkSniffer.stop();
    }

    const captured = [];
    const seenHex = new Set();
    let lastPayload = "";
    let armed = false;
    let pendingLabel = null;

    const origFeature = HIDDevice.prototype.sendFeatureReport;
    const origOutput = HIDDevice.prototype.sendReport;

    function toHex(arr) {
        return Array.from(arr).map(b => '0x' + b.toString(16).padStart(2, '0')).join(', ');
    }

    function toBytes(data) {
        return Array.from(new Uint8Array(data.buffer || data));
    }

    function hook(orig, type) {
        return function (reportId, data) {
            const bytes = toBytes(data);
            const hex = bytes.join(',');

            if (armed && hex !== lastPayload && !seenHex.has(hex)) {
                seenHex.add(hex);
                lastPayload = hex;

                const entry = {
                    type,
                    reportId,
                    data: bytes,
                    label: pendingLabel || `profile_${captured.length + 1}`,
                    ts: new Date().toISOString()
                };
                captured.push(entry);

                console.log(
                    `%c[Поймано #${captured.length}] ${entry.label} (${type})`,
                    "color:#00ff00;font-weight:bold"
                );
                console.log("  Bytes:", toHex(bytes));
                pendingLabel = null;
            }
            return orig.apply(this, arguments);
        };
    }

    HIDDevice.prototype.sendFeatureReport = hook(origFeature, "feature");
    HIDDevice.prototype.sendReport = hook(origOutput, "output");

    window.qmkSniffer = {
        arm(label) {
            pendingLabel = label || null;
            armed = true;
            lastPayload = "";
            console.log(
                `%c[ARMED] Жду пакет${label ? ` для "${label}"` : ""}. Переключи профиль сейчас.`,
                "color:#ffaa00;font-weight:bold"
            );
        },
        disarm() {
            armed = false;
            console.log("[DISARMED]");
        },
        capture(label) {
            this.arm(label);
        },
        list() {
            console.table(captured.map((c, i) => ({
                idx: i,
                label: c.label,
                type: c.type,
                reportId: c.reportId,
                firstBytes: toHex(c.data.slice(0, 6))
            })));
        },
        rename(idx, label) {
            if (captured[idx]) {
                captured[idx].label = label;
                console.log(`[${idx}] -> ${label}`);
            }
        },
        remove(idx) {
            captured.splice(idx, 1);
            console.log("Removed", idx);
        },
        clear() {
            captured.length = 0;
            seenHex.clear();
            lastPayload = "";
            console.log("Cleared");
        },
        export() {
            const payloads = {};
            for (const c of captured) {
                payloads[c.label] = { data: c.data, hotkey: "" };
            }
            const json = JSON.stringify(payloads, null, 4);
            console.log("%c=== payloads для profiles_config.json ===", "color:#00aaff;font-weight:bold");
            console.log(json);
            try {
                navigator.clipboard.writeText(json);
                console.log("%c(скопировано в буфер обмена)", "color:#888");
            } catch (e) { }
            return payloads;
        },
        stop() {
            HIDDevice.prototype.sendFeatureReport = origFeature;
            HIDDevice.prototype.sendReport = origOutput;
            delete window.qmkSniffer;
            console.log("Sniffer остановлен.");
        }
    };

    console.log("%cQMK Sniffer v2 готов", "color:#00ff00;font-size:14px;font-weight:bold");
    console.log("Команды:");
    console.log("  qmkSniffer.arm('Gaming')   — жду следующий новый пакет и называю его 'Gaming'");
    console.log("  qmkSniffer.list()          — показать пойманные");
    console.log("  qmkSniffer.rename(0,'X')   — переименовать");
    console.log("  qmkSniffer.remove(0)       — удалить");
    console.log("  qmkSniffer.export()        — JSON в консоль + буфер обмена");
    console.log("  qmkSniffer.clear()         — очистить список");
    console.log("  qmkSniffer.stop()          — снять хуки");
})();
