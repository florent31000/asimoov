// Mirrors the repo's `docs/*.md` into the Starlight collection so the site
// never carries a second copy. Generated files are gitignored.
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const from = resolve(here, '../../docs');
const to = resolve(here, '../src/content/docs/docs');

const SYNCED = ['contracts.md'];

mkdirSync(to, { recursive: true });

for (const name of readdirSync(from)) {
  if (!SYNCED.includes(name)) continue;
  const raw = readFileSync(join(from, name), 'utf8').replace(/\r\n/g, '\n');
  const [, title, body] = raw.match(/^#\s+(.+?)\n([\s\S]*)$/) ?? [];
  if (!title) throw new Error(`docs/${name}: expected a level-1 heading on line 1`);
  const front = [
    '---',
    `title: ${JSON.stringify(title)}`,
    'description: "The frozen v1 contracts every ASIMOOV workstream builds against."',
    'editUrl: false',
    '---',
    '',
    '<!-- Generated from docs/' + name + ' by scripts/sync-docs.mjs. Do not edit here. -->',
    '',
    '',
  ].join('\n');
  writeFileSync(join(to, name), front + body.trimStart(), 'utf8');
  console.log(`synced docs/${name}`);
}
