/**
 * Fail if any tracked text file contains a raw control byte.
 *
 * Invisible control characters have caused two real bugs here: a validation regex
 * that silently rejected every food name containing a space, and a documentation
 * line that could not be edited because it did not match what was on screen. Both
 * were invisible on inspection, which is exactly why this is a machine check rather
 * than a review convention.
 *
 * Tabs, newlines and carriage returns are allowed. Everything else below 0x20, plus
 * DEL (0x7F), is rejected — write it as a \u escape or build it with
 * String.fromCharCode instead.
 *
 *   node scripts/check-control-bytes.mjs
 */

import { execSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

/** Binary files have no business being scanned as text. */
const BINARY = /\.(png|jpe?g|gif|ico|webp|woff2?|ttf|eot|pdf|zip|gz)$/i;

const ALLOWED = new Set([0x09, 0x0a, 0x0d]); // tab, LF, CR

function isForbidden(code) {
  return (code < 0x20 && !ALLOWED.has(code)) || code === 0x7f;
}

const files = execSync('git ls-files', { encoding: 'utf8' })
  .split('\n')
  .filter((name) => name && !BINARY.test(name));

const findings = [];

for (const name of files) {
  let text;
  try {
    text = readFileSync(name, 'utf8');
  } catch {
    continue; // unreadable or genuinely binary; not this check's problem
  }
  text.split(/\r?\n/).forEach((line, index) => {
    for (const char of line) {
      const code = char.charCodeAt(0);
      if (isForbidden(code)) {
        const hex = code.toString(16).toUpperCase().padStart(2, '0');
        findings.push(`${name}:${index + 1}  byte 0x${hex}`);
        break; // one report per line is enough to find it
      }
    }
  });
}

if (findings.length > 0) {
  console.error(`Found ${findings.length} line(s) containing raw control bytes:\n`);
  for (const finding of findings) console.error(`  ${finding}`);
  console.error('\nWrite them as \\u escapes, or build them with String.fromCharCode.');
  process.exit(1);
}

console.log(`No raw control bytes in ${files.length} tracked files.`);
