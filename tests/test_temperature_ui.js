// NODE_PATH=/tmp/rme-mapping-ui-test/node_modules node tests/test_temperature_ui.js
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const api = require('../octoprint_rme_compatibility/static/js/passive_tools.js');
const {jQueryFactory} = require('jquery/factory');
(async function () {
const dom = new JSDOM(`<!doctype html><div id="temp"><table><tbody>
<!-- ko foreach: tools --><tr data-bind="template: {name: 'temprow-template'}"></tr><!-- /ko -->
<tr data-bind="template: {name: 'temprow-template', data: bedTemp}"></tr>
<tr data-bind="template: {name: 'temprow-template', data: chamberTemp}"></tr>
</tbody></table></div>
<div id="tab_plugin_dashboard"><!-- ko foreach: temperatureModel.tools -->
<span class="tool" data-bind="text: Math.round($parent.convertTemp(actual())) + $parent.tempSymbol()"></span><!-- /ko -->
<span data-bind="text: temperatureModel.bedTemp.actual"></span>
<span class="chamber" data-bind="css: gaugesCentreInGrid('chamber'), text: temperatureModel.chamberTemp.actual"></span></div>
<script type="text/html" id="temprow-template"><th data-bind="text: name"></th>
<td data-bind="html: formatTemperature(actual())"></td><td><form data-bind="submit: function(element) { $root.setTargetToValue($data, newTarget()) }">
<input data-bind="value: newTarget"><button type="submit">Set</button></form></td></script>`, {runScripts: 'outside-only'});
const w = dom.window;
const $ = jQueryFactory(w);
w.$ = w.jQuery = $;
w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
const ko = w.ko;
function item(key, actual) {
    return {key: ko.observable(key), name: ko.observable(key), actual: ko.observable(actual), newTarget: ko.observable(200)};
}
const sent = [];
const vm = {tools: ko.observableArray(Array.from({length: 8}, (_, n) => item('tool' + n, n === 3 ? 210 : -1))),
    bedTemp: item('bed', 60), chamberTemp: item('chamber', 35), clearAutosendTarget: () => {},
    hasChamber: ko.observable(true), updatePlot: () => {}, _printerProfileUpdated: () => {},
    _processTemperatureData: (_, data) => data,
    setTargetToValue: (entry, value) => sent.push([entry.key(), value]),
    rmeSendTemperature: command => {sent.push(command); return {done: cb => cb()};}};
const template = w.document.getElementById('temprow-template');
w.RmePassiveTools = api;
w.formatTemperature = String;
w.OctoPrint = {printer: {commands: vm.rmeSendTemperature}};
w.OctoPrintClient = {createRejectedDeferred: () => 'rejected'};
w.OCTOPRINT_VIEWMODELS = [];
w.gettext = x => x;
w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
await new Promise(resolve => $(resolve));
// Exercise the real constructor: jQuery.html() corrupts a <th>-leading
// script template even though direct innerHTML assignment passes.
const rme = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, {}, vm]);
const state = rme.state;
state({supported: true, machine: {hotends: 8, tool_capacity: 8}, active_tool: {physical: 3, logical: 0}});
assert(template.textContent.includes('<th'));
ko.applyBindings(vm, w.document.getElementById('temp'));
ko.applyBindings({temperatureModel: vm, convertTemp: x => x, tempSymbol: () => '°C',
    gaugesCentreInGrid: () => ({centreInGrid2: vm.tools().length === 8})}, w.document.getElementById('tab_plugin_dashboard'));
assert(!w.document.querySelector('#tab_plugin_dashboard .chamber').classList.contains('centreInGrid2'));
assert.equal(w.document.querySelectorAll('#temp tr').length, 3);
assert.equal(w.document.querySelectorAll('#tab_plugin_dashboard .tool').length, 1);
assert(!w.document.getElementById('temp').textContent.includes('-1'));
const form = w.document.querySelector('#temp form');
form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
assert.equal(sent[0], 'M104 S200'); // Not T3: the physical slot is mapped to logical T0.
vm.setTargetToValue(vm.chamberTemp, 45);
vm.setTargetToValue(vm.bedTemp, 70);
assert.equal(sent[1], 'M141 S45');
assert.deepEqual(sent[2], ['bed', 70]);
// Dashboard centres its final grid row using the number of tools it reads.
const toolsObservable = vm.tools;
const dashboard = {gaugesCentreInGrid: (type, index, css = {}) => {
    const count = vm.tools().length + 3; // tools, bed, chamber, fan
    css.centreInGrid2 = count % 3 === 2 && ['chamber', 'fan'].includes(type);
    css.centreInGrid1 = count % 3 === 1 && type === 'fan';
    return css;
}};
assert.equal(vm.rmeGaugesCentreInGrid(dashboard, 'chamber').centreInGrid2, false);
assert.equal(vm.rmeGaugesCentreInGrid(dashboard, 'fan').centreInGrid1, true);
assert.equal(vm.tools, toolsObservable);
assert.throws(() => vm.rmeGaugesCentreInGrid({gaugesCentreInGrid: () => {throw Error('layout');}}, 'tool'));
assert.equal(vm.tools, toolsObservable); // Restore even if another plugin fails.
// Native preheat-all walks all physical rows. Only the mounted tool may send
// a nozzle command; bed and chamber keep their independent targets.
function preheat() {
    vm.tools().forEach(entry => vm.setTargetToValue(entry, 215));
    vm.setTargetToValue(vm.bedTemp, 60);
    vm.setTargetToValue(vm.chamberTemp, 35);
}
sent.length = 0;
preheat();
assert.deepEqual(sent, ['M104 S215', ['bed', 60], 'M141 S35']);
vm.tools()[3].actual(-1);
state({...state(), active_tool: {physical: null}});
assert.equal(w.document.querySelectorAll('#temp tr').length, 3);
assert.equal(w.document.querySelectorAll('#tab_plugin_dashboard .tool').length, 1);
assert.equal(w.document.querySelector('#tab_plugin_dashboard .tool').textContent, 'Unloaded');
assert(w.document.getElementById('temp').textContent.includes('Unloaded'));
assert(w.document.getElementById('temp').textContent.includes('60'));
assert(w.document.getElementById('temp').textContent.includes('35'));
assert.equal(vm.tools().length, 8); // Never corrupt telemetry indexing.
sent.length = 0;
preheat();
assert.deepEqual(sent, [['bed', 60], 'M141 S35']);
vm.tools()[3].actual(-1);
vm.tools()[7].actual(225);
// The readable head arrives before the delayed session/tool-state snapshot.
assert.equal(w.document.querySelector('#tab_plugin_dashboard .tool').textContent, '225°C');
assert.equal(vm.rmeToolName(vm.rmeVisibleTools()[0]), 'Tool T7');
sent.length = 0;
preheat();
assert.deepEqual(sent, [['bed', 60], 'M141 S35']); // No unconfirmed heater writes.
state({...state(), active_tool: {physical: 7}});
assert.equal(w.document.querySelector('#tab_plugin_dashboard .tool').textContent, '225°C');
assert.equal(w.document.querySelectorAll('#temp tr').length, 3);
sent.length = 0;
preheat();
assert.deepEqual(sent, ['M104 S215', ['bed', 60], 'M141 S35']);
// Likewise, parking clears the display before the session catches up.
vm.tools()[7].actual(-1);
assert.equal(w.document.querySelector('#tab_plugin_dashboard .tool').textContent, 'Unloaded');
state({...state(), supported: false});
assert.equal(vm.rmeGaugesCentreInGrid(dashboard, 'chamber').centreInGrid2, true);
assert.equal(w.document.querySelectorAll('#temp tr').length, 10);
assert.equal(w.document.querySelectorAll('#tab_plugin_dashboard .tool').length, 8);
dom.window.close();
console.log('INDX Temperature/Dashboard bindings and heater controls passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
