import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { transformSync } from 'rolldown/utils';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// Exercise the real TSX renderer without a browser or a second copy of its logic.
registerHooks({
  resolve(specifier, context, nextResolve) {
    try { return nextResolve(specifier, context); }
    catch (error) {
      if (!specifier.startsWith('.')) throw error;
      for (const extension of ['.ts', '.tsx']) {
        const url = new URL(`${specifier}${extension}`, context.parentURL);
        if (existsSync(fileURLToPath(url))) return { url: url.href, shortCircuit: true };
      }
      throw error;
    }
  },
  load(url, context, nextLoad) {
    if (!/\.tsx?$/.test(url)) return nextLoad(url, context);
    const source = readFileSync(fileURLToPath(url), 'utf8');
    return { format: 'module', shortCircuit: true, source: transformSync(fileURLToPath(url), source).code };
  },
});
const { MarkdownView } = await import(new URL('../src/components/MarkdownView.tsx', import.meta.url).href);
const render = content => renderToStaticMarkup(React.createElement(MarkdownView, { content, onOpen() {} }));

test('legacy source links never render their code locations or an actionable source link', () => {
  const source = 'source://repository-id/commit-id/src/service/PrivateService.java#L10-L20';
  const html = render(`[src/service/PrivateService.java:10](${source})`);
  assert.doesNotMatch(html, /PrivateService|source:\/\/|src\/service|查看引用的源码|<button/);
  assert.match(html, /内部分析依据/);
});

test('wiki source links are blocked even when a label disguises them as business evidence', () => {
  const html = render('[[source://repository-id/commit-id/src/Secret.java#L1|查看退款代码]]');
  assert.doesNotMatch(html, /Secret|repository-id|查看退款代码|<button/);
  assert.match(html, /内部分析依据/);
});

test('normal business references retain their label and open control', () => {
  const note = { id: 'rule', path: '01-Wiki/天梯赛事/规则/退款条件.md', title: '退款条件', tags: [], type: 'rule', updated_at: '' };
  const html = renderToStaticMarkup(React.createElement(MarkdownView, { content: '[[退款条件]]', notes: [note], onOpen() {} }));
  assert.match(html, /<button/);
  assert.match(html, /退款条件/);
  assert.doesNotMatch(html, /内部分析依据/);
});

test('read-only rendering also hides legacy wiki source labels', () => {
  const html = renderToStaticMarkup(React.createElement(MarkdownView, { content: '[[source://repo/rev/src/Private.java|Private.java]]' }));
  assert.doesNotMatch(html, /Private|source:\/\//);
  assert.match(html, /内部分析依据/);
});

test('encoded and mixed-case source references cannot bypass the business renderer', () => {
  for (const content of ['[Private.java](SOURCE://repo/rev/Private.java)', '[[%73ource%3A%2F%2Frepo/rev/Private.java|Private.java]]']) {
    assert.doesNotMatch(render(content), /Private|source:\/\/|<button/i);
  }
});
