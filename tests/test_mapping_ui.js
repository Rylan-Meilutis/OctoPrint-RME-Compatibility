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
    const state = {supported: true, prompt: {kind: 'toolmap', requirements: [
        {logical: 0, material: 'PLA', color: '#ffffff'}], recommendation: {0: 1, 1: 0, 2: 2}},
        loaded_filaments: [{tool: 0, material: 'PLA', color: '#000000'},
            {tool: 1, material: 'PLA', color: '#ffffff'}, {tool: 2, material: 'PETG'}]};
    vm.state(state);
    vm.physicalTools([0, 1, 2]);
    vm.mappingRows([0, 1, 2].map(i => ({logical: i, physical: ko.observable(i)})));
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2', 'utf8');
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
