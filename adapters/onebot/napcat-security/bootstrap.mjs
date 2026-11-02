// Experimental subtract-only policy, imported before NapCat module dependencies.
// Native QQ libraries, pre-saved Worker constructors, same-process tampering and explicit custom CA remain outside scope.
import { createRequire } from 'node:module';
import { isMainThread, threadId } from 'node:worker_threads';
const require = createRequire(import.meta.url);
const policy = require('./runtime-policy.cjs');
const workerGuard = require('./worker-guard.cjs');
const probe = require('./probe.cjs');
const identity = {pid:process.pid, thread:threadId, type:process.type || 'node',
    napcat_worker:process.env.NAPCAT_WORKER_PROCESS === '1'};
const emit = value => process.stderr.write(JSON.stringify({kind:'clonoth-tls-bootstrap',...identity,...value})+'\n');

if (process.env.NODE_TLS_REJECT_UNAUTHORIZED === '0') throw new Error('Unsafe TLS override');
const nodePolicy = policy.installNode();
const guardState = workerGuard.installWorkerGuard();
let electron;
const decisions = new Map();
let chromium;
if (isMainThread && process.type === 'browser') {
    electron = require('electron');
    const unsafe = probe.unsafeEnvironment(process.env, process.argv, electron.app);
    if (unsafe.length) throw new Error('Unsafe/custom TLS configuration: '+unsafe.join('; '));
    chromium = policy.installChromium(electron, decision => decisions.set(decision.host, decision));
    await chromium.ready;
}
emit({stage:'policy-ready', node:nodePolicy, worker_guard:guardState.installed, chromium:!!chromium, full_coverage:false});

function chromiumCheck(target) {
    return new Promise(resolve => {
        let request, done = false;
        const finish = observation => {
            if (done) return;
            done = true; clearTimeout(timer); request?.abort();
            resolve(probe.verdict(target, observation));
        };
        const timer = setTimeout(()=>finish({kind:'error',error:{code:'PROBE_TIMEOUT'}}),15000);
        try {
            request = electron.net.request({url:'https://'+target.host+'/',
                session:electron.session.defaultSession,redirect:'manual',useSessionCookies:false});
            request.on('response',r=>finish({kind:'response',status:r.statusCode}));
            request.on('redirect',status=>finish({kind:'response',status}));
            request.on('error',error=> {
                if (/\bERR_FAILED\b/.test(error.message) && decisions.get(target.host)?.code === -206)
                    error = new Error('net::ERR_CERT_REVOKED');
                finish({kind:'error',error});
            });
            request.end();
        } catch(error) {finish({kind:'error',error});}
    });
}

// Optional diagnostic, not an automatic approval gate. Inconclusive stays inconclusive.
// It does not modify verification callbacks or use account cookies.
if (process.env.CLONOTH_TLS_SELFTEST === '1' && isMainThread) {
    const results = [];
    const targets = probe.targets();
    for (let i=0;i<targets.length;i+=4) {
        results.push(...await Promise.all(targets.slice(i,i+4).map(async target=>
            ({stack:'node',...await probe.nodeRequest(target,15000)}))));
    }
    if (process.env.CLONOTH_TLS_SELFTEST_WORKER === '1') {
        const { Worker } = require('node:worker_threads');
        const workerResults = await new Promise((resolve, reject) => {
            const script = `const tls=require('node:tls');
                const {parentPort,workerData}=require('node:worker_threads');
                const pol=require(workerData.policy);
                if(pol.filterRoots(tls.getCACertificates('default')).removed.length!==0){
                    throw new Error('worker selftest entry had unremoved blocked roots');
                }
                const p=require(workerData.probe);
                (async()=>{
                    const out=[];
                    for(let i=0;i<workerData.targets.length;i+=4){
                        out.push(...await Promise.all(workerData.targets.slice(i,i+4).map(t=>p.nodeRequest(t,15000))));
                    }
                    parentPort.postMessage(out);
                })().catch(e=>{throw e});`;
            const w = new Worker(script, {eval:true, workerData:{
                probe:require.resolve('./probe.cjs'),
                policy:require.resolve('./runtime-policy.cjs'),
                targets,
            }});
            const timer = setTimeout(() => { w.terminate(); reject(new Error('worker selftest deadline')); }, 60000);
            w.on('message', r => { clearTimeout(timer); resolve(r); });
            w.on('error', e => { clearTimeout(timer); reject(e); });
            w.on('exit', c => { if (c !== 0) { clearTimeout(timer); reject(new Error('worker selftest exit ' + c)); } });
        });
        results.push(...workerResults.map(r => ({stack:'node-worker', ...r})));
    }
    if (chromium) {
        for (let i=0;i<targets.length;i+=3) {
            results.push(...await Promise.all(targets.slice(i,i+3).map(async target=>
                ({stack:'chromium',...await chromiumCheck(target)}))));
        }
    }
    emit({stage:'selftest',ok:results.every(r=>r.verdict==='pass'),results,full_coverage:false});
}
