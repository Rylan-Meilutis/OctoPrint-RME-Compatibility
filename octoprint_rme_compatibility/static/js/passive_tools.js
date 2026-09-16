(function (root, factory) {
    var api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.RmePassiveTools = api;
})(typeof window !== "undefined" ? window : this, function () {
    "use strict";
    function isIndx(state) {
        var machine = state.machine || {};
        return !!state.supported && Number(machine.hotends) === 1 && Number(machine.tool_capacity) === 8;
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
            return unavailable(getState(), unwrap(item.key), unwrap(item.actual));
        };
        vm.rmeTemperatureText = function (item) {
            return vm.rmePassiveTool(item) ? "Parked / unavailable" : format(unwrap(item.actual));
        };
        var setTarget = vm.setTargetToValue;
        vm.setTargetToValue = function (item, value) {
            // Also covers presets/autosend; never preheat a passive parked tool.
            // Allow Off so a previously stored target can still be cleared.
            if (vm.rmePassiveTool(item) && Number(value) !== 0) return reject();
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
    return {isIndx: isIndx, unavailable: unavailable, graphData: graphData, install: install};
});
