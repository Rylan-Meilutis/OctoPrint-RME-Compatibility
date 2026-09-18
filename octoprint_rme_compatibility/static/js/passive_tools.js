(function (root, factory) {
    var api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.RmePassiveTools = api;
})(typeof window !== "undefined" ? window : this, function () {
    "use strict";
    function isIndx(state) {
        var machine = state.machine || {};
        // INDX reports eight physical tool slots, not one sensing/heating head.
        return !!state.supported && Number(machine.tool_capacity) === 8 &&
            [1, 8].indexOf(Number(machine.hotends)) !== -1;
    }
    function unavailable(state, key, actual) {
        return isIndx(state) && /^tool\d+$/.test(key) &&
            (actual === null || actual === undefined || !Number.isFinite(Number(actual)) || Number(actual) < 0);
    }
    function graphData(state, data) {
        if (!isIndx(state)) return data;
        return data.map(function (sample) {
            var copy = Object.assign({}, sample);
            Object.keys(copy).forEach(function (key) {
                if (copy[key] && unavailable(state, key, copy[key].actual)) {
                    copy[key] = Object.assign({}, copy[key], {actual: null, target: null});
                }
            });
            return copy;
        });
    }
    function install(vm, getState, unwrap, template, format, reject) {
        if (vm.rmePassiveTool) return template;
        var unloaded = vm._createToolEntry ? vm._createToolEntry() : null;
        if (unloaded) { unloaded.key("tool0"); unloaded.name("Tool"); }
        function displayedTool() {
            var state = getState(), tools = unwrap(vm.tools);
            // INDX exposes a readable sensor only on its mounted head. A full
            // physical snapshot can precede the next RME session response.
            // Never infer this from an incomplete/shared-nozzle profile.
            var complete = tools.length === 8 && tools.every(function (item, index) {
                return unwrap(item.key) === "tool" + index;
            });
            if (complete) {
                var readable = tools.filter(function (item) {
                    return !unavailable(state, unwrap(item.key), unwrap(item.actual));
                });
                if (readable.length === 1) return readable[0];
                if (!readable.length) return null;
            }
            var physical = (state.active_tool || {}).physical;
            return tools.find(function (item) { return unwrap(item.key) === "tool" + physical; });
        }
        vm.rmePassiveTool = function (item) {
            var state = getState(), key = unwrap(item.key);
            if (!isIndx(state) || !/^tool\d+$/.test(key)) return false;
            var selected = displayedTool();
            return !selected || key !== unwrap(selected.key) || unavailable(state, key, unwrap(item.actual));
        };
        vm.rmeVisibleTools = function () {
            var tools = unwrap(vm.tools);
            if (!isIndx(getState())) return tools;
            var active = displayedTool();
            // Keep an explicit unloaded row instead of removing the heater UI.
            return active ? [active] : (tools.length ? tools.slice(0, 1) : (unloaded ? [unloaded] : []));
        };
        vm.rmeToolName = function (item) {
            return isIndx(getState()) && /^tool\d+$/.test(unwrap(item.key)) ?
                (vm.rmePassiveTool(item) ? "Tool" : "Tool T" + unwrap(item.key).slice(4)) : unwrap(item.name);
        };
        vm.rmeTemperatureText = function (item) {
            return vm.rmePassiveTool(item) ? "Unloaded" : format(unwrap(item.actual));
        };
        vm.rmeGaugesCentreInGrid = function (dashboard, type, index, css) {
            if (!isIndx(getState())) return dashboard.gaugesCentreInGrid(type, index, css);
            var tools = vm.tools, visible = vm.rmeVisibleTools();
            // Dashboard closes over its temperature model. Give its layout
            // calculation the displayed count, never mutate telemetry storage.
            vm.tools = function () { return visible; };
            try { return dashboard.gaugesCentreInGrid(type, index, css); }
            finally { vm.tools = tools; }
        };
        var setTarget = vm.setTargetToValue;
        vm.setTargetToValue = function (item, value) {
            // Covers presets/autosend as well as the individual control.
            if (vm.rmePassiveTool(item)) return reject();
            var key = unwrap(item.key);
            // Display may lead the session snapshot; do not heat a head until
            // the authoritative selection agrees with that displayed sensor.
            if (isIndx(getState()) && /^tool\d+$/.test(key) &&
                key !== "tool" + (getState().active_tool || {}).physical) return reject();
            if (isIndx(getState()) && (key === "chamber" || /^tool\d+$/.test(key))) {
                if (String(value).trim() === "" || !Number.isInteger(Number(value)) ||
                    Number(value) < 0 || Number(value) > 999) return reject();
                vm.clearAutosendTarget(item);
                // Physical temperature rows must not be reinterpreted as logical
                // T indices by firmware tool mapping. Address the mounted head.
                // M141 also works with older OctoPrint profiles lacking heatedChamber.
                return vm.rmeSendTemperature((key === "chamber" ? "M141 S" : "M104 S") + Number(value))
                    .done(function () { item.newTarget(""); });
            }
            return setTarget.apply(this, arguments);
        };
        var process = vm._processTemperatureData;
        vm._processTemperatureData = function (serverTime, data, result) {
            return process.call(this, serverTime, graphData(getState(), data), result);
        };
        var getPlotInfo = vm._getPlotInfo;
        if (getPlotInfo) vm._getPlotInfo = function () {
            if (!isIndx(getState())) return getPlotInfo.apply(this, arguments);
            var options = vm.heaterOptions, temperatures = vm.temperatures;
            var selected = vm.rmeVisibleTools()[0];
            var key = selected && unwrap(selected.key);
            var visible = {};
            Object.keys(unwrap(options) || {}).forEach(function (type) {
                if (!/^tool\d+$/.test(type) || type === key) visible[type] = unwrap(options)[type];
            });
            if (selected && visible[key]) {
                visible[key] = Object.assign({}, visible[key], {name: vm.rmeToolName(selected)});
                if (vm.rmePassiveTool(selected)) {
                    visible[key].name = "Tool (Unloaded)";
                    vm.temperatures = Object.assign({}, temperatures);
                    vm.temperatures[key] = {actual: [], target: []};
                }
            }
            // Filter only the plot's read, never the telemetry-indexed tools or
            // persistent heater options/history. Restore even if plotting fails.
            vm.heaterOptions = function () { return visible; };
            try { return getPlotInfo.apply(this, arguments); }
            finally { vm.heaterOptions = options; vm.temperatures = temperatures; }
        };
        return template.replace(/formatTemperature\(actual\(\)(?:, undefined, undefined, true)?\)/g,
            "$root.rmeTemperatureText($data)").replace(
            'submit: function(element)', 'visible: !$root.rmePassiveTool($data), submit: function(element)').replace(
            'text: name, attr: {title: name}', 'text: $root.rmeToolName($data), attr: {title: $root.rmeToolName($data)}');
    }
    function installRows(document) {
        // Only change presentation collections; OctoPrint indexes its original
        // tools array when processing telemetry and must retain all eight slots.
        ["temperature", "temp", "tab_plugin_dashboard"].forEach(function (id) {
            var host = document.getElementById(id);
            if (!host) return;
            var walker = document.createTreeWalker(host, 128), node;
            while ((node = walker.nextNode())) {
                node.nodeValue = node.nodeValue.replace(/foreach:\s*temperatureModel\.tools\b(?!\()/g,
                    "foreach: temperatureModel.rmeVisibleTools()").replace(/foreach:\s*tools\b(?!\()/g,
                    "foreach: rmeVisibleTools()");
            }
            if (id === "tab_plugin_dashboard") {
                host.querySelectorAll("[data-bind]").forEach(function (element) {
                    var binding = element.getAttribute("data-bind");
                    binding = binding.replace(/(\$parent\.)?gaugesCentreInGrid\(/g, function (_, parent) {
                        return parent ? "$parent.temperatureModel.rmeGaugesCentreInGrid($parent, " :
                            "temperatureModel.rmeGaugesCentreInGrid($data, ";
                    });
                    binding = binding.replace("text: Math.round($parent.convertTemp(actual())) + $parent.tempSymbol()",
                        "text: $parent.temperatureModel.rmePassiveTool($data) ? 'Unloaded' : Math.round($parent.convertTemp(actual())) + $parent.tempSymbol()");
                    if (element.matches("text.dashboardGauge")) {
                        binding = binding.replace("text: name()", "text: $parent.temperatureModel.rmeToolName($data)");
                    }
                    element.setAttribute("data-bind", binding);
                });
                host.querySelectorAll(".dashboard_threeQuarterGauge").forEach(function (gauge) {
                    if (gauge.parentElement.classList.contains("dashboardGridContainer")) {
                        gauge.parentElement.classList.add("rme-dashboard-heaters");
                    }
                });
            }
        });
    }
    return {isIndx: isIndx, unavailable: unavailable, graphData: graphData, install: install, installRows: installRows};
});
