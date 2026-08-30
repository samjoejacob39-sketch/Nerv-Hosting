/**
 * File Manager & Backups Component
 *
 * Allows browsing files, in-browser configuration editing (server.properties, etc.),
 * and triggering manual world backups.
 */
(function () {
  'use strict';

  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  var modal = document.getElementById('filemanager-modal');
  var closeBtn = document.getElementById('filemanager-modal-close');
  var serverNameEl = document.getElementById('filemanager-modal-server-name');
  var fileListEl = document.getElementById('filemanager-list');
  var currentDirEl = document.getElementById('filemanager-current-dir');
  var editorContainer = document.getElementById('filemanager-editor-container');
  var editorTextarea = document.getElementById('filemanager-editor');
  var editingFileNameEl = document.getElementById('filemanager-editing-filename');
  var saveBtn = document.getElementById('filemanager-save-btn');
  var closeEditorBtn = document.getElementById('filemanager-close-editor-btn');
  var backupBtn = document.getElementById('filemanager-backup-btn');

  var currentServerId = null;
  var currentDirectory = '/';
  var currentFilePath = null;

  async function loadFiles(serverId, directory) {
    currentDirectory = directory || '/';
    if (currentDirEl) currentDirEl.textContent = currentDirectory;
    if (fileListEl) fileListEl.innerHTML = '<p class="text-xs text-slate-500 italic p-4">Loading directory contents...</p>';
    if (editorContainer) editorContainer.classList.add('hidden');

    try {
      var res = await fetch('/dashboard/server/' + serverId + '/files?directory=' + encodeURIComponent(currentDirectory), {
        headers: { 'Accept': 'application/json' }
      });
      var data = await res.json();
      if (!res.ok || !data.ok) {
        if (fileListEl) fileListEl.innerHTML = '<p class="text-xs text-rose-400 p-4">Error: ' + ((data.error && data.error.message) || 'Failed to load files') + '</p>';
        return;
      }

      var items = data.data.files || [];
      if (fileListEl) {
        fileListEl.innerHTML = '';
        if (currentDirectory !== '/') {
          var upBtn = document.createElement('div');
          upBtn.className = 'cursor-pointer px-4 py-2 text-xs font-semibold text-brand-400 hover:bg-ink-800 rounded-lg flex items-center gap-2';
          upBtn.textContent = '📁 .. (Up one level)';
          upBtn.onclick = function () {
            var parts = currentDirectory.replace(/\/$/, '').split('/');
            parts.pop();
            var parent = parts.join('/') || '/';
            loadFiles(currentServerId, parent);
          };
          fileListEl.appendChild(upBtn);
        }

        if (items.length === 0) {
          fileListEl.innerHTML += '<p class="text-xs text-slate-500 italic p-4">Directory is empty.</p>';
          return;
        }

        items.forEach(function (item) {
          var row = document.createElement('div');
          row.className = 'flex items-center justify-between px-4 py-2.5 text-xs hover:bg-ink-800 rounded-lg transition border border-transparent hover:border-ink-700 cursor-pointer';
          var isFile = item.is_file;
          var name = item.name;
          var size = isFile ? (item.size ? Math.round(item.size / 1024) + ' KB' : '0 KB') : 'Folder';

          row.innerHTML = '<div class="flex items-center gap-2.5"><span class="text-slate-400">' + (isFile ? '📄' : '📁') + '</span><span class="font-bold ' + (isFile ? 'text-white' : 'text-brand-400') + '">' + name + '</span></div><span class="text-[0.65rem] text-slate-500 font-mono">' + size + '</span>';

          row.onclick = function () {
            if (isFile) {
              var fullPath = (currentDirectory === '/' ? '' : currentDirectory) + '/' + name;
              openFile(currentServerId, fullPath, name);
            } else {
              var nextDir = (currentDirectory === '/' ? '' : currentDirectory) + '/' + name;
              loadFiles(currentServerId, nextDir);
            }
          };
          fileListEl.appendChild(row);
        });
      }
    } catch (err) {
      if (fileListEl) fileListEl.innerHTML = '<p class="text-xs text-rose-400 p-4">Network error: ' + err.message + '</p>';
    }
  }

  async function openFile(serverId, filePath, fileName) {
    currentFilePath = filePath;
    if (editingFileNameEl) editingFileNameEl.textContent = fileName || filePath;
    if (editorContainer) editorContainer.classList.remove('hidden');
    if (editorTextarea) {
      editorTextarea.value = 'Loading file contents...';
      editorTextarea.disabled = true;
    }

    try {
      var res = await fetch('/dashboard/server/' + serverId + '/files/content?file=' + encodeURIComponent(filePath), {
        headers: { 'Accept': 'application/json' }
      });
      var data = await res.json();
      if (!res.ok || !data.ok) {
        if (editorTextarea) editorTextarea.value = 'Error loading file: ' + ((data.error && data.error.message) || 'Failed to read');
        return;
      }
      if (editorTextarea) {
        editorTextarea.value = data.data.content || '';
        editorTextarea.disabled = false;
      }
    } catch (err) {
      if (editorTextarea) editorTextarea.value = 'Network error reading file: ' + err.message;
    }
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', async function () {
      if (!currentServerId || !currentFilePath || !editorTextarea) return;
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving...';

      try {
        var res = await fetch('/dashboard/server/' + currentServerId + '/files/save', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-CSRFToken': getCsrfToken()
          },
          body: JSON.stringify({
            file: currentFilePath,
            content: editorTextarea.value
          })
        });
        var data = await res.json();
        if (res.ok && data.ok) {
          alert('File saved successfully!');
        } else {
          alert('Failed to save file: ' + ((data.error && data.error.message) || 'Error'));
        }
      } catch (err) {
        alert('Network error saving file: ' + err.message);
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Changes';
      }
    });
  }

  if (backupBtn) {
    backupBtn.addEventListener('click', async function () {
      if (!currentServerId) return;
      backupBtn.disabled = true;
      backupBtn.textContent = 'Creating Backup...';

      try {
        var res = await fetch('/dashboard/server/' + currentServerId + '/backups', {
          method: 'POST',
          headers: {
            'Accept': 'application/json',
            'X-CSRFToken': getCsrfToken()
          }
        });
        var data = await res.json();
        if (res.ok && data.ok) {
          alert('Backup triggered successfully!');
        } else {
          alert('Backup failed: ' + ((data.error && data.error.message) || 'Error'));
        }
      } catch (err) {
        alert('Network error creating backup: ' + err.message);
      } finally {
        backupBtn.disabled = false;
        backupBtn.textContent = 'Create Backup';
      }
    });
  }

  if (closeEditorBtn && editorContainer) {
    closeEditorBtn.addEventListener('click', function () {
      editorContainer.classList.add('hidden');
      currentFilePath = null;
    });
  }

  if (closeBtn && modal) {
    closeBtn.addEventListener('click', function () {
      modal.classList.add('hidden');
      currentServerId = null;
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-open-filemanager]');
    if (!btn || !modal) return;
    currentServerId = btn.getAttribute('data-open-filemanager');
    var name = btn.getAttribute('data-server-name');
    if (serverNameEl) serverNameEl.textContent = name || '#' + currentServerId;
    modal.classList.remove('hidden');
    loadFiles(currentServerId, '/');
  });
})();
