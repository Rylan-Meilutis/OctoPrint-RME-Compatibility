const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<!doctype html><html><body></body></html>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.gettext = x => x;
    w.PNotify = function () {};
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {isPrinting: w.ko.observable(true), stateString: w.ko.observable('Printing')}, {}]);
    const commands = [];
    vm.command = (name, data) => commands.push([name, data]);
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_settings.jinja2', 'utf8');
    const start = template.indexOf('<!-- ko if: tune().pellet_ejection_entry_only');
    w.document.body.innerHTML = template.slice(start, template.indexOf('<!-- /ko -->', start) + '<!-- /ko -->'.length);
    w.ko.applyBindings(vm, w.document.body);
    const state = {connected: true, supported: true, machine: {tune: 1}, lock: {locked: false}, tune: {updated: Date.now()/1000}};
    vm.state(state); vm.tick(Date.now()); w.ko.tasks.runEarly();
    assert.equal(w.document.querySelector('select'), null); // unsupported/non-INDX
    for (const value of [0, 1, 0]) {
        vm.state({...state, tune: {...state.tune, pellet_ejection_entry_only: value}});
        w.ko.tasks.runEarly();
        const select = w.document.querySelector('select');
        assert(select && !select.disabled);
        assert.equal(select.value, String(value)); // zero is a supported default
        select.value = String(1-value);
        select.dispatchEvent(new w.Event('change', {bubbles: true}));
        const [name, data] = commands.at(-1);
        assert.equal(name, 'set_print_override');
        assert.equal(data.kind, 'pellet_ejection_entry_only');
        assert.equal(data.value, 1-value);
        assert.equal(select.value, String(value)); // don't claim success before readback
    }
    vm.state({...state, tune: {...state.tune, pellet_ejection_entry_only: 0}, lock: {locked: true}});
    w.ko.tasks.runEarly(); assert(w.document.querySelector('select').disabled);
    vm.state(state); w.ko.tasks.runEarly();
    assert.equal(w.document.querySelector('select'), null);
    assert(w.document.body.textContent.indexOf('purge amounts') === -1); // hidden with unsupported field
    for (const phase of ['open', 'active']) {
        vm.state({...state, workflow: {workflow: 'chamber_temperature', state: phase, code: 'temperature_wait', message: 'Waiting for chamber: 23/50 C'}});
        assert(vm.workflowVisible());
        assert.equal(vm.workflowTitle(), 'Waiting for chamber temperature');
        assert.equal(vm.workflowMessage(), 'Waiting for chamber: 23/50 C');
    }
    for (const phase of ['closed', 'skipped', 'cancelled']) {
        vm.state({...state, workflow: {workflow: 'chamber_temperature', state: phase}});
        assert(!vm.workflowVisible());
    }
    vm.state({...state, workflow: {workflow: 'printer', type: 'notification',
        state: 'paused', message: 'Retracting filament', legacy_expires_at: Date.now()/1000 + 15}});
    assert(vm.workflowVisible());
    vm.state({...state, workflow: {workflow: 'printer', type: 'notification',
        state: 'paused', message: 'Retracting filament', legacy_expires_at: Date.now()/1000 - 1}});
    assert(!vm.workflowVisible());
    w.close();
    console.log('Pellet-ejection settings browser tests passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
