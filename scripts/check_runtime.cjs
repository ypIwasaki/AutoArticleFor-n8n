'use strict';
// Load the installed native SQLite driver and exercise it without touching disk.
const path = require('node:path');
const { createRequire } = require('node:module');
const runtime = path.resolve(process.argv[2] || path.join(__dirname, '..', 'runtime'));
const localRequire = createRequire(path.join(runtime, 'package.json'));
const sqlite3 = localRequire('sqlite3');
const db = new sqlite3.Database(':memory:', error => {
  if (error) throw error;
  db.get('SELECT 1 AS value', (error, row) => {
    if (error) throw error;
    if (row.value !== 1) throw new Error('SQLite runtime check failed');
    db.close(error => {
      if (error) throw error;
      console.log('SQLite runtime OK');
    });
  });
});
