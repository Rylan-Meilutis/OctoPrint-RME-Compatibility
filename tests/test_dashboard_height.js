// NODE_PATH=<jsdom/jquery/knockout dependencies> node tests/test_dashboard_height.js
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<div id="tab_plugin_dashboard"></div>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko, printer = {filepath: ko.observable('fsr.gcode')};
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, printer, {}]);
    const dash = {totalHeight: ko.observable('975494.63'), currentHeight: ko.observable(8.2),
        heightProgressString: ko.observable(0), heightProgressBarString: ko.observable('0%'),
        getEta: ko.observable('untouched'), timeProgressString: ko.observable(42)};
    ko.applyBindings(dash, w.document.getElementById('tab_plugin_dashboard'));
    vm.installDashboardHeight();
    assert.equal(dash.totalHeight(), '975494.63'); // No changes on non-RME printers.
    vm.state({supported: true, machine: {z_max: 270}});
    assert.equal(dash.totalHeight(), '-');
    vm.objectPreview({file: 'fsr.gcode', height: 92});
    assert.equal(dash.totalHeight(), '92.00');
    assert.equal(dash.heightProgressBarString(), '9%');
    dash.totalHeight('975494.63'); // Later Dashboard messages cannot restore corrupt metadata.
    assert.equal(dash.totalHeight(), '92.00');
    dash.currentHeight(46);
    assert.equal(dash.heightProgressString(), 50);
    vm.installDashboardHeight(); // Idempotent.
    printer.filepath('next.gcode');
    assert.equal(dash.totalHeight(), '-'); // Never reuse the previous file's height.
    vm.objectPreview({file: 'fsr.gcode', height: 92});
    assert.equal(dash.totalHeight(), '-'); // Late result for previous file.
    dash.totalHeight('25');
    assert.equal(dash.totalHeight(), '25.00'); // Preserve sane native fallback.
    vm.objectPreview({file: 'next.gcode', height: 975494.63});
    assert.equal(dash.totalHeight(), '25.00');
    dash.totalHeight('Infinity');
    assert.equal(dash.totalHeight(), '-');
    assert.equal(dash.getEta(), 'untouched');
    assert.equal(dash.timeProgressString(), 42);
    dom.window.close();
    console.log('Dashboard height validation, file isolation, progress and time isolation passed');
})();
