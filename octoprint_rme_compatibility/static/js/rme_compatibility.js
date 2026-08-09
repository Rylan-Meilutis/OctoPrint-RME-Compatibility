$(function () {
    function RmeCompatibilityViewModel(parameters) {
        var self = this;
        self.settings = parameters[0];
        self.loginState = parameters[1];
        self.printerState = parameters[2];
        self.state = ko.observable({
            connected: false,
            supported: false,
            session: {},
            machine: {},
            active_tool: {},
            loaded_filaments: [],
            stats: {},
            firmware: {},
            spoolmanager: {},
            firmware_files: []
        });
        self.tick = ko.observable(Date.now());
        self.coreTiming = null;
        self.pendingUpload = ko.observable(null);
        self.selectedFirmware = ko.observable();
        self.mappingRows = ko.observableArray([]);
        self.mappingEnabled = ko.observable(true);
        self.physicalTools = ko.observableArray([]);
        self.spoolSelectionRows = ko.observableArray([]);
        self.newSpoolTool = ko.observable(0);
        self.unlockPin = ko.observable("");
        self.lightScreen = ko.observable(100);
        self.lightChamber = ko.observable(100);
        self.lightStatus = ko.observable(100);
        self.themeKeys = [
            {key: "primary", label: "primary", value: ko.observable("#3366cc")},
            {key: "progress", label: "progress", value: ko.observable("#00aa55")},
            {key: "warning", label: "warning", value: ko.observable("#ffaa00")},
            {key: "error", label: "error", value: ko.observable("#dd2222")},
            {key: "image", label: "image", value: ko.observable("#101018")}
        ];
        self.filamentSlot = ko.observable(0);
        self.filamentName = ko.observable("PLAplus");
        self.filamentNozzle = ko.observable(215);
        self.filamentPreheat = ko.observable(170);
        self.filamentBed = ko.observable(60);
        self.filamentVisible = ko.observable(true);
        // Only reset this persistent form when a different firmware request
        // arrives; routine websocket snapshots must not erase in-progress edits.
        self.pendingSpoolKey = "";
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
            if (tool.logical === null || tool.logical === undefined) return "Active extruder not reported yet";
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
        self.stats = ko.pureComputed(function () { return self.state().stats || {}; });
        self.formatStat = function (key, value) {
            var number = Number(value);
            if (!Number.isFinite(number)) return String(value);
            if (/_m$/.test(key)) return number.toLocaleString() + " m";
            if (/_s$/.test(key)) {
                var seconds = Math.max(0, Math.floor(number));
                var hours = Math.floor(seconds / 3600);
                var minutes = Math.floor((seconds % 3600) / 60);
                var remainder = seconds % 60;
                return (hours ? hours + "h " : "") + (minutes ? minutes + "m " : "") + remainder + "s";
            }
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
        self.spoolLabel = function (spool) {
            var remaining = spool.remaining_weight;
            return (spool.alias ? spool.alias + " — " : "") + spool.display_name + " · " + spool.material +
                (remaining === null || remaining === undefined ? "" : " · " + Number(remaining).toFixed(0) + " g left");
        };

        self.firmwareFiles = ko.pureComputed(function () { return self.state().firmware_files || []; });
        self.firmwareLabel = function (file) { return file.name + " (" + formatBytes(file.size) + ")"; };
        self.firmware = ko.pureComputed(function () { return self.state().firmware || {}; });
        self.firmwareProgress = ko.pureComputed(function () { return Number(self.firmware().progress || 0) + "%"; });
        self.firmwareBusy = ko.pureComputed(function () {
            return ["starting", "uploading", "verifying"].indexOf(self.firmware().status) >= 0;
        });
        self.firmwareActive = ko.pureComputed(function () { return self.firmwareBusy() || self.firmware().status === "staged"; });
        self.firmwareError = ko.pureComputed(function () { return self.firmware().error || ""; });
        self.firmwareStatus = ko.pureComputed(function () {
            var fw = self.firmware();
            if (!fw.status || fw.status === "idle") return "No firmware staged.";
            if (fw.status === "staged") return "Verified on printer USB. Ready for explicit flash.";
            if (fw.status === "flashing") return "Bootloader handoff requested; the printer should reboot.";
            if (fw.status === "uploading") return "Sending " + formatBytes(fw.offset || 0) + " of " + formatBytes(fw.size || 0) + ".";
            return fw.status.charAt(0).toUpperCase() + fw.status.slice(1) + ".";
        });
        self.canStage = ko.pureComputed(function () {
            return !!self.selectedFirmware() && self.state().supported && !self.firmwareBusy();
        });
        self.canFlash = ko.pureComputed(function () { return self.firmware().status === "staged"; });

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
        self.acceptState = function (value) {
            if (!value) return;
            var oldPrompt = self.state().prompt || {};
            self.state(value);
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
                    if (value.theme[entry.key] !== undefined) entry.value(toHexColor(value.theme[entry.key]));
                });
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
            var selectionRows = [];
            for (var tool = 0; tool < machineTools; tool++) {
                selectionRows.push({
                    tool: tool,
                    selected: ko.observable(selectedByTool[tool] === undefined ? null : selectedByTool[tool])
                });
            }
            self.spoolSelectionRows(selectionRows);
            renderCoreWorkflow();
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
            var body = new FormData();
            body.append("file", file);
            $.ajax({
                url: PLUGIN_BASEURL + "rme_compatibility/firmware",
                type: "POST",
                data: body,
                processData: false,
                contentType: false
            }).done(function (response) {
                self.pendingUpload(null);
                self.selectedFirmware(response.file.name);
                self.acceptState(response.state);
                new PNotify({title: "Firmware stored on Pi", text: response.file.name, type: "success"});
            }).fail(function (xhr) {
                new PNotify({title: "Firmware upload failed", text: responseError(xhr), type: "error", hide: false});
            });
        };
        self.stageFirmware = function () { self.command("stage_firmware", {filename: self.selectedFirmware()}); };
        self.cancelFirmware = function () { self.command("cancel_firmware"); };
        self.flashFirmware = function () {
            if (window.confirm("Flash the verified firmware now? The printer will reboot and the bootloader will validate its signature and machine compatibility.")) {
                self.command("flash_firmware");
            }
        };
        self.applyMachineProfile = function () { self.command("apply_machine_profile"); };
        self.uiControl = function (action, value) { self.command("ui_control", {action: action, value: value}); };
        self.lockNow = function () { self.command("lock_now"); };
        self.unlock = function () { self.command("lock_unlock", {pin: self.unlockPin()}); };
        self.applyLights = function () {
            self.command("set_temp_lights", {
                screen: Number(self.lightScreen()), chamber: Number(self.lightChamber()), status: Number(self.lightStatus())
            });
        };
        self.applyTheme = function () {
            var colors = {};
            ko.utils.arrayForEach(self.themeKeys, function (entry) { colors[entry.key] = entry.value(); });
            self.command("set_theme", {colors: colors});
        };
        self.applyFilament = function () {
            self.command("set_filament", {
                slot: Number(self.filamentSlot()), name: self.filamentName(),
                nozzle: Number(self.filamentNozzle()), preheat: Number(self.filamentPreheat()),
                bed: Number(self.filamentBed()), visible: self.filamentVisible()
            });
        };
        self.syncSpoolmanager = function () { self.command("sync_spoolmanager"); };
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

        self.onBeforeBinding = function () {
            OctoPrint.simpleApiGet("rme_compatibility").done(self.acceptState);
            window.setInterval(function () {
                self.tick(Date.now());
                renderCoreWorkflow();
            }, 1000);
        };
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin === "rme_compatibility") self.acceptState(data);
        };

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
    function formatBytes(bytes) {
        var value = Number(bytes || 0);
        if (value < 1024) return value + " B";
        if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KiB";
        return (value / (1024 * 1024)).toFixed(1) + " MiB";
    }
    function responseError(xhr) {
        var response = xhr && xhr.responseJSON;
        return (response && (response.error || response.description || response.message)) || (xhr && xhr.statusText) || "Unknown error";
    }
    function toHexColor(value) {
        if (typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value)) return value;
        var numeric = Math.max(0, Math.min(0xffffff, Number(value) || 0));
        return "#" + ("000000" + numeric.toString(16)).slice(-6);
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: RmeCompatibilityViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel", "printerStateViewModel"],
        elements: ["#tab_plugin_rme_compatibility", "#settings_plugin_rme_compatibility"]
    });
});
