// Profile a real, isolated OctoPrint server (no printer or user credentials).
// NODE_PATH=<playwright modules> node tests/browser/profile_octoprint_settings.js
const fs = require('fs');
const {chromium} = require('playwright');
(async () => {
    const browser = await chromium.launch({headless: true});
    try {
        const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
        const errors = [], requests = [];
        page.on('pageerror', error => { errors.push(error.message); console.error('Browser error:', error.message); });
        await page.route('**/*', route => {
            const url = new URL(route.request().url());
            return url.hostname === '127.0.0.1' ? route.continue() : route.abort();
        });
        page.on('requestfinished', request => {
            const timing = request.timing();
            requests.push({path: new URL(request.url()).pathname, ms: timing.responseEnd});
        });
        const login = await page.request.post('http://127.0.0.1:5517/api/login', {
            headers: {'X-Api-Key': 'RME-PROFILE-LOCAL-ONLY'},
            data: {user: 'rmeprofile', pass: 'profile-local-only', remember: false}
        });
        if (!login.ok()) throw new Error('Temporary fixture login failed: ' + login.status());
        await page.goto('http://127.0.0.1:5517/', {waitUntil: 'domcontentloaded', timeout: 90000});
        await page.waitForFunction(() => window.ko && ko.dataFor(document.getElementById('settings_dialog'))?.show, {timeout: 90000});
        await page.waitForTimeout(2500);
        await page.waitForFunction(() => {
            const model = window.ko && ko.dataFor(document.getElementById('settings_dialog'));
            if (!model?.allViewModels?.length) return false;
            window.profileSettingsVm = model;
            return true;
        }, null, {timeout: 90000});
        const session = await page.context().newCDPSession(page);
        if (process.env.RME_PROFILE_CPU_RATE) await session.send('Emulation.setCPUThrottlingRate', {rate: Number(process.env.RME_PROFILE_CPU_RATE)});
        await session.send('Profiler.enable'); await session.send('Profiler.start');
        const result = await page.evaluate(async baseline => {
            const settings = window.profileSettingsVm || window.OctoPrint?.coreui?.viewmodels?.settingsViewModel;
            if (!settings) throw new Error('Settings reference lost; globals=' + [!!window.ko, !!window.OctoPrint, document.readyState].join(','));
            const hooks = [], longTasks = [];
            if (baseline && settings.printerProfiles.currentProfileData._rmeStableProfileUpdates) {
                // Reproduce b127's native behavior for an A/B test in this
                // disposable browser only. Never change server configuration.
                settings.printerProfiles.currentProfileData.equalityComparer = ko.observable.fn.equalityComparer;
                settings.printerProfiles.currentProfileData._rmeStableProfileUpdates = false;
            }
            new PerformanceObserver(list => longTasks.push(...list.getEntries().map(e => e.duration)))
                .observe({type: 'longtask', buffered: false});
            for (const vm of settings.allViewModels || []) {
                if (typeof vm.onSettingsShown !== 'function') continue;
                const original = vm.onSettingsShown;
                vm.onSettingsShown = function (...args) {
                    const start = performance.now();
                    try { return original.apply(this, args); }
                    finally { hooks.push({model: vm.constructor.name, bindings: vm._bindings, ms: performance.now() - start}); }
                };
            }
            let maxHeartbeatMs = 0, last = performance.now();
            const heartbeat = setInterval(() => {const now = performance.now(); maxHeartbeatMs = Math.max(maxHeartbeatMs, now-last); last=now;}, 16);
            const started = performance.now();
            const shown = new Promise(resolve => $('#settings_dialog').one('shown', resolve));
            settings.show();
            const syncMs = performance.now() - started;
            await Promise.race([shown, new Promise((_, reject) => setTimeout(() => reject(new Error('Settings never shown')), 20000))]);
            const shownMs = performance.now() - started;
            await new Promise(resolve => setTimeout(resolve, 2000));
            clearInterval(heartbeat);
            return {syncMs, shownMs, maxHeartbeatMs, hooks, longTasks,
                nativeProfileGuard: !!settings.printerProfiles.currentProfileData._rmeStableProfileUpdates,
                nodes: document.querySelectorAll('*').length};
        }, !!process.env.RME_PROFILE_BASELINE);
        const {profile} = await session.send('Profiler.stop');
        const output = '/tmp/rme-real-settings' + (process.env.RME_PROFILE_CPU_RATE ? '-cpu' + process.env.RME_PROFILE_CPU_RATE : '') + (process.env.RME_PROFILE_BASELINE ? '-baseline' : '-fixed');
        fs.writeFileSync(output + '.cpuprofile', JSON.stringify(profile));
        fs.writeFileSync(output + '.json', JSON.stringify({...result, errors, slowRequests: requests.filter(r => r.ms > 500)}, null, 2));
        console.log(JSON.stringify({...result, errors, slowRequests: requests.filter(r => r.ms > 500)}, null, 2));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
