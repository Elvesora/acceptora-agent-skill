#!/usr/bin/env node

import { lstatSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const TOKEN_ENV = 'ACCEPTORA_PROJECT_TOKEN';
const TOKEN_PATTERN = /^avt_[0-9A-HJKMNP-TV-Z]{26}_[A-Za-z0-9]{48}$/;
const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..');
const TOKEN_PATH = resolve(PROJECT_ROOT, '.acceptora-env');

function fail() {
  process.stderr.write('Acceptora MCP authentication is unavailable.\n');
  process.exitCode = 1;
}

try {
  const metadata = lstatSync(TOKEN_PATH);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 4_096) {
    fail();
  } else {
    const document = readFileSync(TOKEN_PATH, 'utf8');
    const prefix = `${TOKEN_ENV}=`;
    const values = [];
    let invalid = document.includes('\0');
    for (const line of document.split(/\r\n|\n|\r/)) {
      if (!line.trim() || line.trimStart().startsWith('#')) {
        continue;
      }
      if (!line.startsWith(prefix)) {
        invalid = true;
        break;
      }
      values.push(line.slice(prefix.length));
    }
    if (invalid || values.length !== 1 || !TOKEN_PATTERN.test(values[0])) {
      fail();
    } else {
      process.stdout.write(JSON.stringify({ Authorization: `Bearer ${values[0]}` }));
    }
  }
} catch {
  fail();
}
