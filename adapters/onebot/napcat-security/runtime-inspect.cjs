'use strict';
// Diagnostics only: mount as QQ's main module in an account-free container.
const { app, net, session } = require('electron');
const tls = require('node:tls');
const { X509Certificate, createHash } = require('node:crypto');
const { Worker } = require('node:worker_threads');
const digest = pem => {
    const x = new X509Certificate(pem);
    return { subject: x.subject, sha256: x.fingerprint256,
        spki: createHash('sha256').update(x.publicKey.export({ type: 'spki', format: 'der' })).digest('hex') };
};
function chain(cert) {
    const out = [], seen = new Set();
    while (cert && out.length < 20) {
        if (!cert.data || seen.has(cert.data)) break;
        seen.add(cert.data); out.push(digest(cert.data)); cert = cert.issuerCert;
    }
    return out;
}
console.log(JSON.stringify({kind:'runtime-api', versions:process.versions,
    getCA:typeof tls.getCACertificates, setCA:typeof tls.setDefaultCACertificates,
    roots:tls.rootCertificates.filter(pem=>/emSign|AAA Certificate Services/.test(new X509Certificate(pem).subject)).map(digest)}));
const deadline = setTimeout(() => app.exit(1), 20000);
app.whenReady().then(async () => {
    // Current API's worker behavior is investigated without modifying trust.
    await new Promise((resolve, reject) => {
        const w = new Worker("const t=require('node:tls');require('node:worker_threads').parentPort.postMessage({kind:'worker-api',getCA:typeof t.getCACertificates,setCA:typeof t.setDefaultCACertificates});", {eval:true});
        w.on('message', m=>console.log(JSON.stringify(m))); w.on('error', reject); w.on('exit', resolve);
    });
    const s = session.fromPartition('diagnostic', {cache:false});
    s.setCertificateVerifyProc((details, cb) => {
        console.log(JSON.stringify({kind:'chain',host:details.hostname,error:details.verificationResult,
            presented:chain(details.certificate),validated:chain(details.validatedCertificate)}));
        cb(-3);
    });
    await Promise.all(['dv-g3b-emudhra.we-have-a-problem.com','github.com'].map(host => new Promise(resolve => {
        const req = net.request({url:'https://'+host, session:s, redirect:'manual'});
        req.on('error', e=>{console.log(JSON.stringify({host,error:e.message}));resolve();});
        req.on('response', r=>{console.log(JSON.stringify({host,status:r.statusCode}));req.abort();resolve();});
        req.end();
    })));
    clearTimeout(deadline); app.exit(0);
}).catch(e=>{ console.error(e); app.exit(1); });
