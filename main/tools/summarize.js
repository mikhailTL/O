'use strict';
// Turns Claude Code tool calls into a pet mood + a short, friendly line for speech bubbles.

const path = require('path');

function host(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return String(url || '').slice(0, 40); }
}

function clip(s, n = 60) {
  s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function base(p) {
  return p ? path.basename(String(p)) : 'a file';
}

const BROWSER = {
  browser_navigate: (i) => `Opening ${host(i.url)}`,
  browser_navigate_back: () => 'Going back a page',
  browser_navigate_forward: () => 'Going forward a page',
  browser_click: (i) => `Clicking ${clip(i.element || 'something', 40)}`,
  browser_type: (i) => `Typing into ${clip(i.element || 'a field', 40)}`,
  browser_fill_form: () => 'Filling in a form',
  browser_select_option: (i) => `Picking an option in ${clip(i.element || 'a list', 40)}`,
  browser_hover: (i) => `Hovering over ${clip(i.element || 'something', 40)}`,
  browser_drag: () => 'Dragging something around',
  browser_press_key: (i) => `Pressing ${clip(i.key || 'a key', 20)}`,
  browser_snapshot: () => 'Peering at the page',
  browser_take_screenshot: () => 'Taking a snapshot',
  browser_wait_for: (i) => (i.text ? `Waiting for "${clip(i.text, 30)}"` : 'Waiting a moment'),
  browser_tabs: (i) => (i.action === 'new' ? 'Opening a new tab' : i.action === 'close' ? 'Closing a tab' : 'Juggling tabs'),
  browser_tab_new: () => 'Opening a new tab',
  browser_tab_close: () => 'Closing a tab',
  browser_tab_select: () => 'Switching tabs',
  browser_close: () => 'Closing the browser',
  browser_resize: () => 'Resizing the browser',
  browser_evaluate: () => 'Running a little page script',
  browser_file_upload: () => 'Uploading a file',
  browser_handle_dialog: () => 'Dealing with a pop-up',
  browser_console_messages: () => 'Reading the page console',
  browser_network_requests: () => 'Checking network requests',
  browser_install: () => 'Installing a browser',
  browser_pdf_save: () => 'Saving a PDF',
};

/**
 * @returns {{ mood: string, summary: string }}
 */
function describeTool(name, input) {
  const i = input || {};
  const n = String(name || '');

  if (n.startsWith('mcp__playwright__')) {
    const short = n.slice('mcp__playwright__'.length);
    const f = BROWSER[short];
    return { mood: 'browsing', summary: f ? f(i) : `Browser: ${short.replace(/^browser_/, '').replace(/_/g, ' ')}` };
  }

  switch (n) {
    case 'Bash':
      return { mood: 'working', summary: i.description ? clip(i.description) : `Running: ${clip(i.command, 50)}` };
    case 'PowerShell':
      return { mood: 'working', summary: `Running: ${clip(i.command, 50)}` };
    case 'Write':
      return { mood: 'working', summary: `Writing ${base(i.file_path)}` };
    case 'Edit':
    case 'MultiEdit':
      return { mood: 'working', summary: `Editing ${base(i.file_path)}` };
    case 'NotebookEdit':
      return { mood: 'working', summary: `Editing notebook ${base(i.notebook_path)}` };
    case 'Read':
      return { mood: 'thinking', summary: `Reading ${base(i.file_path)}` };
    case 'Glob':
      return { mood: 'thinking', summary: `Looking for ${clip(i.pattern, 30)}` };
    case 'Grep':
      return { mood: 'thinking', summary: `Searching for "${clip(i.pattern, 30)}"` };
    case 'LS':
      return { mood: 'thinking', summary: 'Looking around a folder' };
    case 'WebSearch':
      return { mood: 'browsing', summary: `Searching the web: ${clip(i.query, 40)}` };
    case 'WebFetch':
      return { mood: 'browsing', summary: `Fetching ${host(i.url)}` };
    case 'mcp__o__see_screen':
      return { mood: 'browsing', summary: 'Peeking at your screen' };
    case 'TodoWrite':
      return { mood: 'thinking', summary: 'Making a plan' };
    case 'Task':
    case 'Agent':
      return { mood: 'working', summary: `Calling in a helper${i.description ? ': ' + clip(i.description, 40) : ''}` };
    default:
      if (n.startsWith('mcp__')) {
        const parts = n.split('__');
        return { mood: 'working', summary: `Using ${parts[1]}: ${(parts[2] || '').replace(/_/g, ' ')}` };
      }
      return { mood: 'working', summary: `Using ${n}` };
  }
}

/** Longer, explicit text for permission cards. */
function describePermission(name, input) {
  const i = input || {};
  switch (name) {
    case 'Bash':
    case 'PowerShell':
      return `Run this command?\n${clip(i.command, 400)}`;
    case 'Write':
      return `Create or overwrite ${i.file_path || 'a file'}?`;
    case 'Edit':
    case 'MultiEdit':
      return `Edit ${i.file_path || 'a file'}?`;
    case 'NotebookEdit':
      return `Edit notebook ${i.notebook_path || ''}?`;
    default:
      return `Allow ${describeTool(name, input).summary}?`;
  }
}

module.exports = { describeTool, describePermission, clip, host };
