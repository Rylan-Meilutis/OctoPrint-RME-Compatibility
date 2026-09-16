// NODE_PATH=/tmp/rme-mapping-ui-test/node_modules node tests/test_temperature_ui.js
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const api = require('../octoprint_rme_compatibility/static/js/passive_tools.js');
const dom = new JSDOM(`<!doctype html><div id="temperature"><table><tbody>
<!-- ko foreach: tools --><tr data-bind="template: {name: 'temprow-template'}"></tr><!-- /ko -->
<tr data-bind="template: {name: 'temprow-template', data: bedTemp}"></tr>
<tr data-bind="template: {name: 'temprow-template', data: chamberTemp}"></tr>
</tbody></table></div>
<div id="tab_plugin_dashboard"><!-- ko foreach: temperatureModel.tools -->
<span class="tool" data-bind="text: key"></span><!-- /ko -->
<span data-bind="text: temperatureModel.bedTemp.actual"></span>
<span data-bind="text: temperatureModel.chamberTemp.actual"></span></div>
<script type="text/html" id="temprow-template"><td data-bind="text: key"></td>
<td data-bind="html: formatTemperature(actual())"></td><td><form data-bind="submit: function(element) { $root.setTargetToValue($data, newTarget()) }">
<input data-bind="value: newTarget"><button type="submit">Set</button></form></td></script>`, {runScripts: 'outside-only'});
const w = dom.window;
w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
const ko = w.ko;
const state = ko.observable({supported: true, machine: {hotends: 8, tool_capacity: 8}, active_tool: {physical: 3, logical: 0}});
function item(key, actual) {
    return {key: ko.observable(key), actual: ko.observable(actual), newTarget: ko.observable(200)};
}
const sent = [];
const vm = {tools: ko.observableArray(Array.from({length: 8}, (_, n) => item('tool' + n, n === 3 ? 210 : -1))),
    bedTemp: item('bed', 60), chamberTemp: item('chamber', 35), clearAutosendTarget: () => {},
    _processTemperatureData: (_, data) => data,
    setTargetToValue: (entry, value) => sent.push([entry.key(), value]),
    rmeSendTemperature: command => {sent.push(command); return {done: cb => cb()};}};
const template = w.document.getElementById('temprow-template');
template.innerHTML = api.install(vm, state, ko.unwrap, template.innerHTML, String, () => 'rejected');
api.installRows(w.document);
ko.applyBindings(vm, w.document.getElementById('temperature'));
ko.applyBindings({temperatureModel: vm}, w.document.getElementById('tab_plugin_dashboard'));
assert.equal(w.document.querySelectorAll('#temperature tr').length, 3);
assert.equal(w.document.querySelectorAll('#tab_plugin_dashboard .tool').length, 1);
assert(!w.document.getElementById('temperature').textContent.includes('-1'));
const form = w.document.querySelector('#temperature form');
form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
assert.equal(sent[0], 'M104 S200'); // Not T3: the physical slot is mapped to logical T0.
vm.setTargetToValue(vm.chamberTemp, 45);
vm.setTargetToValue(vm.bedTemp, 70);
assert.equal(sent[1], 'M141 S45');
assert.deepEqual(sent[2], ['bed', 70]);
state({...state(), active_tool: {physical: null}});
assert.equal(w.document.querySelectorAll('#temperature tr').length, 2);
assert.equal(w.document.querySelectorAll('#tab_plugin_dashboard .tool').length, 0);
assert.equal(vm.tools().length, 8); // Never corrupt telemetry indexing.
state({...state(), supported: false});
assert.equal(w.document.querySelectorAll('#temperature tr').length, 10);
assert.equal(w.document.querySelectorAll('#tab_plugin_dashboard .tool').length, 8);
dom.window.close();
console.log('INDX Temperature/Dashboard bindings and heater controls passed');
