const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<html><body></body></html>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.gettext = x => x;
    w.PNotify = function () {};
    $.fn.modal = function () { return this; };
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_storage_controls.jinja2', 'utf8');
    const start = template.indexOf('<table class="table table-condensed rme-storage-table"');
    const host = w.document.createElement('div');
    host.innerHTML = '<!-- ko if: settingsStorageExpanded -->' + template.slice(start, template.indexOf('</table>', start) + 8) + '<!-- /ko -->';
    w.document.body.append(host);
    w.ko.applyBindings(vm, host);
    const state = {connected: true, supported: true, storage: {supported: true, entries:
        Array.from({length: 300}, (_, i) => ({name: 'part' + i + '.gcode', type: 'file', size: 1000}))}};
    vm.acceptState(JSON.parse(JSON.stringify(state)));
    assert.equal(host.querySelectorAll('tbody tr').length, 0, 'hidden Settings storage must not render files');
    vm.settingsStorageExpanded(true);
    const first = host.querySelector('tbody tr');
    first.querySelector('button').focus();
    const began = performance.now();
    for (let i = 0; i < 5; i++) vm.acceptState(JSON.parse(JSON.stringify(state)));
    console.log('Five unchanged 300-file snapshots: ' + Math.round(performance.now() - began) + 'ms');
    assert(host.querySelector('tbody tr') === first, 'unchanged polls must retain DOM rows and focus');
    assert(w.document.activeElement === first.querySelector('button'));
    state.storage.entries[299].size = 2000;
    vm.acceptState(JSON.parse(JSON.stringify(state)));
    assert(host.querySelector('tbody tr') === first, 'changing one file must preserve other rows');
    assert(host.textContent.includes('2.0 KiB'));
    vm.settingsStorageExpanded(false);
    assert.equal(host.querySelectorAll('tbody tr').length, 0);
    vm.settingsStorageExpanded(true);
    assert(host.textContent.includes('2.0 KiB'), 'reopening shows current files');
    const settings = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_settings.jinja2', 'utf8');
    const workspace = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2', 'utf8');
    assert(!settings.includes('rme-storage-table'));
    assert(!settings.includes('include "rme_compatibility_firmware_controls.jinja2"'));
    assert(settings.includes('<!-- ko if: settingsPrinterExpanded -->'));
    assert(workspace.includes('<!-- ko if: settingsStorageExpanded -->'));
    // A burst accumulated while the browser is busy is rendered once, using
    // the latest snapshot, not replayed as dozens of full-page updates.
    const frames = [];
    w.requestAnimationFrame = callback => frames.push(callback);
    let renders = 0;
    const accept = vm.acceptState;
    vm.acceptState = value => { renders++; accept(value); };
    for (let i = 0; i < 100; i++) vm.onDataUpdaterPluginMessage('rme_compatibility', {connected: true, firmware: {offset: i}});
    assert.equal(frames.length, 1);
    assert.equal(renders, 0);
    frames.shift()();
    assert.equal(renders, 1);
    assert.equal(vm.state().firmware.offset, 99);
    vm.onDataUpdaterPluginMessage('rme_compatibility', {firmware: {offset: 100}});
    vm.acceptState({firmware: {offset: 200}}); // Immediate operator response wins.
    frames.shift()();
    assert.equal(vm.state().firmware.offset, 200);
    // Unchanged nested objects still notify by default in Knockout. With a
    // real inventory this repeatedly filters all spools for all eight tools,
    // even when only progress or provider status changed.
    const inventoryState = {connected: true, supported: true, machine: {logical_tools: 8},
        loaded_filaments: Array.from({length: 8}, (_, tool) => ({tool, material: 'PLA'})),
        spoolmanager: {provider: 'spoolmanager', inventory: Array.from({length: 1000}, (_, i) =>
            ({database_id: i + 1, material: 'PLA', display_name: 'Spool ' + i}))}};
    vm.acceptState(JSON.parse(JSON.stringify(inventoryState)));
    let choiceUpdates = 0;
    const subscriptions = vm.spoolSelectionRows().map(row => row.availableSpools.subscribe(() => choiceUpdates++));
    for (let i = 0; i < 100; i++) {
        inventoryState.firmware = {offset: i};
        inventoryState.spoolmanager.status = 'Poll ' + i;
        vm.acceptState(JSON.parse(JSON.stringify(inventoryState)));
    }
    assert.equal(choiceUpdates, 0, 'telemetry and provider status must not rebuild all spool choices');
    inventoryState.spoolmanager.inventory.push({database_id: 1001, material: 'PLA'});
    vm.acceptState(JSON.parse(JSON.stringify(inventoryState)));
    assert.equal(choiceUpdates, 8, 'a real inventory change refreshes every affected tool');
    subscriptions.forEach(subscription => subscription.dispose());
    let created = 0, removed = 0;
    w.PNotify = function () { created++; this.remove = () => removed++; };
    for (let i = 0; i < 100; i++) vm.updateFirmwareRecoveryNotice({type: 'error',
        workflow: 'printer', state: 'waiting', message: 'Check filament', seq: i, received_at: i}, {});
    assert.equal(created, 1, 'repeated event IDs must not recreate the same notice');
    vm.updateFirmwareRecoveryNotice({}, {});
    assert.equal(removed, 1);
    console.log('Settings list identity and updates passed');
    w.close();
})();
