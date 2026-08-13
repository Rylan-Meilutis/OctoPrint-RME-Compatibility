$(function () {
    function RmeCompatibilityViewModel(parameters) {
        var self = this;
        self.settings = parameters[0];
        self.loginState = parameters[1];
        self.printerState = parameters[2];
        self.files = parameters[3];
        self.state = ko.observable({
            connected: false,
            supported: false,
            session: {},
            machine: {},
            active_tool: {},
            loaded_filaments: [],
            stats: {},
            storage: {},
            firmware: {},
            spoolmanager: {},
            firmware_files: []
        });
        self.transportRecoveryRequired = ko.pureComputed(function () {
            return !!((self.state().firmware || {}).recovery_required);
        });
        self.tick = ko.observable(Date.now());
        self.coreTiming = null;
        self.pendingUpload = ko.observable(null);
        self.piUploadActive = ko.observable(false);
        self.piUploadProgress = ko.observable(0);
        self.pendingStorageUpload = ko.observable(null);
        self.storageUploadActive = ko.observable(false);
        self.storageUploadProgress = ko.observable(0);
        self.downloadChoiceEntry = ko.observable(null);
        self.handledDownloadJobs = {};
        self.activeDownloadJobId = null;
        self.nativeFilesKey = "";
        self.fileManagerBridgeInstalled = false;
        self.octoprintFirmwareEntry = ko.observable(null);
        self.selectedFirmware = ko.observable();
        self.partialCleanupPath = ko.observable("");
        self.mappingRows = ko.observableArray([]);
        self.mappingEnabled = ko.observable(true);
        self.physicalTools = ko.observableArray([]);
        self.spoolSelectionRows = ko.observableArray([]);
        self.newSpoolTool = ko.observable(0);
        self.unlockPin = ko.observable("");
        self.lightScreen = ko.observable(100);
        self.lightChamber = ko.observable(100);
        self.lightStatus = ko.observable(100);
        self.lockPinConfig = ko.observable("");
        self.lockTimeout = ko.observable(300);
        self.lockSerial = ko.observable(true);
        self.lockEnabled = ko.observable(false);
        self.themeKeys = [
            {key: "primary", label: "primary", value: ko.observable("#3366cc")},
            {key: "progress", label: "progress", value: ko.observable("#00aa55")},
            {key: "warning", label: "warning", value: ko.observable("#ffaa00")},
            {key: "error", label: "error", value: ko.observable("#dd2222")},
            {key: "image", label: "image", value: ko.observable("#101018")}
        ];
        self.themePresets = [
            {name: "RME Indigo", colors: {primary: "#4b2e83", progress: "#4b2e83", warning: "#ffcc00", error: "#ff2f2f", image: "#4b2e83"}},
            {name: "Prusa Orange", colors: {primary: "#fa6831", progress: "#00a878", warning: "#ffb000", error: "#dc3545", image: "#fa6831"}},
            {name: "Ocean Blue", colors: {primary: "#1976d2", progress: "#00a6a6", warning: "#ffb300", error: "#e53935", image: "#124e78"}},
            {name: "Forest", colors: {primary: "#2e7d32", progress: "#43a047", warning: "#f9a825", error: "#c62828", image: "#1b5e20"}},
            {name: "High Contrast", colors: {primary: "#ffffff", progress: "#00ff66", warning: "#ffdd00", error: "#ff3030", image: "#d0d0d0"}}
        ];
        ko.utils.arrayForEach(self.themePresets, function (preset) {
            preset.swatches = self.themeKeys.map(function (entry) { return preset.colors[entry.key]; });
        });
        self.providerSyncNotice = null;
        self.providerSyncNoticeKey = "";
        self.lightSnapshotKey = "";
        self.persistentLightProfiles = [
            makeLightProfile("screen", "Screen", 20, 20, 100, 60),
            makeLightProfile("chamber", "Chamber", 20, 20, 100, 100),
            makeLightProfile("status", "Status", 20, 20, 100, 100)
        ];
        // Only reset this persistent form when a different firmware request
        // arrives; routine websocket snapshots must not erase in-progress edits.
        self.pendingSpoolKey = "";
        self.spoolSelectionKey = "";
        var coreRenderPending = false;
        self.newSpoolName = ko.observable("");
        self.newSpoolVendor = ko.observable("");
        self.newSpoolMaterial = ko.observable("PLA");
        self.newSpoolColor = ko.observable("#808080");
        self.newSpoolColorName = ko.observable("");
        self.newSpoolWeight = ko.observable(1000);
        self.newSpoolNozzle = ko.observable(215);
        self.newSpoolBed = ko.observable(60);

        self.connectionText = ko.pureComputed(function () {
            var value = self.state();
            if (!value.connected) return "Printer disconnected";
            if (!value.supported) return "RME protocol not detected";
            return value.session && value.session.active ? "RME session active" : "RME firmware detected";
        });
        self.connectionClass = ko.pureComputed(function () {
            var value = self.state();
            return value.supported ? "label-success" : (value.connected ? "label-warning" : "label-default");
        });
        self.activeTool = ko.pureComputed(function () { return self.state().active_tool || {}; });
        self.activeToolVisible = ko.pureComputed(function () {
            return !!self.state().supported && self.activeTool().logical !== null && self.activeTool().logical !== undefined;
        });
        self.activeToolColor = ko.pureComputed(function () {
            var color = self.activeTool().color;
            return typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color) ? color : "#808080";
        });
        self.activeToolText = ko.pureComputed(function () {
            var tool = self.activeTool();
            if (tool.logical === null || tool.logical === undefined) {
                return self.mmuDetected() ? "MMU idle" : "Active extruder not reported yet";
            }
            var logical = Number(tool.logical);
            var physical = tool.physical === null || tool.physical === undefined ? logical : Number(tool.physical);
            var label = "Extruder T" + logical;
            if (physical !== logical) label += " → physical T" + physical;
            if (tool.material && tool.material !== "---") label += " · " + tool.material;
            if (tool.color_name && tool.color_name !== "None") label += " · " + tool.color_name;
            return label;
        });

        var workflowNames = {
            mmu: "MMU filament handling",
            tool_change: "Tool change / pickup",
            filament_runout: "Filament runout",
            stuck_filament: "Stuck filament recovery",
            pressure_advance: "Pressure advance calibration",
            probing: "Bed probing",
            heating: "Heating",
            firmware_update: "Firmware update",
            waste_bin: "Purge bucket / waste bin",
            filament_load: "Loading filament",
            filament_unload: "Unloading filament",
            chamber_vent: "Chamber vent movement",
            filtration: "Chamber filtration",
            printer: "Printer workflow"
        };
        self.workflow = ko.pureComputed(function () { return self.state().workflow || {}; });
        self.workflowVisible = ko.pureComputed(function () {
            var workflow = self.workflow();
            var state = String(workflow.state || "").toLowerCase();
            var terminal = ["canceled", "cancelled", "closed", "complete", "completed", "idle", "skipped", "stopped"];
            if (!workflow.workflow || terminal.indexOf(state) >= 0) return false;
            return !(workflow.workflow === "chamber_vent" && state === "open" && Number(workflow.progress) >= 100);
        });
        self.workflowTitle = ko.pureComputed(function () {
            var key = self.workflow().workflow;
            return workflowNames[key] || (key ? key.replace(/_/g, " ") : "Printer activity");
        });
        self.workflowState = ko.pureComputed(function () {
            var state = self.workflow().state || "active";
            return state.charAt(0).toUpperCase() + state.slice(1);
        });
        self.workflowMessage = ko.pureComputed(function () { return self.workflow().message || self.workflowTitle(); });
        self.workflowIndeterminate = ko.pureComputed(function () { return !isFinite(Number(self.workflow().progress)); });
        self.workflowWidth = ko.pureComputed(function () {
            var progress = Number(self.workflow().progress);
            return isFinite(progress) ? Math.max(0, Math.min(100, progress)) + "%" : "100%";
        });
        self.workflowTiming = ko.pureComputed(function () {
            self.tick();
            var started = Number(self.workflow().phase_started_at || self.workflow().received_at || 0) * 1000;
            if (!started) return "";
            var seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
            var progress = self.workflow().progress;
            return "Phase active for " + formatDuration(seconds) + (isFinite(Number(progress)) ? " · " + progress + "%" : "");
        });

        self.prompt = ko.pureComputed(function () { return self.state().prompt || {}; });
        self.hasFirmwarePrompt = ko.pureComputed(function () { return self.prompt().kind === "firmware"; });
        self.hasToolmapPrompt = ko.pureComputed(function () { return self.prompt().kind === "toolmap"; });
        self.promptMessage = ko.pureComputed(function () { return self.prompt().message || "Printer action required"; });
        self.promptActions = ko.pureComputed(function () { return self.prompt().actions || []; });
        self.toolmapTimeoutText = ko.pureComputed(function () {
            self.tick();
            var prompt = self.prompt();
            if (prompt.kind !== "toolmap") return "";
            if (prompt.timer_paused) return "Timeout paused because you started interacting.";
            if (!prompt.deadline) return "No automatic timeout is configured.";
            var remaining = Math.max(0, Math.ceil(Number(prompt.deadline) - Date.now() / 1000));
            return "Current mapping will be kept in " + formatDuration(remaining) + " unless you interact.";
        });
        self.spoolmanager = ko.pureComputed(function () { return self.state().spoolmanager || {}; });
        self.externalSpoolProvider = ko.pureComputed(function () {
            return ["spoolmanager", "spoolman"].indexOf(String(self.spoolmanager().provider || "").toLowerCase()) >= 0;
        });
        self.internalSpoolProvider = ko.pureComputed(function () {
            return self.spoolmanager().provider === "internal";
        });
        self.spoolOwnershipText = ko.pureComputed(function () {
            var provider = String(self.spoolmanager().provider || "").toLowerCase();
            if (provider === "spoolmanager") return "Spools managed by SpoolManager";
            if (provider === "spoolman") return "Spools managed by Spoolman";
            return "Spools managed by RME Compatibility";
        });
        self.spoolmanagerStatus = ko.pureComputed(function () {
            var spool = self.spoolmanager();
            var provider = spool.provider ? spool.provider + ": " : "";
            return provider + (spool.error || spool.status || "not checked");
        });
        self.publishedSpools = ko.pureComputed(function () { return self.spoolmanager().published || []; });
        self.inventorySpools = ko.pureComputed(function () { return self.spoolmanager().inventory || []; });
        self.selectedSpools = ko.pureComputed(function () { return self.spoolmanager().selected || []; });
        self.loadedFilaments = ko.pureComputed(function () { return self.state().loaded_filaments || []; });
        self.pendingNewSpool = ko.pureComputed(function () { return self.spoolmanager().pending_new || null; });
        self.pendingProviderSync = ko.pureComputed(function () {
            return self.spoolmanager().pending_provider_sync || null;
        });
        self.stats = ko.pureComputed(function () { return self.state().stats || {}; });
        self.storage = ko.pureComputed(function () { return self.state().storage || {}; });
        self.storageEntries = ko.pureComputed(function () { return self.storage().entries || []; });
        self.storageDownloadJob = ko.pureComputed(function () { return self.storage().download || {}; });
        self.storageDownloadWidth = ko.pureComputed(function () {
            return Math.max(0, Math.min(100, Number(self.storageDownloadJob().progress) || 0)) + "%";
        });
        self.storageDownloadActive = ko.pureComputed(function () {
            return ["queued", "downloading"].indexOf(self.storageDownloadJob().status) >= 0;
        });
        self.storageDownloads = ko.pureComputed(function () {
            return (self.storage().downloads || []).slice().reverse();
        });
        self.storageDownloadSummary = ko.pureComputed(function () {
            var job = self.storageDownloadJob();
            if (!job.id) return "";
            if (job.status === "queued") return "Waiting for the printer transfer queue…";
            if (job.status === "downloading") {
                return (job.name || "Printer file") + " · " + formatBytes(job.offset || 0) +
                    " of " + formatBytes(job.size || 0);
            }
            if (job.status === "error") return job.error || "Download failed";
            return (job.name || "Printer file") + " is stored safely on the Pi.";
        });
        self.storageStatus = ko.pureComputed(function () {
            var storage = self.storage();
            var text = storage.error || storage.status || "not checked";
            if (storage.progress !== null && storage.progress !== undefined) text += " · " + storage.progress + "%";
            return text;
        });
        self.storageParentVisible = ko.pureComputed(function () { return self.storage().path !== "/"; });
        self.formatStat = function (key, value) {
            var number = Number(value);
            if (!Number.isFinite(number)) return String(value);
            if (/_m$/.test(key)) return formatDistance(number);
            if (/_s$/.test(key)) return formatDurationLong(number);
            return number.toLocaleString();
        };
        self.statsRows = ko.pureComputed(function () {
            var values = self.stats().values || {};
            return Object.keys(values).sort().map(function (key) {
                var label = key.replace(/_(m|s)$/, "").replace(/_/g, " ");
                label = label.charAt(0).toUpperCase() + label.slice(1);
                return {key: key, label: label, value: self.formatStat(key, values[key])};
            });
        });
        self.currentThemeRows = ko.pureComputed(function () {
            var theme = self.state().theme || {};
            return self.themeKeys.filter(function (entry) {
                return theme[entry.key] !== undefined;
            }).map(function (entry) {
                return {label: entry.label, color: toHexColor(theme[entry.key])};
            });
        });
        self.currentLightsText = ko.pureComputed(function () {
            var light = self.state().light || {};
            var live = light.live || {};
            var values = [];
            if (live.state) {
                values.push(String(live.state).replace(/_/g, " "));
                if (Number(live.screen) >= 0) values.push("screen " + live.screen + "%");
                if (Number(live.chamber) >= 0) values.push("chamber " + live.chamber + "%");
            } else {
                if (Number(light.screen_print) >= 0) values.push("screen " + light.screen_print + "%");
                if (Number(light.chamber_print) >= 0) values.push("chamber " + light.chamber_print + "%");
            }
            if (light.status_print !== undefined && Number(light.status_print) >= 0) values.push("print status " + light.status_print + "%");
            return values.length ? values.join(" · ") : "Not reported yet";
        });
        self.lightPolicyText = ko.pureComputed(function () {
            var policy = (self.state().light || {}).policy || {};
            var values = [];
            if (policy.activity_timeout_s !== undefined) values.push("activity " + formatDurationLong(policy.activity_timeout_s));
            if (policy.event_timeout_s !== undefined) values.push("event " + formatDurationLong(policy.event_timeout_s));
            if (policy.off_timeout_s !== undefined) values.push("off " + formatDurationLong(policy.off_timeout_s));
            if (policy.status_finished_hold_s !== undefined) values.push("finished status " + formatDurationLong(policy.status_finished_hold_s));
            if (policy.door_holds_active !== undefined) values.push(Number(policy.door_holds_active) ? "door keeps lights active" : "door hold disabled");
            if (policy.post_print_hold !== undefined) values.push(Number(policy.post_print_hold) ? "post-print hold enabled" : "post-print hold disabled");
            return values.join(" · ");
        });
        self.navbarTransfer = ko.pureComputed(function () {
            var state = self.state();
            var firmware = state.firmware || {};
            var storage = state.storage || {};
            var partial = storage.partial || null;
            var download = storage.download || {};
            var firmwareStatuses = ["queued", "canceling", "starting", "uploading", "verifying", "flash_queued", "flashing", "restarting"];
            var progress;
            function numericProgress(value) {
                if (value === null || value === undefined || value === "") return null;
                var number = Number(value);
                return isFinite(number) ? number : null;
            }
            if (self.piUploadActive()) {
                progress = Number(self.piUploadProgress());
                return {
                    active: true, icon: "fa-microchip", title: "Firmware upload",
                    summary: "Firmware → Pi · " + Math.round(progress || 0) + "%",
                    detail: "Uploading firmware to OctoPrint", progress: progress
                };
            }
            if (firmwareStatuses.indexOf(String(firmware.status || "")) >= 0) {
                progress = numericProgress(firmware.progress);
                var firmwareLabels = {
                    queued: "Firmware queued", canceling: "Canceling firmware transfer",
                    starting: "Starting firmware transfer", uploading: "Firmware → printer",
                    verifying: "Verifying firmware", flash_queued: "Firmware flash queued",
                    flashing: "Flashing firmware",
                    restarting: "Printer restarting"
                };
                var firmwareLabel = firmwareLabels[firmware.status] || "Firmware update";
                return {
                    active: true, icon: "fa-microchip", title: "Firmware update",
                    summary: firmwareLabel + (progress !== null ? " · " + Math.round(progress) + "%" : ""),
                    detail: firmware.filename || firmwareLabel,
                    progress: progress
                };
            }
            if (partial) {
                return {
                    active: true, icon: "fa-upload", title: "Interrupted upload",
                    summary: partial.status === "discarding" ? "Discarding printer partial" :
                        (partial.status === "queued" ? "Resume queued" : "Upload interrupted · action required"),
                    detail: partial.remote_path || "Interrupted printer upload",
                    progress: null
                };
            }
            if (["queued", "downloading"].indexOf(String(download.status || "")) >= 0) {
                progress = numericProgress(download.progress);
                return {
                    active: true, icon: "fa-download", title: "File download",
                    summary: (download.status === "queued" ? "Download queued" : "Printer → Pi") +
                        (progress !== null ? " · " + Math.round(progress) + "%" : ""),
                    detail: download.name || "Downloading printer file",
                    progress: progress
                };
            }
            if (storage.status === "uploading") {
                progress = numericProgress(storage.progress);
                return {
                    active: true, icon: "fa-upload", title: "File upload",
                    summary: "File → printer" + (progress !== null ? " · " + Math.round(progress) + "%" : ""),
                    detail: "Verified printer USB transfer",
                    progress: progress
                };
            }
            if (self.storageUploadActive()) {
                progress = Number(self.storageUploadProgress());
                return {
                    active: true, icon: "fa-upload", title: "File upload",
                    summary: "File → OctoPrint · " + Math.round(progress || 0) + "%",
                    detail: (self.pendingStorageUpload() || {}).name || "Uploading file",
                    progress: progress
                };
            }
            return {active: false, icon: "", title: "", summary: "", detail: "", progress: null};
        });
        self.navbarTransferActive = ko.pureComputed(function () { return !!self.navbarTransfer().active; });
        self.navbarTransferWidth = ko.pureComputed(function () {
            var value = self.navbarTransfer().progress;
            var progress = value === null || value === undefined ? NaN : Number(value);
            return isFinite(progress) ? Math.max(0, Math.min(100, progress)) + "%" : "100%";
        });
        self.navbarTransferIndeterminate = ko.pureComputed(function () {
            var value = self.navbarTransfer().progress;
            return value === null || value === undefined || !isFinite(Number(value));
        });
        self.navbarTransferPercentText = ko.pureComputed(function () {
            if (!self.navbarTransferActive() || self.navbarTransferIndeterminate()) return "";
            return Math.round(Number(self.navbarTransfer().progress)) + "%";
        });
        self.mmuDetected = ko.pureComputed(function () {
            var machine = self.state().machine || {};
            var workflow = self.workflow();
            var values = self.stats().values || {};
            // mmu_changes is emitted as zero on every firmware target. Only
            // HAS_MMU-only failure fields are positive hardware evidence.
            var statsEvidence = Object.keys(values).some(function (key) {
                return /^(mmu_load_|mmu_general_)/.test(key);
            });
            return workflow.workflow === "mmu" || statsEvidence ||
                (Number(machine.logical_tools || 0) > 1 && Number(machine.hotends || 0) === 1);
        });
        self.multiToolDetected = ko.pureComputed(function () {
            return Number((self.state().machine || {}).logical_tools || 0) > 1;
        });
        self.navbarVisible = ko.pureComputed(function () {
            // Keep a small health indicator present even before discovery. If
            // this item is visible, the plugin assets and view model loaded;
            // richer tool and MMU data appears after firmware discovery.
            self.state();
            return true;
        });
        self.navbarMmuActive = ko.pureComputed(function () {
            return self.workflow().workflow === "mmu" && self.workflowVisible();
        });
        self.navbarMmuActionable = ko.pureComputed(function () {
            return self.navbarMmuActive() && self.hasFirmwarePrompt();
        });
        self.navbarModeText = ko.pureComputed(function () {
            if (self.transportRecoveryRequired()) return "Printer reboot required";
            if (self.navbarTransferActive()) return self.navbarTransfer().title;
            if (!self.state().supported) return "RME Compatibility";
            return self.mmuDetected() ? "RME MMU" : "RME multi-tool";
        });
        self.navbarMmuText = ko.pureComputed(function () {
            var workflow = self.workflow();
            if (self.transportRecoveryRequired()) return "RME transmission locked for safety";
            if (self.navbarTransferActive()) return self.navbarTransfer().detail;
            if (!self.state().supported) return self.connectionText();
            if (!self.navbarMmuActive()) return self.mmuDetected() ? "MMU idle" : "Multi-tool ready";
            var state = String(workflow.state || "active").replace(/_/g, " ");
            var label = workflow.message || state.charAt(0).toUpperCase() + state.slice(1);
            if (isFinite(Number(workflow.progress))) label += " · " + workflow.progress + "%";
            return label;
        });
        self.navbarCompactText = ko.pureComputed(function () {
            if (self.transportRecoveryRequired()) return "Reboot printer";
            if (self.navbarTransferActive()) {
                // Keep the percentage in its own non-shrinking navbar badge.
                // The descriptive label may ellipsize on narrow windows.
                return self.navbarTransfer().summary.replace(/\s*·\s*\d+%\s*$/, "");
            }
            if (!self.state().connected) return "RME · disconnected";
            if (!self.state().supported) return "RME · not detected";
            if (self.navbarMmuActive()) {
                var state = String(self.workflow().state || "active").replace(/_/g, " ");
                var phase = self.workflow().message || state.charAt(0).toUpperCase() + state.slice(1);
                return "MMU · " + phase + (isFinite(Number(self.workflow().progress)) ? " " + self.workflow().progress + "%" : "");
            }
            var tool = self.activeTool();
            if (tool.logical === null || tool.logical === undefined) {
                return self.mmuDetected() ? "MMU · idle" : "RME · ready";
            }
            var label = "T" + Number(tool.logical);
            if (tool.material && tool.material !== "---") label += " · " + tool.material;
            return label;
        });
        self.navbarTitle = ko.pureComputed(function () {
            if (self.transportRecoveryRequired()) {
                return "Printer reboot required; no RME commands will be sent";
            }
            if (self.navbarTransferActive()) {
                return self.navbarTransfer().summary + " · " + self.navbarTransfer().detail;
            }
            if (!self.state().supported) return self.connectionText();
            var details = self.activeToolText();
            return self.navbarMmuActive() ? details + " · " + self.navbarMmuText() : details;
        });
        self.navbarIconClass = ko.pureComputed(function () {
            if (self.transportRecoveryRequired()) return "fa-exclamation-triangle";
            if (self.navbarTransferActive()) return self.navbarTransfer().icon;
            return self.mmuDetected() ? "fa-random" : "fa-tools";
        });
        self.navbarToolRows = ko.pureComputed(function () {
            var state = self.state();
            var machine = state.machine || {};
            var mapping = state.toolmap || {};
            var selected = {};
            var loaded = {};
            ko.utils.arrayForEach((state.spoolmanager || {}).selected || [], function (item) {
                selected[Number(item.tool)] = item;
            });
            ko.utils.arrayForEach(state.loaded_filaments || [], function (item) {
                loaded[Number(item.tool)] = item;
            });
            var active = self.activeTool().logical;
            var rows = [];
            for (var logical = 0; logical < Number(machine.logical_tools || 0); logical++) {
                // Printer-reported M865 loadout is authoritative; the active
                // inventory provider fills gaps while its sync is in flight.
                var item = loaded[logical] || selected[logical] || {};
                var physical = mapping.enabled && mapping.mapping && mapping.mapping[logical] !== undefined ?
                    Number(mapping.mapping[logical]) : logical;
                var material = item.material && item.material !== "---" ? item.material : "Unassigned";
                var colorName = item.color_name && item.color_name !== "None" ? item.color_name : "";
                var vendor = item.vendor || "";
                var color = typeof item.color === "string" && /^#[0-9a-f]{6}$/i.test(item.color) ? item.color : "#808080";
                rows.push({
                    logical: logical,
                    physical: physical,
                    label: "T" + logical + (physical !== logical ? " → T" + physical : ""),
                    details: material + (vendor ? " · " + vendor : "") + (colorName ? " · " + colorName : ""),
                    color: color,
                    active: active !== null && active !== undefined && Number(active) === logical
                });
            }
            return rows;
        });
        self.spoolLabel = function (spool) {
            var remaining = spool.remaining_weight;
            return (spool.alias ? spool.alias + " — " : "") + spool.display_name +
                (spool.vendor ? " · " + spool.vendor : "") + " · " + spool.material +
                (remaining === null || remaining === undefined ? "" : " · " + Number(remaining).toFixed(0) + " g left");
        };
        self.loadedFilamentLabel = function (item) {
            return "T" + item.tool + ": " + item.material +
                (item.vendor ? " · " + item.vendor : "") +
                (item.color_name && item.color_name !== "None" ? " · " + item.color_name : "");
        };

        self.firmwareFiles = ko.pureComputed(function () { return self.state().firmware_files || []; });
        self.firmwareLabel = function (file) { return file.name + " (" + formatBytes(file.size) + ")"; };
        self.firmware = ko.pureComputed(function () { return self.state().firmware || {}; });
        self.partialTransfer = ko.pureComputed(function () {
            return (self.state().storage || {}).partial || null;
        });
        self.partialRecoveryActive = ko.pureComputed(function () {
            var partial = self.partialTransfer();
            return !!partial && ["queued", "transferring", "discarding"].indexOf(partial.status) >= 0;
        });
        self.partialTransferSummary = ko.pureComputed(function () {
            var partial = self.partialTransfer();
            if (!partial) return "";
            var label = partial.remote_path || "printer upload";
            var detail = formatBytes(partial.size || 0) + " · " + (partial.transport || "negotiated transport");
            if (!partial.source_available) detail += " · source unavailable";
            return label + " — " + detail;
        });
        self.firmwareProgress = ko.pureComputed(function () { return Number(self.firmware().progress || 0) + "%"; });
        self.piUploadWidth = ko.pureComputed(function () {
            return Math.max(0, Math.min(100, Number(self.piUploadProgress()) || 0)) + "%";
        });
        self.piUploadStatus = ko.pureComputed(function () {
            if (!self.piUploadActive()) return "";
            var progress = Math.round(Number(self.piUploadProgress()) || 0);
            return progress < 100 ? "Uploading to Pi · " + progress + "%" : "Upload sent · saving on Pi…";
        });
        self.firmwareBusy = ko.pureComputed(function () {
            return ["queued", "canceling", "starting", "uploading", "verifying"].indexOf(self.firmware().status) >= 0;
        });
        self.firmwareActive = ko.pureComputed(function () { return self.firmwareBusy() || self.firmware().status === "ready"; });
        self.firmwareError = ko.pureComputed(function () {
            return self.firmware().status === "error" ? (self.firmware().error || "") : "";
        });
        self.firmwareStatus = ko.pureComputed(function () {
            var fw = self.firmware();
            if (fw.recovery_required) return "Communication locked. Power-cycle the printer, then confirm the reboot below. No RME commands or printer file actions will be sent until recovery is confirmed.";
            if (!fw.status || fw.status === "idle") return "No firmware candidate uploaded.";
            if (fw.status === "queued") return "Waiting for the printer USB queue; no firmware bytes have been sent yet.";
            if (fw.status === "canceling") return "Canceling the queued transfer before any firmware bytes are sent.";
            if (fw.status === "ready") return "Firmware candidate verified as protected FWUPD.RME. It is not armed for the bootloader; use Flash and reboot to request installation.";
            if (fw.status === "flash_queued") return "Flash command queued; waiting for the printer to confirm the bootloader restart.";
            if (fw.status === "flashing") return "Bootloader handoff requested; the printer should reboot.";
            if (fw.status === "restarting") return "Printer confirmed the firmware restart; waiting for USB to reconnect.";
            if (fw.status === "uploading") return "Sending " + formatBytes(fw.offset || 0) + " of " + formatBytes(fw.size || 0) +
                (fw.flash_after_stage ? "; flashing automatically after verification." : ".");
            if (fw.status === "verifying" && fw.flash_after_stage) return "Verifying on printer; flash will start automatically after success.";
            return fw.status.charAt(0).toUpperCase() + fw.status.slice(1) + ".";
        });
        self.canStage = ko.pureComputed(function () {
            return !!self.selectedFirmware() && self.state().supported &&
                !self.firmwareBusy() && ["flash_queued", "flashing", "restarting"].indexOf(self.firmware().status) < 0;
        });
        self.canFlash = ko.pureComputed(function () { return ["ready", "staged"].indexOf(self.firmware().status) >= 0; });
        self.canUnstage = ko.pureComputed(function () { return ["ready", "staged"].indexOf(self.firmware().status) >= 0; });

        self.hasMachine = ko.pureComputed(function () {
            var machine = self.state().machine || {};
            return machine.x_max !== undefined && machine.logical_tools !== undefined;
        });
        self.machineSummary = ko.pureComputed(function () {
            var m = self.state().machine || {};
            if (!self.hasMachine()) return "";
            return (Number(m.x_max) - Number(m.x_min)) + " × " +
                (Number(m.y_max) - Number(m.y_min)) + " × " +
                (Number(m.z_max) - Number(m.z_min)) + " mm · " +
                m.logical_tools + " logical tool" + (Number(m.logical_tools) === 1 ? "" : "s");
        });
        self.lockSummary = ko.pureComputed(function () {
            var lock = self.state().lock || {};
            if (!Number(lock.enabled)) return "disabled";
            return Number(lock.locked) ? "locked" : "unlocked";
        });

        self.command = function (command, data) {
            return OctoPrint.simpleApiCommand("rme_compatibility", command, data || {})
                .done(self.acceptState)
                .fail(function (xhr) {
                    new PNotify({title: "RME command failed", text: responseError(xhr), type: "error", hide: false});
                });
        };
        self.updateProviderSyncNotice = function (pending) {
            var key = pending ? [pending.tool, pending.database_id, pending.updated].join(":") : "";
            if (key === self.providerSyncNoticeKey) return;
            if (self.providerSyncNotice && typeof self.providerSyncNotice.remove === "function") {
                self.providerSyncNotice.remove();
            }
            self.providerSyncNotice = null;
            self.providerSyncNoticeKey = key;
            if (pending) {
                self.providerSyncNotice = new PNotify({
                    title: "Filament resynchronization required",
                    text: (pending.message || "Filament selections changed.") + " Use the RME top-bar menu or RME Compatibility Settings to choose.",
                    type: "notice",
                    hide: false
                });
            }
        };
        self.acceptState = function (value) {
            if (!value) return;
            var oldPrompt = self.state().prompt || {};
            self.state(value);
            var storageSupported = !!(value.supported && value.storage && value.storage.supported);
            var nativeFiles = (value.storage && value.storage.native_files) || [];
            var nativeKey = (storageSupported ? "rme|" : "native|") + nativeFiles.map(function (item) {
                return [item.path, item.size, item.category].join(":");
            }).join("|");
            if (self.fileManagerBridgeInstalled && nativeKey !== self.nativeFilesKey) {
                self.nativeFilesKey = nativeKey;
                self.files.requestData();
            }
            var download = value.storage && value.storage.download;
            if (download && download.id && self.activeDownloadJobId === "pending") {
                self.activeDownloadJobId = download.id;
            }
            if (download && download.id && self.activeDownloadJobId === download.id) {
                var progressDialog = ensureDownloadProgressDialog();
                progressDialog.find(".bar").css("width", Math.max(0, Math.min(100, Number(download.progress) || 0)) + "%");
                progressDialog.find(".rme-download-progress-text").text(self.storageDownloadSummary());
                progressDialog.find(".progress").toggleClass("active", ["queued", "downloading"].indexOf(download.status) >= 0);
                progressDialog.find(".rme-download-progress-close").toggle(download.status === "ready" || download.status === "error");
            }
            if (download && download.id && download.status === "ready" && !self.handledDownloadJobs[download.id]) {
                self.handledDownloadJobs[download.id] = true;
                if (download.target === "device") {
                    var link = document.createElement("a");
                    link.href = OctoPrint.getBlueprintUrl("rme_compatibility") +
                        "storage/downloads/" + encodeURIComponent(download.id);
                    link.download = download.name || "download.bin";
                    link.style.display = "none";
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                    new PNotify({title: "Printer file ready", text: download.name + " was copied through the Pi and sent to this device.", type: "success"});
                } else {
                    new PNotify({title: "Printer file stored on Pi", text: download.name, type: "success"});
                }
            }
            self.updateProviderSyncNotice(
                value.spoolmanager && value.spoolmanager.pending_provider_sync
            );
            var prompt = value.prompt || {};
            if (prompt.kind === "toolmap" && (oldPrompt.kind !== "toolmap" || self.mappingRows().length !== prompt.count)) {
                var count = Number(prompt.count || 0);
                var options = [];
                var rows = [];
                for (var index = 0; index < count; index++) options.push(index);
                for (var logical = 0; logical < count; logical++) {
                    var initial = prompt.mapping && prompt.mapping[logical] !== undefined ? prompt.mapping[logical] : logical;
                    rows.push({logical: logical, physical: ko.observable(Number(initial))});
                }
                self.physicalTools(options);
                self.mappingRows(rows);
                self.mappingEnabled(prompt.enabled !== false);
            }
            if (value.theme) {
                ko.utils.arrayForEach(self.themeKeys, function (entry) {
                    if (value.theme[entry.key] !== undefined) {
                        var color = toHexColor(value.theme[entry.key]);
                        if (entry.value() !== color) entry.value(color);
                    }
                });
            }
            if (value.lock && value.lock.enabled !== undefined) {
                var lockEnabled = !!Number(value.lock.enabled);
                if (self.lockEnabled() !== lockEnabled) self.lockEnabled(lockEnabled);
            }
            if (value.light) {
                var screen = Number(value.light.screen_print);
                var chamber = Number(value.light.chamber_print);
                var status = Number(value.light.status_print);
                if (screen >= 0 && self.lightScreen() !== screen) self.lightScreen(screen);
                if (chamber >= 0 && self.lightChamber() !== chamber) self.lightChamber(chamber);
                if (status >= 0 && self.lightStatus() !== status) self.lightStatus(status);
                if (Number(value.light.schema) >= 2) {
                    var snapshotKey = [value.light.screen, value.light.chamber, value.light.status,
                        value.light.screen_supported, value.light.chamber_supported,
                        value.light.status_supported].join(":");
                    if (snapshotKey !== self.lightSnapshotKey) {
                        self.lightSnapshotKey = snapshotKey;
                        var packed = {
                            screen: Number(value.light.screen) || 0,
                            chamber: Number(value.light.chamber) || 0,
                            status: Number(value.light.status) || 0
                        };
                        ko.utils.arrayForEach(self.persistentLightProfiles, function (profile) {
                            var supported = !!Number(value.light[profile.key + "_supported"]);
                            profile.supported(supported);
                            applyPackedBrightness(profile, packed[profile.key]);
                        });
                    }
                }
            }
            var pending = value.spoolmanager && value.spoolmanager.pending_new;
            var pendingKey = pending ? [pending.tool, pending.material, pending.color].join(":") : "";
            if (pending && pendingKey !== self.pendingSpoolKey) {
                self.pendingSpoolKey = pendingKey;
                self.newSpoolName(pending.display_name || "New spool");
                self.newSpoolVendor(pending.vendor || "");
                self.newSpoolMaterial(pending.material || "PLA");
                self.newSpoolColor(pending.color || "#808080");
                self.newSpoolColorName(pending.color_name || "");
                self.newSpoolWeight(pending.total_weight || 1000);
                self.newSpoolNozzle(pending.nozzle_temperature || 215);
                self.newSpoolBed(pending.bed_temperature || 60);
            } else if (!pending) {
                self.pendingSpoolKey = "";
            }
            var machineTools = Number((value.machine || {}).logical_tools || 0);
            var selectedByTool = {};
            ko.utils.arrayForEach((value.spoolmanager || {}).selected || [], function (item) {
                selectedByTool[Number(item.tool)] = Number(item.database_id);
            });
            var selectionKey = [machineTools].concat(Object.keys(selectedByTool).sort().map(function (tool) {
                return tool + ":" + selectedByTool[tool];
            })).join("|");
            if (selectionKey !== self.spoolSelectionKey) {
                self.spoolSelectionKey = selectionKey;
                var selectionRows = [];
                for (var tool = 0; tool < machineTools; tool++) {
                    selectionRows.push({
                        tool: tool,
                        selected: ko.observable(selectedByTool[tool] === undefined ? null : selectedByTool[tool])
                    });
                }
                self.spoolSelectionRows(selectionRows);
            }
            scheduleCoreWorkflowRender();
        };
        self.discover = function () { self.command("discover"); };
        self.openSession = function () { self.command("open_session"); };
        self.respond = function (action) { self.command("respond", {action: action}); };
        self.resetToolmap = function () { self.command("reset_toolmap"); };
        self.applyToolmap = function () {
            var mapping = {};
            var used = {};
            var duplicate = false;
            ko.utils.arrayForEach(self.mappingRows(), function (row) {
                var physical = Number(row.physical());
                duplicate = duplicate || used[physical] === true;
                used[physical] = true;
                mapping[row.logical] = physical;
            });
            if (duplicate) {
                new PNotify({title: "Invalid tool mapping", text: "Each physical tool can only be selected once.", type: "error"});
                return;
            }
            self.command("apply_toolmap", {mapping: mapping, enabled: self.mappingEnabled()});
        };
        self.touchToolmap = function () {
            if (self.hasToolmapPrompt() && !self.prompt().timer_paused) self.command("touch_toolmap");
            return true;
        };
        self.chooseUpload = function (_, event) { self.pendingUpload(event.target.files[0] || null); };
        self.uploadToPi = function () {
            var file = self.pendingUpload();
            if (!file) return;
            // Use OctoPrint's client so browser sessions and API-key sessions
            // both receive the required CSRF/authentication headers. This path
            // must be relative because the client prepends BASEURL itself;
            // PLUGIN_BASEURL would create //plugin at root installations.
            self.piUploadProgress(0);
            self.piUploadActive(true);
            OctoPrint.postForm(
                "plugin/rme_compatibility/firmware",
                {file: file},
                {xhr: function () {
                    var request = $.ajaxSettings.xhr();
                    if (request.upload) {
                        request.upload.addEventListener("progress", function (event) {
                            if (event.lengthComputable) {
                                self.piUploadProgress(event.total ? event.loaded * 100 / event.total : 0);
                            }
                        });
                    }
                    return request;
                }}
            ).done(function (response) {
                self.piUploadProgress(100);
                self.piUploadActive(false);
                self.pendingUpload(null);
                self.selectedFirmware(response.file.name);
                self.acceptState(response.state);
                new PNotify({title: "Firmware stored on Pi", text: response.file.name, type: "success"});
            }).fail(function (xhr) {
                self.piUploadActive(false);
                new PNotify({title: "Firmware upload failed", text: responseError(xhr), type: "error", hide: false});
            });
        };
        self.stageFirmware = function () { self.command("stage_firmware", {filename: self.selectedFirmware()}); };
        self.stageAndFlashFirmware = function () {
            if (window.confirm("Stage, verify, and flash this firmware in one operation? The printer will reboot only after the transfer is verified.")) {
                self.command("stage_and_flash_firmware", {filename: self.selectedFirmware()});
            }
        };
        self.stageOctoprintFirmware = function (flashAfterStage) {
            var entry = self.octoprintFirmwareEntry();
            if (!entry || !entry.path) return;
            var command = flashAfterStage ?
                "stage_and_flash_octoprint_firmware" : "stage_octoprint_firmware";
            ensureOctoprintFirmwareDialog().modal("hide");
            self.command(command, {path: entry.path});
        };
        self.cancelFirmware = function () { self.command("cancel_firmware"); };
        self.unstageFirmware = function () {
            if (window.confirm("Remove the staged firmware from the printer? The BBF stored on this Pi will be kept.")) {
                self.command("unstage_firmware");
            }
        };
        self.confirmPrinterReboot = function () {
            if (window.confirm("Confirm that the printer itself was power-cycled or rebooted? RME communication will be unlocked.")) {
                self.command("confirm_printer_reboot");
            }
        };
        self.resumePartialTransfer = function () {
            self.command("partial_resume");
        };
        self.discardPartialTransfer = function () {
            if (window.confirm("Discard this interrupted upload and remove its private partial data from the printer?")) {
                self.command("partial_discard");
            }
        };
        self.cleanupNamedPartial = function () {
            var path = String(self.partialCleanupPath() || "").trim();
            if (!path || !window.confirm("Probe and remove only " + path + ".rme-part and " + path + ".rme-meta?")) return;
            self.command("partial_cleanup", {path: path});
        };
        self.deleteFirmware = function () {
            var filename = self.selectedFirmware();
            if (!filename || !window.confirm("Delete " + filename + " from this Pi?")) return;
            self.command("delete_firmware", {filename: filename}).done(function () {
                self.selectedFirmware(null);
            });
        };
        self.flashFirmware = function () {
            if (window.confirm("Flash the verified firmware now? The printer will reboot and the bootloader will validate its signature and machine compatibility.")) {
                self.command("flash_firmware");
            }
        };
        self.applyMachineProfile = function () { self.command("apply_machine_profile"); };
        self.queryControls = function () { self.command("query_controls"); };
        self.uiControl = function (action, value) { self.command("ui_control", {action: action, value: value}); };
        self.lockNow = function () { self.command("lock_now"); };
        self.unlock = function () { self.command("lock_unlock", {pin: self.unlockPin()}); };
        self.applyLights = function () {
            self.command("set_temp_lights", {
                screen: Number(self.lightScreen()), chamber: Number(self.lightChamber()), status: Number(self.lightStatus())
            });
        };
        self.applyPersistentLights = function () {
            var values = {};
            ko.utils.arrayForEach(self.persistentLightProfiles, function (profile) {
                values[profile.key] = packBrightness(profile);
            });
            self.command("set_persistent_lights", values);
        };
        self.applyLockSettings = function () {
            self.command("set_lock", {
                pin: self.lockPinConfig(), timeout: Number(self.lockTimeout()),
                serial: self.lockSerial(), enabled: self.lockEnabled()
            });
        };
        self.applyTheme = function () {
            var colors = {};
            ko.utils.arrayForEach(self.themeKeys, function (entry) { colors[entry.key] = entry.value(); });
            self.command("set_theme", {colors: colors});
        };
        self.selectThemePreset = function (preset) {
            ko.utils.arrayForEach(self.themeKeys, function (entry) {
                entry.value(preset.colors[entry.key]);
            });
        };
        self.syncSpoolmanager = function () { self.command("sync_filaments_to_printer"); };
        self.syncFilamentsFromPrinter = function () { self.command("sync_filaments_from_printer"); };
        self.syncFilamentsToPrinter = function () { self.command("sync_filaments_to_printer"); };
        self.confirmProviderSync = function () { self.command("confirm_provider_sync"); };
        self.cancelProviderSync = function () { self.command("cancel_provider_sync"); };
        self.changeSpoolSelection = function (row) {
            var databaseId = row.selected();
            if (databaseId === null || databaseId === undefined || databaseId === "") {
                self.command("deselect_spool", {tool: row.tool});
            } else {
                self.command("select_spool", {tool: row.tool, database_id: Number(databaseId)});
            }
        };
        self.beginNewSpool = function () {
            self.command("begin_new_spool", {tool: Number(self.newSpoolTool())});
        };
        self.createSpool = function () {
            self.command("create_spool", {
                display_name: self.newSpoolName(), vendor: self.newSpoolVendor(),
                material: self.newSpoolMaterial(), color: self.newSpoolColor(),
                color_name: self.newSpoolColorName(), total_weight: Number(self.newSpoolWeight()),
                nozzle_temperature: Number(self.newSpoolNozzle()), bed_temperature: Number(self.newSpoolBed())
            });
        };
        self.cancelNewSpool = function () { self.command("cancel_new_spool"); };
        self.refreshStorage = function () {
            self.command("storage_list", {path: self.storage().path || "/"});
        };
        self.storageParent = function () {
            var parts = String(self.storage().path || "/").split("/").filter(Boolean);
            parts.pop();
            self.command("storage_list", {path: "/" + parts.join("/")});
        };
        self.storageOpen = function (entry) {
            if (entry.type === "dir") self.command("storage_list", {path: entry.path});
        };
        self.startStorageDownload = function (entry, target) {
            if (!entry || !entry.path) return;
            self.activeDownloadJobId = "pending";
            var dialog = ensureDownloadProgressDialog();
            dialog.find(".bar").css("width", "0%");
            dialog.find(".progress").addClass("active");
            dialog.find(".rme-download-progress-text").text("Waiting for the printer transfer queue…");
            dialog.find(".rme-download-progress-close").hide();
            dialog.modal("show");
            self.command("storage_download", {path: entry.path, target: target}).fail(function (xhr) {
                self.activeDownloadJobId = null;
                dialog.find(".progress").removeClass("active");
                dialog.find(".rme-download-progress-text").text(responseError(xhr));
                dialog.find(".rme-download-progress-close").show();
            });
        };
        self.storageDownloadToPi = function (entry) {
            self.startStorageDownload(entry, "pi");
        };
        self.storageDownloadToDevice = function (entry) {
            self.startStorageDownload(entry, "device");
        };
        self.storageDownload = function (entry) {
            self.downloadChoiceEntry(entry);
            ensureDownloadChoiceDialog().find(".rme-download-name").text(entry.name || entry.path);
            ensureDownloadChoiceDialog().modal("show");
        };
        self.downloadCachedStorageFile = function (entry) {
            if (!entry || !entry.id) return;
            var link = document.createElement("a");
            link.href = OctoPrint.getBlueprintUrl("rme_compatibility") +
                "storage/downloads/" + encodeURIComponent(entry.id);
            link.download = entry.name || "download.bin";
            link.style.display = "none";
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        };
        self.storageDelete = function (entry) {
            if (window.confirm("Delete " + entry.path + " from printer USB? This cannot be undone.")) {
                self.command("storage_delete", {path: entry.path});
            }
        };
        self.storageRename = function (entry) {
            var name = window.prompt("New name", entry.name);
            if (!name || name === entry.name) return;
            var parent = String(entry.path).split("/").slice(0, -1).join("/") || "/";
            self.command("storage_rename", {
                path: entry.path, destination: parent.replace(/\/$/, "") + "/" + name
            });
        };
        self.storageMove = function (entry) {
            var destination = window.prompt("New full printer path", entry.path);
            if (!destination || destination === entry.path) return;
            if (destination.charAt(0) !== "/") destination = "/" + destination;
            self.command("storage_rename", {
                path: entry.path, destination: destination
            });
        };
        self.storageMkdir = function () {
            var name = window.prompt("New folder name");
            if (!name) return;
            self.command("storage_mkdir", {
                path: String(self.storage().path || "/").replace(/\/$/, "") + "/" + name
            });
        };
        self.storagePrint = function (entry) {
            if (window.confirm("Start printing " + entry.name + " from printer USB?")) {
                self.command("storage_print", {path: entry.path});
            }
        };
        self.storageFlash = function (entry) {
            if (window.confirm("Queue " + entry.name + " for bootloader flashing? The printer will reboot.")) {
                self.command("storage_flash", {path: entry.path});
            }
        };
        self.chooseStorageUpload = function (_, event) {
            self.pendingStorageUpload(event.target.files[0] || null);
        };
        self.uploadStorage = function () {
            var file = self.pendingStorageUpload();
            if (!file) return;
            self.storageUploadProgress(0);
            self.storageUploadActive(true);
            OctoPrint.postForm(
                "plugin/rme_compatibility/storage/upload",
                {file: file, path: self.storage().path || "/"},
                {xhr: function () {
                    var request = $.ajaxSettings.xhr();
                    if (request.upload) {
                        request.upload.addEventListener("progress", function (event) {
                            if (event.lengthComputable) {
                                self.storageUploadProgress(event.total ? event.loaded * 100 / event.total : 0);
                            }
                        });
                    }
                    return request;
                }}
            ).done(function (response) {
                self.storageUploadProgress(100);
                self.storageUploadActive(false);
                self.pendingStorageUpload(null);
                self.acceptState(response);
                new PNotify({title: "Uploaded to printer USB", text: file.name, type: "success"});
            }).fail(function (xhr) {
                self.storageUploadActive(false);
                new PNotify({title: "Printer USB upload failed", text: responseError(xhr), type: "error", hide: false});
            });
        };
        self.storageFileSize = function (entry) { return entry.type === "dir" ? "Folder" : formatBytes(entry.size); };
        self.storageCanPrint = function (entry) { return entry.type === "file" && /\.(?:gcode|gco|bgcode)$/i.test(entry.name); };
        self.storageCanFlash = function (entry) { return entry.type === "file" && /\.bbf$/i.test(entry.name); };
        self.showRmeTab = function () {
            $("a[href='#tab_plugin_rme_compatibility']").tab("show");
        };

        self.onBeforeBinding = function () {
            installFileManagerBridge();
            OctoPrint.simpleApiGet("rme_compatibility").done(self.acceptState);
            window.setInterval(function () {
                self.tick(Date.now());
                if (self.workflowVisible()) scheduleCoreWorkflowRender();
            }, 1000);
        };
        self.onSettingsShown = function () {
            if (self.state().supported) {
                self.queryControls();
                if (self.storage().supported) self.refreshStorage();
                else self.command("storage_caps");
            }
        };
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin === "rme_compatibility") self.acceptState(data);
        };

        function ensureDownloadChoiceDialog() {
            var dialog = $("#rme-storage-download-choice");
            if (dialog.length) return dialog;
            dialog = $(
                '<div id="rme-storage-download-choice" class="modal hide fade" tabindex="-1">' +
                '<div class="modal-header"><button type="button" class="close" data-dismiss="modal">&times;</button><h3>Download printer file</h3></div>' +
                '<div class="modal-body"><p class="rme-download-name"></p><p>The printer copy is always staged safely on the Pi first.</p></div>' +
                '<div class="modal-footer"><button class="btn" data-dismiss="modal">Cancel</button>' +
                '<button class="btn rme-download-pi">Download to Pi</button>' +
                '<button class="btn btn-primary rme-download-device">Download to device</button></div></div>'
            ).appendTo(document.body);
            dialog.find(".rme-download-pi").on("click", function () {
                dialog.modal("hide");
                self.storageDownloadToPi(self.downloadChoiceEntry());
            });
            dialog.find(".rme-download-device").on("click", function () {
                dialog.modal("hide");
                self.storageDownloadToDevice(self.downloadChoiceEntry());
            });
            return dialog;
        }

        function ensureDownloadProgressDialog() {
            var dialog = $("#rme-storage-download-progress");
            if (dialog.length) return dialog;
            dialog = $(
                '<div id="rme-storage-download-progress" class="modal hide fade" tabindex="-1">' +
                '<div class="modal-header"><h3>Downloading printer file</h3></div>' +
                '<div class="modal-body"><div class="progress progress-striped active"><div class="bar" style="width:0%"></div></div>' +
                '<p class="rme-download-progress-text">Waiting for the printer transfer queue…</p></div>' +
                '<div class="modal-footer"><button class="btn rme-download-progress-close" data-dismiss="modal">Close</button></div></div>'
            ).appendTo(document.body);
            dialog.find(".rme-download-progress-close").on("click", function () {
                self.activeDownloadJobId = null;
            });
            return dialog;
        }

        function nativeFileTree() {
            var roots = [];
            var folders = {};
            function childrenFor(parts) {
                if (!parts.length) return roots;
                var path = parts.join("/");
                if (!folders[path]) {
                    var parent = childrenFor(parts.slice(0, -1));
                    var folder = {
                        type: "folder", typePath: ["folder"], name: parts[parts.length - 1],
                        display: parts[parts.length - 1], path: path, origin: "sdcard",
                        children: [], rme: true
                    };
                    folders[path] = folder;
                    parent.push(folder);
                }
                return folders[path].children;
            }
            ko.utils.arrayForEach((self.storage().native_files || []), function (item) {
                var parts = String(item.path || "").split("/").filter(Boolean);
                if (!parts.length) return;
                var filename = parts.pop();
                childrenFor(parts).push({
                    type: item.category === "model" ? "model" : "machinecode",
                    typePath: item.category === "model" ? ["model", "rme_artifact"] : ["machinecode", "gcode"],
                    name: filename, display: filename, path: String(item.path),
                    origin: "sdcard", size: Number(item.size || 0), rme: true,
                    refs: {resource: OctoPrint.getBlueprintUrl("rme_compatibility") + "storage/download"}
                });
            });
            return roots;
        }

        function installFileManagerBridge() {
            if (self.fileManagerBridgeInstalled || !self.files) return;
            self.fileManagerBridgeInstalled = true;
            var originalFromResponse = self.files.fromResponse;
            self.files.fromResponse = function (response, params) {
                var result;
                if (!(self.state().supported && self.storage().supported)) {
                    result = originalFromResponse.call(self.files, response, params);
                } else {
                    var merged = $.extend({}, response);
                    merged.files = (response.files || []).filter(function (entry) {
                        return entry.origin !== "sdcard";
                    }).concat(nativeFileTree());
                    result = originalFromResponse.call(self.files, merged, params);
                }
                window.setTimeout(decorateOctoprintFirmwareEntries, 0);
                return result;
            };
            var originalDownloadLink = self.files.downloadLink;
            self.files.downloadLink = function (entry) {
                return entry && entry.rme ? "#" : originalDownloadLink.call(self.files, entry);
            };
            var originalEnableMove = self.files.enableMove;
            self.files.enableMove = function (entry) {
                return entry && entry.rme ? true : originalEnableMove.call(self.files, entry);
            };
            var originalShowMoveDialog = self.files.showMoveDialog;
            self.files.showMoveDialog = function (entry, event) {
                if (!entry || !entry.rme) return originalShowMoveDialog.call(self.files, entry, event);
                self.storageMove({path: "/" + String(entry.path).replace(/^\//, ""), name: entry.name});
            };
            var originalRemoveEntry = self.files._removeEntry;
            self.files._removeEntry = function (entry, event) {
                if (!entry || !entry.rme) return originalRemoveEntry.call(self.files, entry, event);
                return self.command("storage_delete", {path: "/" + String(entry.path).replace(/^\//, "")})
                    .done(function () { self.files.requestData(); });
            };
            var originalEnableSelect = self.files.enableSelect;
            var originalEnableSelectAndPrint = self.files.enableSelectAndPrint;
            var originalEnableSlicing = self.files.enableSlicing;
            self.files.enableSelect = function (entry) {
                return entry && entry.rme && entry.type !== "machinecode" ? false :
                    originalEnableSelect.apply(self.files, arguments);
            };
            self.files.enableSelectAndPrint = function (entry) {
                return entry && entry.rme && entry.type !== "machinecode" ? false :
                    originalEnableSelectAndPrint.apply(self.files, arguments);
            };
            if (typeof originalEnableSlicing === "function") {
                self.files.enableSlicing = function (entry) {
                    return entry && entry.rme ? false : originalEnableSlicing.apply(self.files, arguments);
                };
            }
            $(document).off("click.rmeStorageDownload", "#files .btn-files-download")
                .on("click.rmeStorageDownload", "#files .btn-files-download", function (event) {
                    var entry = ko.dataFor(this);
                    if (!entry || !entry.rme) return;
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    self.storageDownload({
                        path: "/" + String(entry.path).replace(/^\//, ""), name: entry.name
                    });
                });
            $(document).off("click.rmeLocalFirmware", "#files .rme-local-firmware-action")
                .on("click.rmeLocalFirmware", "#files .rme-local-firmware-action", function (event) {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    var entry = ko.dataFor($(this).closest(".entry, li")[0]);
                    if (!isOctoprintFirmwareEntry(entry)) return;
                    self.octoprintFirmwareEntry(entry);
                    ensureOctoprintFirmwareDialog().find(".rme-firmware-name")
                        .text(entry.display || entry.name || entry.path);
                    ensureOctoprintFirmwareDialog().modal("show");
                });
        }

        function isOctoprintFirmwareEntry(entry) {
            return !!entry && entry.origin === "local" && entry.type !== "folder" &&
                /\.bbf$/i.test(String(entry.path || entry.name || ""));
        }

        function decorateOctoprintFirmwareEntries() {
            $("#files .entry, #files li").each(function () {
                var row = $(this);
                var entry = ko.dataFor(this);
                if (!isOctoprintFirmwareEntry(entry) || row.find(".rme-local-firmware-action").length) return;
                var host = row.find(".btn-group").last();
                if (!host.length) host = row;
                $('<button type="button" class="btn btn-mini rme-local-firmware-action" title="Firmware actions"><i class="fa fa-microchip"></i> Firmware</button>')
                    .appendTo(host);
            });
        }

        function ensureOctoprintFirmwareDialog() {
            var dialog = $("#rme-octoprint-firmware-dialog");
            if (dialog.length) return dialog;
            dialog = $(
                '<div id="rme-octoprint-firmware-dialog" class="modal hide fade" tabindex="-1">' +
                '<div class="modal-header"><button type="button" class="close" data-dismiss="modal">&times;</button><h3>Printer firmware</h3></div>' +
                '<div class="modal-body"><p class="rme-firmware-name"></p><p>Use this BBF from OctoPrint’s local Files storage. The guarded RME transfer remains blocked while printing or while another printer transfer owns the latch.</p></div>' +
                '<div class="modal-footer"><button class="btn" data-dismiss="modal">Cancel</button>' +
                '<button class="btn rme-fw-stage">Upload candidate</button>' +
                '<button class="btn btn-danger rme-fw-flash">Upload and flash…</button></div></div>'
            ).appendTo(document.body);
            dialog.find(".rme-fw-stage").on("click", function () {
                self.stageOctoprintFirmware(false);
            });
            dialog.find(".rme-fw-flash").on("click", function () {
                if (window.confirm("Upload, verify, and hand this firmware to the bootloader? The printer will reboot only after verification.")) {
                    self.stageOctoprintFirmware(true);
                }
            });
            return dialog;
        }

        function scheduleCoreWorkflowRender() {
            if (coreRenderPending) return;
            coreRenderPending = true;
            (window.requestAnimationFrame || function (callback) { return window.setTimeout(callback, 16); })(function () {
                coreRenderPending = false;
                renderCoreWorkflow();
            });
        }

        function renderCoreWorkflow() {
            var state = self.state();
            var workflow = state.workflow || {};
            var active = self.workflowVisible();
            var target = $("#state .progress").first();
            var strip = $("#rme-workflow-strip");
            var toolIndicator = $("#rme-active-tool-indicator");
            updateCorePrintTiming(active);
            if (!target.length) return;
            if (!strip.length) {
                strip = $('<div id="rme-workflow-strip"><div class="rme-strip-bar"></div><div class="rme-strip-label"></div></div>');
                target.after(strip);
            }
            if (!toolIndicator.length) {
                toolIndicator = $('<div id="rme-active-tool-indicator"><span class="rme-color-dot"></span><span class="rme-active-tool-label"></span></div>');
                strip.after(toolIndicator);
            }
            toolIndicator.toggle(self.activeToolVisible());
            if (self.activeToolVisible()) {
                toolIndicator.find(".rme-color-dot").css("background-color", self.activeToolColor());
                toolIndicator.find(".rme-active-tool-label").text(self.activeToolText());
                toolIndicator.attr("title", self.activeToolText());
            }
            strip.toggle(active);
            if (!active) return;
            var progress = Number(workflow.progress);
            var determinate = isFinite(progress);
            strip.toggleClass("rme-indeterminate", !determinate);
            strip.find(".rme-strip-bar").css("width", determinate ? Math.max(0, Math.min(100, progress)) + "%" : "100%");
            var started = Number(workflow.phase_started_at || workflow.received_at || 0) * 1000;
            var elapsed = started ? formatDuration(Math.max(0, Math.floor((Date.now() - started) / 1000))) : "";
            var label = self.workflowTitle() + " — " + (workflow.message || self.workflowState());
            if (determinate) label += " · " + progress + "%";
            if (elapsed) label += " · " + elapsed;
            strip.find(".rme-strip-label").text(label).attr("title", label);
        }

        function updateCorePrintTiming(activeWorkflow) {
            var printing = ko.unwrap(self.printerState.isPrinting) || ko.unwrap(self.printerState.isPausing);
            var reportedTime = Number(ko.unwrap(self.printerState.printTime));
            if (!activeWorkflow || !printing || !isFinite(reportedTime)) {
                self.coreTiming = null;
                return;
            }
            var now = Date.now();
            if (!self.coreTiming) {
                self.coreTiming = {
                    at: now,
                    printTime: reportedTime,
                    printTimeLeft: Number(ko.unwrap(self.printerState.printTimeLeft))
                };
            }
            var expected = self.coreTiming.printTime + (now - self.coreTiming.at) / 1000;
            // A newer server value supersedes our local interpolation. A stale
            // value caused by the blocked command never moves the display back.
            if (reportedTime > expected + 0.5) {
                self.coreTiming.at = now;
                self.coreTiming.printTime = reportedTime;
                self.coreTiming.printTimeLeft = Number(ko.unwrap(self.printerState.printTimeLeft));
                expected = reportedTime;
            }
            self.printerState.printTime(expected);
            if (isFinite(self.coreTiming.printTimeLeft)) {
                self.printerState.printTimeLeft(Math.max(
                    0,
                    self.coreTiming.printTimeLeft - (now - self.coreTiming.at) / 1000
                ));
            }
        }
    }

    function formatDuration(seconds) {
        var minutes = Math.floor(seconds / 60);
        var remainder = seconds % 60;
        return minutes ? minutes + "m " + remainder + "s" : remainder + "s";
    }
    function formatDurationLong(value) {
        var seconds = Math.max(0, Math.floor(Number(value) || 0));
        var days = Math.floor(seconds / 86400);
        var hours = Math.floor((seconds % 86400) / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var remainder = seconds % 60;
        if (days) return days + "d " + hours + "h";
        if (hours) return hours + "h " + minutes + "m";
        if (minutes) return minutes + "m " + remainder + "s";
        return remainder + "s";
    }
    function formatDistance(value) {
        var meters = Math.max(0, Number(value) || 0);
        if (meters >= 1000) return (meters / 1000).toLocaleString(undefined, {maximumFractionDigits: 2}) + " km";
        if (meters >= 1) return meters.toLocaleString(undefined, {maximumFractionDigits: 1}) + " m";
        return (meters * 100).toLocaleString(undefined, {maximumFractionDigits: 1}) + " cm";
    }
    function makeLightProfile(key, label, deepIdle, idle, active, printing) {
        return {
            key: key, label: label,
            supported: ko.observable(true),
            deepIdle: ko.observable(deepIdle), idle: ko.observable(idle),
            active: ko.observable(active), printing: ko.observable(printing)
        };
    }
    function applyPackedBrightness(profile, packed) {
        var value = Number(packed) >>> 0;
        profile.deepIdle((value >>> 24) & 0xff);
        profile.idle((value >>> 16) & 0xff);
        profile.active((value >>> 8) & 0xff);
        profile.printing(value & 0xff);
    }
    function packBrightness(profile) {
        function byte(value) { return Math.max(0, Math.min(100, Number(value) || 0)); }
        return (
            byte(profile.deepIdle()) * 0x1000000 + byte(profile.idle()) * 0x10000 +
            byte(profile.active()) * 0x100 + byte(profile.printing())
        );
    }
    function formatBytes(bytes) {
        var value = Number(bytes || 0);
        if (value < 1024) return value + " B";
        if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KiB";
        return (value / (1024 * 1024)).toFixed(1) + " MiB";
    }
    function responseError(xhr) {
        var response = xhr && xhr.responseJSON;
        var message = response && (response.error || response.description || response.message);
        if (!message && xhr && xhr.responseText) {
            // Older OctoPrint/proxy error handlers may still return HTML.
            message = $("<div>").html(xhr.responseText).text().replace(/\s+/g, " ").trim();
        }
        if (!message) message = (xhr && xhr.statusText) || "Unknown error";
        return xhr && xhr.status ? "HTTP " + xhr.status + ": " + message : message;
    }
    function toHexColor(value) {
        if (typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value)) return value;
        var numeric = Math.max(0, Math.min(0xffffff, Number(value) || 0));
        return "#" + ("000000" + numeric.toString(16)).slice(-6);
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: RmeCompatibilityViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel", "printerStateViewModel", "filesViewModel"],
        elements: ["#navbar_plugin_rme_compatibility", "#tab_plugin_rme_compatibility", "#settings_plugin_rme_compatibility"]
    });
});
