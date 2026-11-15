const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');

(async function () {
    const dom = new JSDOM('<!doctype html><body></body>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.PNotify = function () {};
    w.OctoPrint = {getBlueprintUrl: () => '/plugin/rme_compatibility/'};
    w.OCTOPRINT_VIEWMODELS = [];
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    vm.acceptState({supported: true, connected: true, storage: {supported: true, path: '/', entries: [
        {path: '/jobs', name: 'jobs', type: 'dir', size: 0},
        {path: '/part.gcode', name: 'part.gcode', type: 'file', size: 100}
    ]}});
    const panel = $(fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_storage_controls.jinja2', 'utf8')).appendTo(w.document.body);
    w.ko.applyBindings(vm, panel[0]);
    assert.equal(panel.find('tbody:first tr').length, 2);
    vm.storageAllSelected(true);
    assert.equal(vm.storageSelectedEntries().length, 2);
    vm.storageFilter('part');
    assert.equal(panel.find('tbody:first tr').length, 1);
    vm.storageAllSelected(true);
    assert.equal(vm.storageSelectedEntries().length, 1);
    const calls = [];
    vm.command = (name, data) => { calls.push({name, data}); return $.Deferred().resolve().promise(); };
    w.confirm = () => false;
    vm.storageDeleteSelected();
    assert.equal(calls.length, 0);
    let confirmation;
    w.confirm = text => { confirmation = text; return true; };
    vm.storageFilter('');
    vm.storageAllSelected(true);
    vm.storageDeleteSelected();
    assert.match(confirmation, /ALL CONTENTS/);
    assert.equal(calls[0].name, 'storage_delete_many');
    assert.equal(calls[0].data.recursive, true);
    assert.equal(calls[0].data.paths.length, 2);
    assert.equal(vm.storageBatchBusy(), false);
    assert.equal(vm.storageSelectedPaths().length, 0);
    vm.storageAllSelected(true);
    w.prompt = () => '/archive';
    vm.storageMoveSelected();
    assert.equal(calls[1].data.destination, '/archive/jobs');
    assert.equal(calls[2].data.destination, '/archive/part.gcode');
    assert.equal(vm.storageBatchBusy(), false);
    vm.storageAllSelected(true);
    vm.acceptState({supported: true, storage: {supported: true, path: '/jobs', entries: []}});
    assert.equal(vm.storageSelectedPaths().length, 0);
    console.log('Storage browser selection, binding, confirmation and bulk-action tests passed');
    dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
