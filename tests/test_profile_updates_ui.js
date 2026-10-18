const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('', {runScripts: 'outside-only'});
const w = dom.window;
w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
// Use OctoPrint's real mapping library, including its circular metadata.
w.eval(fs.readFileSync(require.resolve('knockout-mapping'), 'utf8'));
const ko = w.ko;
const {installStableProfileUpdates} = require('../octoprint_rme_compatibility/static/js/ui_performance.js');
const profile = {id: 'indx', extruder: {count: 8, sharedNozzle: true, offsets: [[0, 0]]},
    volume: {width: 250, depth: 205.5, height: 270}, axes: {x: {speed: 350}}};
const current = ko.observable(ko.mapping.fromJS(profile));
const models = {currentProfileData: current};
let changed = 0;
current.subscribe(() => changed++);
assert(installStableProfileUpdates(models, ko));
const comparer = current.equalityComparer;
assert(installStableProfileUpdates(models, ko));
assert.equal(current.equalityComparer, comparer, 'installation must not stack wrappers');
for (let i = 0; i < 100; i++) current(ko.mapping.fromJS(profile));
assert.equal(changed, 0, 'opening Settings must not notify for an identical mapped profile');
for (const edit of [p => p.extruder.count = 7, p => p.extruder.sharedNozzle = false,
    p => p.extruder.offsets[0][0] = 1, p => p.volume.depth = 200,
    p => p.axes.x.speed = 300, p => p.id = 'other']) {
    const changedProfile = JSON.parse(JSON.stringify(profile));
    edit(changedProfile);
    current(ko.mapping.fromJS(changedProfile));
    current(ko.mapping.fromJS(profile));
}
assert.equal(changed, 12, 'all real profile changes must notify and restore normally');
current.valueHasMutated();
assert.equal(changed, 13, 'explicit native notifications must still work');
const own = ko.observable({id: 'custom'});
const ownComparer = own.equalityComparer = () => false;
assert.equal(installStableProfileUpdates({currentProfileData: own}, ko), false);
assert.equal(own.equalityComparer, ownComparer);
// Serialization failure must not break OctoPrint's profile response handler.
const toJS = ko.mapping.toJS;
ko.mapping.toJS = () => { throw new Error('unknown extension'); };
current(ko.mapping.fromJS(profile));
assert.equal(changed, 14);
ko.mapping.toJS = toJS;
assert.equal(installStableProfileUpdates({}, ko), false);
console.log('Identical printer-profile refresh guard and genuine changes passed');
w.close();
