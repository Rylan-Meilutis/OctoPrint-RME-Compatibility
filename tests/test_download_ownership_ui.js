const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');

(async function () {
    async function browser() {
        const dom = new JSDOM('<!doctype html><body></body>', {runScripts: 'outside-only'});
        const w = dom.window, $ = jQueryFactory(w);
        w.$ = w.jQuery = $;
        $.fn.modal = function () { return this; };
        w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
        let saves = 0, notices = 0;
        w.HTMLAnchorElement.prototype.click = () => saves++;
        w.PNotify = function () { notices++; };
        w.OctoPrint = {getBlueprintUrl: () => '/plugin/rme_compatibility/'};
        w.OCTOPRINT_VIEWMODELS = [];
        w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
        await new Promise(resolve => $(resolve));
        const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}]);
        return {dom, vm, $, saves: () => saves, notices: () => notices};
    }
    const owner = await browser(), passive = await browser();
    assert.notEqual(owner.vm.downloadClientId, passive.vm.downloadClientId);
    let command;
    owner.vm.command = (name, data) => { command = {name, data}; return owner.$.Deferred().promise(); };
    owner.vm.startStorageDownload({path: 'part.gcode'}, 'device');
    assert.equal(command.data.request_id, owner.vm.downloadClientId);
    const snapshot = (id, request, target = 'device') => ({connected: true, supported: true,
        storage: {download: {id, request_id: request, target, status: 'ready', name: 'part.gcode', progress: 100}}});
    owner.vm.acceptState(snapshot('other', passive.vm.downloadClientId));
    assert.equal(owner.vm.activeDownloadJobId, 'pending');
    assert.equal(owner.saves(), 0);
    const ready = snapshot('mine', command.data.request_id);
    // A websocket completion can precede the HTTP command response.
    owner.vm.acceptState(ready);
    owner.vm.acceptState(ready);
    passive.vm.acceptState(ready);
    assert.equal(owner.vm.activeDownloadJobId, 'mine');
    assert.equal(owner.saves(), 1);
    assert.equal(passive.saves(), 0);
    assert.equal(passive.notices(), 0);
    const reopened = await browser();
    reopened.vm.acceptState(ready);
    reopened.vm.acceptState(snapshot('legacy', undefined));
    assert.equal(reopened.saves(), 0);
    reopened.vm.downloadCachedStorageFile({id: 'mine', name: 'part.gcode'});
    assert.equal(reopened.saves(), 1); // Explicit cached downloads remain available.
    owner.vm.acceptState(snapshot('pi', owner.vm.downloadClientId, 'pi'));
    assert.equal(owner.saves(), 1);
    [owner, passive, reopened].forEach(b => b.dom.window.close());
    console.log('Initiator-only downloads, passive browsers, reloads and completion races passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
