// NODE_PATH=/tmp/rme-ui-deps/node_modules node tests/test_new_spool_ui.js
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<body><div id="tab_spoolOverview"></div><div id="spool-dialog"></div></body>', {runScripts: 'outside-only'});
    const w = dom.window;
    const $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.PNotify = function () {};
    w.gettext = x => x;
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko;
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    vm.settings.settings = {plugins: {rme_compatibility: {}}};
    vm.showSpoolManagerTab = function () {};
    const item = {};
    for (const field of ['databaseId', 'displayName', 'vendor', 'material', 'temperature', 'bedTemperature',
        'color', 'colorName', 'totalWeight', 'totalCombinedWeight', 'usedWeight', 'selectedForTool', 'noteHtml']) {
        item[field] = ko.observable(null);
    }
    const original = function () { throw new Error('Native save must not handle RME creation'); };
    const api = {callSaveSpool: original};
    const dialog = {apiClient: api, spoolDialog: $('#spool-dialog'), spoolItemForEditing: item};
    const manager = {spoolDialog: dialog, addNewSpool: () => item.databaseId(null)};
    ko.applyBindings(manager, w.document.getElementById('tab_spoolOverview'));
    const requests = [];
    vm.command = (command, data) => {
        const deferred = $.Deferred();
        requests.push({command, data, deferred});
        return deferred.promise();
    };
    let closes = 0;
    const close = () => { closes++; };
    vm.createSpoolInSpoolManager({tool: 0, loaded: {material: 'PLA'}});
    assert.equal(item.selectedForTool(), 0);
    item.noteHtml('<b>Preserve native metadata</b>');
    api.callSaveSpool(item, close);
    api.callSaveSpool(item, close); // Double-click cannot create duplicates.
    assert.equal(requests.length, 1);
    assert.equal(requests[0].command, 'create_native_spool');
    assert.equal(requests[0].data.spool.noteHtml, '<b>Preserve native metadata</b>');
    requests[0].deferred.reject();
    assert.equal(closes, 0); // Failed save leaves the form open.
    api.callSaveSpool(item, close);
    requests[1].deferred.resolve({created_spool: {database_id: 712}});
    assert.equal(requests[2].command, 'select_spool');
    assert.equal(requests[2].data.tool, 0);
    assert.equal(requests[2].data.database_id, 712);
    requests[2].deferred.reject();
    assert.equal(closes, 0);
    api.callSaveSpool(item, close);
    assert.equal(requests[3].command, 'select_spool'); // Retry uses saved ID, never creates a second spool.
    requests[3].deferred.resolve();
    assert.equal(requests[4].command, 'refresh_spool_inventory');
    requests[4].deferred.resolve();
    assert.equal(closes, 1);
    assert.equal(api.callSaveSpool, original);
    vm.createSpoolInSpoolManager({tool: 4});
    $('#spool-dialog').trigger('hidden'); // Cancel must discard slot association.
    assert.equal(api.callSaveSpool, original);
    assert.equal(requests.length, 5);
    vm.createSpoolInSpoolManager({tool: 6});
    api.callSaveSpool(item, close);
    requests[5].deferred.resolve({created_spool: {database_id: 713}});
    assert.equal(requests[6].data.tool, 6);
    assert.equal(requests[6].data.database_id, 713);
    requests[6].deferred.resolve();
    assert.equal(closes, 2);
    assert.equal(api.callSaveSpool, original);
    vm.acceptState({machine: {logical_tools: 2}, loaded_filaments: [{tool: 0, material: 'PLA'}],
        spoolmanager: {inventory: [{database_id: 712, material: 'PLA'}], selected: [{tool: 0, database_id: 1}]}});
    let row = vm.spoolSelectionRows()[0];
    row.selected(712);
    vm.changeSpoolSelection(row);
    vm.acceptState({machine: {logical_tools: 2}, loaded_filaments: [{tool: 0, material: 'PLA', color: '#ffffff'}],
        spoolmanager: {inventory: [{database_id: 712, material: 'PLA'}, {database_id: 713, material: 'PLA'}],
            selected: [{tool: 0, database_id: 1}, {tool: 1, database_id: 713}]}});
    row = vm.spoolSelectionRows()[0];
    assert.equal(row.selected(), 712); // Unrelated updates must not discard a staged selection.
    assert.equal(row.dirty(), true);
    assert.equal(row.availableSpools().length, 2); // New inventory is visible without a reload.
    dom.window.close();
    console.log('Native New Spool assignment UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
