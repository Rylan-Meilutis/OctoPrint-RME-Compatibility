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
    const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {isPrinting: w.ko.observable(true), stateString: w.ko.observable('Printing')}, {}]);
    const commands = [];
    vm.command = (name, data) => commands.push([name, data]);
    const template = fs.readFileSync('octoprint_rme_compatibility/templates/rme_compatibility_settings.jinja2', 'utf8');
    w.document.body.innerHTML = template.slice(template.indexOf('<h4>Chamber'), template.indexOf('    <p>Configure saved behavior'));
    w.ko.applyBindings(vm, w.document.body);
    const state = {connected: true, supported: true, machine: {tune: 1}, lock: {locked: false},
        tune: {material_cooling: 0, print_filter: 1, post_filter: 1, updated: Date.now()/1000}};
    vm.state(state);
    vm.tick(Date.now());
    const selects = [...w.document.querySelectorAll('select')];
    assert.equal(selects.length, 3);
    assert(selects.every(node => !node.disabled));
    for (const node of selects) {
        node.value = '0';
        node.dispatchEvent(new w.Event('change', {bubbles: true}));
        const [name, data] = commands.at(-1);
        assert.equal(name, 'set_print_override');
        assert.equal(data.kind, node.getAttribute('data-chamber-setting'));
        assert.equal(data.value, 0);
    }
    vm.state({...state, tune: {updated: Date.now()/1000}});
    w.ko.tasks.runEarly();
    assert(selects.every(node => node.disabled));
    vm.state({...state, lock: {locked: true}});
    w.ko.tasks.runEarly();
    assert(selects.every(node => node.disabled));
    vm.state({...state, connected: false});
    w.ko.tasks.runEarly();
    assert(selects.every(node => node.disabled));
    console.log('Chamber settings browser tests passed');
    w.close();
})().catch(error => {console.error(error); process.exitCode = 1;});
