// Run with NODE_PATH pointing at temporary jsdom/jquery/knockout dependencies.
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
    w.PNotify = function () {};
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko;
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    vm.command = () => $.Deferred().resolve().promise();
    const linked = {material: 'PLA', profile: 'PLA-00D', firmware_profile: 'PLA-00D', color: '#808080'};
    assert.equal(vm.loadedFilamentLabel(linked), 'PLA');
    assert.equal(linked.profile, 'PLA-00D'); // Internal identity is untouched.
    assert.equal(vm.loadedFilamentLabel({profile: 'PLA', material: 'PLA'}), 'PLA');
    assert.equal(vm.loadedFilamentLabel({profile: 'PLA-00D', material: 'PLA-00D'}), 'Unassigned');
    assert(!vm.spoolLabel({alias: 'PLA-00D', material: 'PLA', display_name: 'PLA-00D'}).includes('PLA-00D'));
    const state = {supported: true, prompt: {kind: 'toolmap', requirements: [
        {logical: 0, material: 'PLA', color: '#ffffff'}], recommendation: {0: 1, 1: 0, 2: 2}},
        loaded_filaments: [{tool: 0, material: 'PLA', color: '#000000'},
            {tool: 1, material: 'PLA', color: '#ffffff'}, {tool: 2, material: 'PETG'}]};
    vm.state(state);
    vm.physicalTools([0, 1, 2]);
    vm.mappingRows([0, 1, 2].map(i => ({logical: i, physical: ko.observable(i)})));
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2', 'utf8');
    const controls = w.document.createElement('div');
    controls.innerHTML = template.slice(template.indexOf('<section id="rme-print-controls"'), template.indexOf('</section>') + 10);
    w.document.body.append(controls);
    ko.applyBindings(vm, controls);
    state.connected = true;
    state.machine = {tune: 1};
    state.tune = {lcd: 1, light: 1, screen_print: 80, chamber_print: 50, status_print: 20, updated: Date.now() / 1000};
    vm.state.valueHasMutated();
    const sent = [];
    vm.command = (command, data) => { sent.push({command, data}); return $.Deferred().resolve().promise(); };
    const lcd = controls.querySelector('input[aria-label="LCD Off or On"]');
    lcd.value = '0';
    lcd.dispatchEvent(new w.Event('change', {bubbles: true}));
    assert.equal(sent[0].data.kind, 'lcd');
    assert.equal(sent[0].data.value, 0);
    assert.equal(vm.tuneLight(), 1); // LCD never changes the chamber selector.
    assert.equal(lcd.value, '1'); // Wait for firmware acknowledgement.
    state.tune.lcd = 0;
    vm.state.valueHasMutated();
    assert.equal(lcd.value, '0');
    const start = template.indexOf('<div class="rme-routing-cards"');
    const end = template.indexOf('<label class="checkbox"><input type="checkbox" data-bind="checked: mappingEnabled">', start);
    const host = w.document.createElement('div');
    host.innerHTML = template.slice(start, end);
    w.document.body.append(host);
    ko.applyBindings(vm, host);
    assert.equal(host.querySelectorAll('.rme-tool-choice').length, 3);
    assert.equal(host.querySelectorAll('.rme-tool-choice:disabled').length, 1);
    host.querySelectorAll('.rme-tool-choice')[1].click();
    assert.equal(vm.mappingRows()[0].physical(), 1);
    assert.equal(vm.mappingRows()[1].physical(), 0);
    assert(vm.materialMappingValid());
    state.loaded_filaments[1].material = 'ASA';
    vm.state.valueHasMutated();
    assert(!vm.materialMappingValid());
    $.fn.modal = function () { return this; };
    vm.spoolSelectionRows([{tool: 0, loaded: {material: 'PLA', color: '#ffffff'},
        selected: ko.observable(null), dirty: ko.observable(false),
        availableSpools: ko.observableArray([{database_id: 1, material: 'PLA', display_name: 'White PLA'}])}]);
    vm.showFilamentMapping();
    assert(w.document.querySelector('#rme-spool-mapping-dialog .rme-tool-choice'));
    w.document.querySelector('#rme-spool-mapping-dialog .rme-tool-choice').click();
    assert.equal(vm.spoolSelectionRows()[0].selected(), 1);
    console.log('Graphical tool/spool mapping DOM bindings and interaction passed');
    dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
