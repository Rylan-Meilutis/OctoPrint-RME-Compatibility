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
    $.fn.modal = function () { return this; };
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const ko = w.ko;
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    const sent = [];
    vm.command = (command, data) => { sent.push({command, data}); return $.Deferred().resolve().promise(); };
    let revision = 0;
    function snapshot(job, nativeState, mapping) {
        return {connected: true, supported: true, machine: {logical_tools: 8},
            _snapshot: {epoch: 'approval', revision: ++revision},
            prompt: {kind: 'toolmap', job_id: job, review_id: job === 'old' ? 100 : 1471396081,
                native_state: nativeState, count: 8, enabled: true,
                mapping, spooljoin_review: true,
                requirements: [{logical: 1, material: 'ASA'}, {logical: 2, material: 'ASA'}, {logical: 4, material: 'TPU'}]},
            loaded_filaments: ['HIPS', 'ASA', 'ASA', 'PETG', 'FLEX', 'PLA', 'PLA', 'PLA'].map((material, tool) =>
                ({tool, material, firmware_material: material, color: tool === 2 ? '#ffffff' : '#000000'}))};
    }
    const identity = Object.fromEntries(Array.from({length: 8}, (_, i) => [i, i]));
    const bad = {...identity, 1: 3, 3: 1};
    vm.acceptState(snapshot('old', 'editing', bad));
    assert.equal(vm.materialMappingValid(), false);
    // A new eight-tool job must not inherit the old incompatible draft.
    vm.acceptState(snapshot('new', 'loading', identity));
    assert.equal(vm.mappingRows()[1].physical(), 1);
    assert.equal(vm.materialMappingValid(), true); // TPU and FLEX agree.
    assert.equal(vm.canApplyToolmap(), false); // Native review not yet editable.
    vm.chooseMappingTool(vm.mappingRows()[1], vm.mappingChoices(vm.mappingRows()[1]).find(c => c.tool === 2));
    vm.acceptState(snapshot('new', 'editing', identity));
    assert.equal(vm.mappingRows()[1].physical(), 2); // Keep edits within this job.
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2', 'utf8');
    const start = template.indexOf('<button class="btn btn-primary" data-bind="click: applyToolmap');
    const host = w.document.createElement('div');
    host.innerHTML = template.slice(start, template.indexOf('</button>', start) + 9);
    w.document.body.append(host);
    ko.applyBindings(vm, host);
    assert.equal(host.querySelector('button').disabled, false);
    host.querySelector('button').click();
    assert.equal(sent.at(-1).command, 'apply_toolmap');
    assert.equal(sent.at(-1).data.review_id, 1471396081);
    assert.equal(sent.at(-1).data.mapping[1], 2);
    vm.acceptState(snapshot('new', 'submitted', identity));
    assert.equal(host.querySelector('button').disabled, true); // No double submission.
    vm.acceptState({...snapshot('new', 'complete', identity), prompt: null});
    assert.equal(vm.hasToolmapPrompt(), false);
    assert.equal(vm.canApplyToolmap(), false);
    console.log('Job-scoped web approval, native state and TPU/FLEX browser regression passed');
    dom.window.close();
})().catch(error => { console.error(error); process.exit(1); });
