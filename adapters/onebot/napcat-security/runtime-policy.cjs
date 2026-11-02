'use strict';
// EXPERIMENTAL: explicit installation only, not a complete QQ security boundary.
// Node: current thread + future default-CA connections only.
// Chromium: installed sessions only; a later verify-proc replacement can undo it.
const fs = require('node:fs');
const path = require('node:path');
const tls = require('node:tls');
const { X509Certificate, createHash } = require('node:crypto');
const EVIDENCE_SHA256 = '233b7a087b5610612342527fe4b15a1a0122137a2a94b2591a56a6e67423c241';
const hash = value => createHash('sha256').update(value).digest('hex');
const spki = cert => hash(cert.publicKey.export({type:'spki',format:'der'}));

function loadPolicy() {
    const raw = fs.readFileSync(path.join(__dirname, 'ca-evidence.json'));
    if (hash(raw) !== EVIDENCE_SHA256) throw new Error('CA evidence hash mismatch');
    const evidence = JSON.parse(raw);
    const rootKeys = new Set(), blockedKeys = new Set();
    for (const [group, records] of Object.entries(evidence)) {
        for (const record of records) {
            const cert = new X509Certificate(record.pem);
            const key = spki(cert);
            if (!cert.ca || hash(cert.raw) !== record.sha256.toLowerCase()
                || key !== record.spki_sha256.toLowerCase()) throw new Error('Invalid CA evidence');
            blockedKeys.add(key);
            if (group === 'roots') rootKeys.add(key);
        }
    }
    if (rootKeys.size !== 4 || blockedKeys.size !== 12) throw new Error('Incomplete CA evidence');
    return {rootKeys, blockedKeys};
}
const policy = loadPolicy();

function filterRoots(pems) {
    const removed = [], retained = [];
    for (const pem of pems) {
        const cert = new X509Certificate(pem);
        if (policy.blockedKeys.has(spki(cert))) removed.push({subject:cert.subject,spki:spki(cert)});
        else retained.push(pem);
    }
    if (!retained.length) throw new Error('Refusing an empty trust store');
    return {removed, retained};
}

function installNode() {
    if (typeof tls.getCACertificates !== 'function' || typeof tls.setDefaultCACertificates !== 'function') {
        throw new Error('Required Node CA APIs unavailable; no unsafe fallback');
    }
    const current = tls.getCACertificates('default');
    const {removed, retained} = filterRoots(current);
    // Only subtract from existing trust; never add roots from a downloaded source.
    tls.setDefaultCACertificates(retained);
    const after = tls.getCACertificates('default');
    if (filterRoots(after).removed.length) throw new Error('Node CA replacement did not take effect');
    return {kind:'node-policy', evidence_sha256:EVIDENCE_SHA256,
        original_count:current.length, retained_count:after.length, removed};
}

function inspectChain(start) {
    if (!start || !start.data) throw new Error('Missing certificate chain');
    const seen = new Set();
    let cert = start;
    for (let depth = 0; cert; depth++) {
        if (depth >= 20 || !cert.data) throw new Error('Malformed/oversized certificate chain');
        const x509 = new X509Certificate(cert.data);
        const fingerprint = hash(x509.raw);
        if (seen.has(fingerprint)) break; // Electron may represent a self-signed root twice.
        seen.add(fingerprint);
        const key = spki(x509);
        if (policy.blockedKeys.has(key)) return {blocked:true,spki:key,subject:x509.subject};
        cert = cert.issuerCert;
    }
    return {blocked:false};
}

function decide(details) {
    try {
        // Check presented AND built chains: cross-signs can have different DER
        // while sharing the compromised public key. Hostnames never drive policy.
        for (const chain of [details.certificate, details.validatedCertificate].filter(Boolean)) {
            const result = inspectChain(chain);
            if (result.blocked) return {code:-206,reason:'blocked_spki',...result}; // CERT_REVOKED
        }
        if (!details.certificate || !details.validatedCertificate) {
            // Existing Chromium rejection stays a rejection. A claimed success
            // without an inspectable built chain must not bypass this policy.
            if (details.verificationResult !== 'OK' && details.verificationResult !== 'net::OK') {
                return {code:-3,reason:'default_rejection'};
            }
            throw new Error('Missing validated certificate chain');
        }
        return {code:-3,reason:'default_verification'};
    } catch (_) {
        return {code:-202,reason:'uninspectable_chain'}; // CERT_AUTHORITY_INVALID; fail closed.
    }
}

function installChromium(electron, onDecision = () => {}) {
    const installed = new WeakSet();
    let count = 0;
    function install(ses) {
        if (installed.has(ses)) return;
        ses.setCertificateVerifyProc((details, callback) => {
            const decision = decide(details);
            // Observability must never turn a rejection into acceptance.
            try { onDecision({host:details.hostname,...decision}); } catch (_) { /* keep policy */ }
            callback(decision.code); // NEVER 0: preserve default CT/hostname/expiry validation.
        });
        installed.add(ses); count++;
    }
    electron.app.on('session-created', install);
    const ready = electron.app.whenReady().then(() => install(electron.session.defaultSession));
    return {ready, install, count:() => count};
}

module.exports = {EVIDENCE_SHA256, spki, filterRoots, installNode, inspectChain, decide, installChromium};
