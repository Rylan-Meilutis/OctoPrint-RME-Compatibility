// Run against a disposable loopback-only fixture, never a live printer server.
const fs = require('fs');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

(async () => {
    const credentials = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const url = process.env.RME_SMOKE_URL || 'http://127.0.0.1:5537';
    assert.equal(new URL(url).hostname, '127.0.0.1', 'fixture must be loopback-only');
    const browser = await chromium.launch({headless:true,
        executablePath:process.env.RME_CHROMIUM_EXECUTABLE || undefined});
    try {
        const page = await browser.newPage();
        const errors = [];
        page.on('pageerror', e=>errors.push(e.message));
        await page.route('**/*', route=>new URL(route.request().url()).hostname === '127.0.0.1'
            ? route.continue() : route.abort());
        const anonymous = await page.request.get(url+'/api/plugin/rme_compatibility');
        assert.ok([401,403].includes(anonymous.status()));
        await page.goto(url+'/', {waitUntil:'domcontentloaded'});
        let cookie = (await page.context().cookies()).find(c=>c.name.startsWith('csrf_token'));
        assert.ok(cookie, 'core supplies CSRF cookie');
        const login = await page.request.post(url+'/api/login', {
            headers:{'X-CSRF-Token':cookie.value},
            data:{user:credentials.user, pass:credentials.password, remember:false}});
        assert.equal(login.status(), 200, 'temporary administrator login');
        await page.goto(url+'/', {waitUntil:'domcontentloaded'});
        await page.waitForFunction(()=>window.ko && document.getElementById('tab_plugin_rme_compatibility')
            && ko.dataFor(document.getElementById('tab_plugin_rme_compatibility'))?.files?.storageFiles,
            null,{timeout:60000});
        const versionReply = await page.request.get(url+'/api/version');
        assert.equal(versionReply.status(),200);
        const hostVersion = (await versionReply.json()).server;
        assert.match(hostVersion,/^2\.0\./);
        const api = await page.request.get(url+'/api/plugin/rme_compatibility');
        assert.equal(api.status(),200);
        const state = await api.json();
        assert.equal(state.host.printer_file_origin,'printer');
        assert.equal(state.connected,false,'fixture must not connect a printer');
        const denied = await page.request.post(url+'/plugin/rme_compatibility/storage/upload', {data:{}});
        assert.equal(denied.status(),400);
        assert.match(await denied.text(),/CSRF/);
        cookie = (await page.context().cookies()).find(c=>c.name.startsWith('csrf_token'));
        const validated = await page.request.post(url+'/plugin/rme_compatibility/storage/upload', {
            headers:{'X-CSRF-Token':cookie.value},data:{}});
        assert.equal(validated.status(),409, 'authenticated upload is safely refused while printer is offline');
        assert.doesNotMatch(await validated.text(),/CSRF validation failed/);
        const result = await page.evaluate(async () => {
            const vm = ko.dataFor(document.getElementById('tab_plugin_rme_compatibility'));
            const response = await new Promise((resolve,reject)=>OctoPrint.files.list(true,true).done(resolve).fail(reject));
            vm.acceptState({...vm.state(),supported:true,connected:false,
                storage:{...vm.storage(),supported:true,native_files:[{path:'jobs/part.gcode',category:'machinecode',size:4}]}});
            vm.files.fromResponse(response, {focus:[]});
            const native = vm.files.storageFiles();
            const item = native.printer.files[0].children[0];
            const settings = ko.dataFor(document.getElementById('settings_dialog'));
            settings.show();
            return {origin:item.origin,path:item.path,flat:Array.isArray(native.files),
                storages:Object.keys(native),localPreserved:native.local === response.local};
        });
        assert.equal(result.origin,'printer');
        assert.equal(result.path,'jobs/part.gcode');
        assert.equal(result.flat,false);
        assert.equal(result.localPreserved,true);
        await page.locator('#settings_dialog').waitFor({state:'visible'});
        await page.locator('a[href="#settings_plugin_rme_compatibility"]').click();
        await page.locator('#settings_plugin_rme_compatibility').waitFor({state:'visible'});
        assert.deepEqual(errors,[],'no real OctoPrint 2.0 frontend exceptions');
        console.log(JSON.stringify({status:'passed',octoprint:hostVersion,authenticated:true,
            globalApiKeyUsed:false,csrfEnforced:true,settingsOpened:true,files:result,errors}));
    } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
