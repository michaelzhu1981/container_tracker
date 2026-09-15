const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const bridge = fs.readFileSync(path.join(__dirname, '..', 'chrome_bridge.js'), 'utf8');

function fixture() {
  let next = 10;
  const windows = [];
  const processes = [];
  const options = {permission: false, invalidCreationReply: true};
  function makeTab(props = {}) {
    const id = String(next++);
    let url = props.url || 'chrome://newtab/';
    const t = {id: () => id, execute: () => {
      if (options.permission) throw new Error('通过 AppleScript 执行 JavaScript 的功能已关闭。允许 Apple 事件中的 JavaScript');
      return JSON.stringify(url);
    }};
    Object.defineProperty(t, 'url', {get: () => () => url, set: v => {url = v;}});
    return t;
  }
  function makeWindow() {
    const id = String(next++);
    const tabs = [makeTab()];
    const w = {id: () => id, tabs: () => tabs, close: () => windows.splice(windows.indexOf(w), 1)};
    w.tabs.byId = id => tabs.find(t => t.id() === id);
    w.tabs.push = t => {tabs.push(t); t.close = () => tabs.splice(tabs.indexOf(t), 1);};
    return w;
  }
  const app = {running: () => true, windows: () => windows, Window: makeWindow, Tab: makeTab, activate: () => {}};
  app.windows.byId = id => windows.find(w => w.id() === id);
  app.windows.push = w => {
    windows.unshift(w);
    if (options.invalidCreationReply) throw new Error('索引错误。');
  };
  const sandbox = {Application: pid => {processes.push(pid); assert.equal(pid, 104); return app;}, delay: () => {}};
  vm.createContext(sandbox);
  vm.runInContext(bridge, sandbox);
  const call = (action, args = {}) => JSON.parse(sandbox.run([JSON.stringify({pid: 104, action, ...args})]));
  return {call, app, options, processes};
}

test('creation recovers an invalid native reply without opening twice', () => {
  const f = fixture();
  const result = f.call('new_window', {url: 'https://www.oocl.com/entry'});
  assert.equal(result.ok, true);
  assert.equal(result.value.tab.url, 'https://www.oocl.com/entry');
  assert.equal(f.app.windows().length, 1);
  assert.equal(typeof result.value.tab.id, 'number');
  assert.deepEqual(f.processes, [104]);
});

test('redirects and front-window changes preserve the selected tab', () => {
  const f = fixture();
  const first = f.call('new_window', {url: 'https://www.oocl.com/entry'}).value;
  const target = {window_id: first.id, tab_id: first.tab.id};
  const result = f.call('new_tab', {...target, url: 'https://www.oocl.com/ExpressLink'}).value;
  target.tab_id = result.id;
  f.app.windows.byId(String(first.id)).tabs.byId(String(result.id)).url = 'https://www.cargosmart.com/results';
  const foreign = f.call('new_window', {url: 'https://www.cargosmart.com/results'}).value;
  const read = f.call('evaluate', {...target, script: '1'});
  assert.equal(read.value, '"https://www.cargosmart.com/results"');
  assert.equal(f.call('close_window', target).ok, true);
  assert.equal(f.app.windows().length, 1);
  assert.equal(Number(f.app.windows()[0].id()), foreign.id);
});

test('a missing tab cannot fall back to another result in the same window', () => {
  const f = fixture();
  const w = f.call('new_window', {url: 'https://www.oocl.com/entry'}).value;
  const result = f.call('evaluate', {window_id: w.id, tab_id: 9999, script: '1'});
  assert.equal(result.code, 'TAB_NOT_FOUND');
});

test('explicit Chinese Chrome permission error is classified separately', () => {
  const f = fixture();
  const w = f.call('new_window', {url: 'https://www.oocl.com/entry'}).value;
  f.options.permission = true;
  const result = f.call('evaluate', {window_id: w.id, tab_id: w.tab.id, script: '1'});
  assert.equal(result.code, 'BROWSER_PERMISSION');
});
