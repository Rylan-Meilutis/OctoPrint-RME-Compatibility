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
        vm.rmePassiveTool = function (item) {
            var state = getState(), key = unwrap(item.key);
            if (!isIndx(state) || !/^tool\d+$/.test(key)) return false;
            var active = state.active_tool || {};
            return active.physical === null || active.physical === undefined ||
                key !== "tool" + active.physical || unavailable(state, key, unwrap(item.actual));
        };
        vm.rmeVisibleTools = function () {
            return unwrap(vm.tools).filter(function (item) { return !vm.rmePassiveTool(item); });
        };
        vm.rmeTemperatureText = function (item) {
            return vm.rmePassiveTool(item) ? "Parked / unavailable" : format(unwrap(item.actual));
        };
        var setTarget = vm.setTargetToValue;
        vm.setTargetToValue = function (item, value) {
            // Covers presets/autosend as well as the individual control.
            if (vm.rmePassiveTool(item)) return reject();
            var key = unwrap(item.key);
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
        return template.replace(/formatTemperature\(actual\(\)(?:, undefined, undefined, true)?\)/g,
            "$root.rmeTemperatureText($data)").replace(
            'submit: function(element)', 'visible: !$root.rmePassiveTool($data), submit: function(element)');
    }
    function installRows(document) {
        // Only change presentation collections; OctoPrint indexes its original
        // tools array when processing telemetry and must retain all eight slots.
        ["temperature", "tab_plugin_dashboard"].forEach(function (id) {
            var host = document.getElementById(id);
            if (!host) return;
            var walker = document.createTreeWalker(host, 128), node;
            while ((node = walker.nextNode())) {
                node.nodeValue = node.nodeValue.replace(/foreach:\s*temperatureModel\.tools\b(?!\()/g,
                    "foreach: temperatureModel.rmeVisibleTools()").replace(/foreach:\s*tools\b(?!\()/g,
                    "foreach: rmeVisibleTools()");
            }
        });
    }
    return {isIndx: isIndx, unavailable: unavailable, graphData: graphData, install: install, installRows: installRows};
});
