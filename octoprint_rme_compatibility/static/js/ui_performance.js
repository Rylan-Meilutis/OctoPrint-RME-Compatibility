(function (root, factory) {
    var api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.RmeUiPerformance = api;
}(typeof window !== "undefined" ? window : this, function () {
    "use strict";

    function installStableProfileUpdates(profiles, ko) {
        var current = profiles && profiles.currentProfileData;
        if (!current || !ko.isObservable(current) || !ko.mapping || !ko.mapping.toJS) return false;
        if (current._rmeStableProfileUpdates) return true;
        var original = current.equalityComparer;
        // Do not replace another integration's custom equality semantics.
        if (original !== ko.observable.fn.equalityComparer) return false;
        current.equalityComparer = function (left, right) {
            if (!left || !right || typeof left !== "object" || typeof right !== "object") {
                return original.call(this, left, right);
            }
            try {
                // The mapping serializer excludes __ko_mapping__ bookkeeping,
                // which contains circular references in OctoPrint 1.11.x.
                // Compare the entire profile, not only its ID or tool count:
                // offsets, envelope, shared-nozzle and speed edits must notify.
                return JSON.stringify(ko.mapping.toJS(left)) === JSON.stringify(ko.mapping.toJS(right));
            } catch (error) {
                // Unexpected profile extensions must retain native behavior.
                return original.call(this, left, right);
            }
        };
        current._rmeStableProfileUpdates = true;
        return true;
    }

    return {installStableProfileUpdates: installStableProfileUpdates};
}));
