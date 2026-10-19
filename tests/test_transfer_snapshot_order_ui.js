const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');
(async function () {
    const dom = new JSDOM('<!doctype html><body></body>', {runScripts: 'outside-only'});
    const w = dom.window, $ = jQueryFactory(w);
    w.$ = w.jQuery = $;
    $.fn.modal = function () { return this; };
    w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
    w.OCTOPRINT_VIEWMODELS = [];
    w.gettext = x => x;
    w.PNotify = function () {};
    w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
    await new Promise(resolve => $(resolve));
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
    const snapshot = (revision, status, progress, id = 'job1', epoch = 'bootA') => ({
        _snapshot: {epoch, revision}, supported: true, connected: true,
        firmware: {status: 'idle'},
        storage: {status: status === 'queued' ? 'download queued' : status, progress,
            download: {id, status, progress, target: 'pi', name: 'part.gcode', size: 100, offset: progress}}
    });
    vm.activeDownloadJobId = 'job1';
    vm.acceptState(snapshot(2, 'downloading', 50));
    assert.equal(vm.navbarTransfer().progress, 50);
    vm.acceptState(snapshot(1, 'queued', 0));
    assert.equal(vm.navbarTransfer().progress, 50);
    vm.pendingStateSnapshot = snapshot(3, 'ready', 100);
    vm.acceptState(snapshot(2, 'downloading', 50));
    assert.equal(vm.storageDownloadJob().status, 'ready');
    assert.equal(vm.navbarTransfer().active, false);
    vm.acceptState(snapshot(1, 'queued', 0));
    assert.equal(vm.navbarTransfer().active, false);
    assert.equal(vm.storageDownloadJob().progress, 100);

    const frames = [];
    w.requestAnimationFrame = callback => frames.push(callback);
    vm.activeDownloadJobId = 'job2';
    vm.onDataUpdaterPluginMessage('rme_compatibility', snapshot(5, 'downloading', 80, 'job2'));
    vm.onDataUpdaterPluginMessage('rme_compatibility', snapshot(4, 'queued', 0, 'job2'));
    assert.equal(vm.pendingStateSnapshot._snapshot.revision, 5);
    frames.shift()();
    assert.equal(vm.navbarTransfer().progress, 80);
    vm.acceptState(snapshot(1, 'ready', 100, 'job2', 'bootB')); // Server restarted.
    assert.equal(vm.navbarTransfer().active, false);

    vm.acceptState({_snapshot: {epoch: 'bootB', revision: 2}, firmware: {status: 'idle'}, storage: {},
        firmware_releases: {status: 'downloading', progress: 45}});
    assert.equal(vm.navbarTransfer().progress, 45);
    assert.match(vm.navbarTransfer().summary, /GitHub/);
    vm.acceptState({_snapshot: {epoch: 'bootB', revision: 3}, firmware: {status: 'idle'}, storage: {},
        firmware_releases: {status: 'downloaded', progress: 100}});
    assert.equal(vm.navbarTransfer().active, false);
    dom.window.close();
    console.log('Ordered transfer snapshots, completion and GitHub download progress passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
