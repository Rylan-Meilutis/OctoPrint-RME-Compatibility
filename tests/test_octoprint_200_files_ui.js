const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {jQueryFactory} = require('jquery/factory');

(async function () {
    for (const origin of ['sdcard', 'printer']) {
        const dom = new JSDOM('<!doctype html><body></body>', {runScripts:'outside-only'});
        const w = dom.window, $ = jQueryFactory(w);
        w.$ = w.jQuery = $;
        w.eval(fs.readFileSync(require.resolve('knockout/build/output/knockout-latest.js'), 'utf8'));
        w.PNotify = function () {};
        w.OctoPrint = {getBlueprintUrl:()=>'/plugin/rme_compatibility/', simpleApiGet:()=>$.Deferred().promise()};
        w.OCTOPRINT_VIEWMODELS = [];
        w.eval(fs.readFileSync('octoprint_rme_compatibility/static/js/rme_compatibility.js', 'utf8'));
        await new Promise(resolve=>$(resolve));
        let captured, params;
        const files = {fromResponse:(response, p)=>{captured=response;params=p;return 'native-result';},
            downloadLink:()=>'', enableMove:()=>false, showMoveDialog:()=>{}, _removeEntry:()=>{},
            enableSelect:()=>true, enableSelectAndPrint:()=>true, requestData:()=>{}};
        const vm = new w.OCTOPRINT_VIEWMODELS[0].construct([{}, {}, {}, files]);
        vm.onBeforeBinding();
        vm.acceptState({supported:true, connected:true, host:{printer_file_origin:origin},
            storage:{supported:true, native_files:[{path:'/jobs/part.gcode',size:100,category:'gcode'}]}});
        const legacy = {files:[{name:'local.gcode',origin:'local'}, {name:'cloud.gcode',origin:'cloud'},
            {name:'old.gcode',origin:'sdcard'}, {name:'duplicate.gcode',origin:'printer'}]};
        const modern = {local:{key:'local',files:[legacy.files[0]],capabilities:{write_file:true}},
            cloud:{key:'cloud',files:[legacy.files[1]]},
            printer:{key:'printer',name:'USB',files:[legacy.files[3]],capabilities:{read_file:false},usage:{free:123}}};
        const response = origin === 'printer' ? modern : legacy;
        const focus = {focus:'part.gcode', force:true};
        assert.equal(files.fromResponse(response, focus), 'native-result');
        assert.equal(params, focus, 'modern params are preserved');
        let tree;
        if (origin === 'printer') {
            assert.equal(captured.local, response.local);
            assert.equal(captured.cloud, response.cloud);
            assert.equal(captured.printer.name, 'USB');
            assert.equal(captured.printer.usage.free, 123);
            assert.equal(captured.printer.capabilities.read_file, false);
            assert.equal(response.printer.files.length, 1);
            assert.equal(response.printer.files[0].name, 'duplicate.gcode');
            assert.equal(captured.files, undefined, 'never add a flat pseudo-storage to 2.0');
            tree = captured.printer.files;
        } else {
            assert.equal(captured.files.length, 3);
            assert.equal(captured.files[0].origin, 'local');
            assert.equal(captured.files[1].origin, 'cloud');
            assert.equal(response.files.length, 4);
            tree = captured.files.slice(2);
        }
        assert.equal(tree[0].origin, origin);
        assert.equal(tree[0].children[0].origin, origin);
        assert.equal(tree[0].children[0].path, 'jobs/part.gcode');
        assert.equal(files.enableSelect(tree[0]), false, 'folders cannot print');
        assert.equal(files.enableSelect(tree[0].children[0]), true);
        dom.window.close();
    }
    console.log('OctoPrint 1.11/2.0 native file origins, replacement and modern params passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
