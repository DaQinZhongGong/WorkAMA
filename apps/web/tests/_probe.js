const http = require('http');
const url = process.argv[2] || 'http://host.docker.internal:20204';
http.get(url, r => process.exit(r.statusCode)).on('error', e => { console.error(e.message); process.exit(2); });