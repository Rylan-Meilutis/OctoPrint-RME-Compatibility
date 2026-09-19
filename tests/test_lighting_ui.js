// NODE_PATH may point at the shared jsdom/jquery/knockout test dependencies.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<!doctype html><html><body></body></html>', {runScripts: 'outside-only'});
    const w = dom.window;
    const $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.gettext = x => x;
    const notices = [];
    w.PNotify = function (notice) { notices.push(notice); };
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {isPrinting: w.ko.observable(true), stateString: w.ko.observable('Printing')}, {}]);
    const state = {connected: true, supported: true, machine: {tune: 1},
        tune: {light: 1, lcd: 1, updated: 1}, lock: {locked: false}};
    vm.state(state);
    vm.tick(Date.now());
    assert.equal(vm.chamberLightPrinterBusy(), true);
    const commands = [];
    vm.command = (name, data) => commands.push([name, data]);
    assert.equal(vm.tuneEnabled(), false); // Old telemetry still blocks motion tuning.
    assert.equal(vm.chamberLightDisabled(), false);
    assert.equal(vm.chamberLightOn(), true);
    assert(vm.chamberLightTitle().includes('last reported'));
    vm.pressChamberLightButton();
    assert.equal(commands[0][0], 'set_print_override');
    assert.equal(commands[0][1].value, 0);
    // Immediate feedback survives old telemetry until acknowledgement/timeout.
    assert.equal(vm.chamberLightOn(), false);
    vm.state({...state});
    assert.equal(vm.chamberLightOn(), false);
    vm.state({...state, tune: {light: 0, updated: Date.now() / 1000}});
    assert.equal(vm.chamberLightOn(), false);
    vm.pressChamberLightButton();
    assert.equal(commands[1][1].value, 1);
    vm.state({...state, tune: {light: 2, updated: Date.now() / 1000}});
    assert.equal(vm.chamberLightHeld(), false); // Printing never exposes Locked.
    assert.equal(vm.tuneLight(), 1);
    vm.state({...state, lock: {locked: true}});
    assert.equal(vm.chamberLightDisabled(), true);
    vm.pressChamberLightButton();
    assert.equal(commands.length, 2);
    vm.state({...state, tune: {}});
    vm.pendingLight(null);
    assert.equal(vm.chamberLightDisabled(), true);
    vm.state({...state, connected: false});
    assert.equal(vm.chamberLightDisabled(), true);
    const navbar = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_light_navbar.jinja2', 'utf8');
    assert(navbar.includes("'aria-pressed': chamberLightOn"));
    const beforeNotice = vm.state();
    vm.onDataUpdaterPluginMessage('rme_compatibility', {
        retry_notice: {message: 'Restart requested', error: false}
    });
    assert.equal(vm.state(), beforeNotice); // Partial notice is not a state snapshot.
    assert.equal(notices.at(-1).type, 'success');
    vm.onDataUpdaterPluginMessage('rme_compatibility', {
        retry_notice: {message: 'Selected file changed', error: true}
    });
    assert.equal(notices.at(-1).type, 'error');
    vm.state({...state, tune: {light: 1}});
    vm.command = () => ({fail: callback => callback()});
    vm.requestChamberLight(0);
    assert.equal(vm.chamberLightOn(), true); // Failed send rolls back immediately.
    vm.command = () => ({});
    vm.requestChamberLight(0);
    assert.equal(vm.chamberLightOn(), false);
    vm.tick(Date.now() + 6000);
    assert.equal(vm.chamberLightOn(), true); // Missing confirmation cannot stick.
    vm.pendingLight(null);
    vm.printerState.isPrinting(false);
    vm.printerState.stateString('Operational');
    vm.state({...state, tune: {light: 2, printing: 0}});
    assert.equal(vm.chamberLightPrinterBusy(), false);
    assert.equal(vm.chamberLightHeld(), true);
    vm.state({...state, tune: {light: 2, printing: 1}});
    assert.equal(vm.chamberLightPrinterBusy(), true); // Printer-local job.
    assert.equal(vm.chamberLightHeld(), false);
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2', 'utf8');
    assert(template.includes('max: chamberLightPrinterBusy() ? 1 : 2'));
    dom.window.close();
    console.log('In-print lighting UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
