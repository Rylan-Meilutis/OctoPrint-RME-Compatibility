const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<!doctype html><body></body>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    w.BASEURL = '/';
    w.confirm = () => true;
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    vm.printerState.filepath = w.ko.observable('parts.gcode');
    const preview = {file: 'parts.gcode', job_id: 'job1', active: true, filepos: 0,
        objects: [{name: 'A', segments: [], last_print_end: 100, safety_reason: null, cancellable: true},
            {name: 'B', segments: [], last_print_end: 200, safety_reason: null, cancellable: true}]};
    vm.objectPreview(preview);
    vm.cancelObjects([{id: 0, object: 'A', cancelled: false}, {id: 1, object: 'B', cancelled: false}]);
    assert(vm.objectList().every(row => row.cancellable));
    let sent = [];
    vm.command = (name, data) => { sent.push({name, data}); return $.Deferred().promise(); };
    $.getJSON = () => $.Deferred().resolve(preview).promise();
    vm.cancelOverviewObject(vm.objectList()[0]);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].name, 'cancel_object');
    assert.equal(sent[0].data.job_id, 'job1');
    vm.fromCurrentData({job: {file: {path: 'parts.gcode'}}, state: {flags: {printing: true}}, progress: {filepos: 100}});
    assert.equal(vm.objectList()[0].completed, true);
    assert.equal(vm.objectList()[0].cancellable, false);
    assert.equal(vm.objectList()[1].cancellable, true);
    vm.cancelOverviewObject(vm.objectList()[0]);
    assert.equal(sent.length, 1);
    // Fresh server state beats a stale enabled button.
    $.getJSON = () => $.Deferred().resolve(Object.assign({}, preview, {job_id: 'job2'})).promise();
    vm.refreshObjects = () => {};
    vm.cancelOverviewObject(vm.objectList()[1]);
    assert.equal(sent.length, 1);
    vm.fromCurrentData({job: {file: {path: 'parts.gcode'}}, state: {flags: {}}, progress: {filepos: 0}});
    assert(vm.objectList().every(row => !row.cancellable));
    dom.window.close();
    console.log('Live completion boundaries, guarded API and stale-job rejection passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
