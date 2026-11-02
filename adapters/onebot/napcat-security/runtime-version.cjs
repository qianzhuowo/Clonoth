'use strict';
// Run only as the main module of an isolated, account-free QQ test container.
const { app } = require('electron');
console.log(JSON.stringify({ kind: 'embedded-runtime', versions: process.versions }));
app.whenReady().then(() => app.exit(0));
setTimeout(() => app.exit(1), 15000).unref();
