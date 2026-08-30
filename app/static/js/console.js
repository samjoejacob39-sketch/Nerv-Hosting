/**
 * Live Console & WebSocket Terminal
 *
 * Connects directly to the Pterodactyl daemon socket to stream live container
 * logs and dispatch administrative console commands in real time.
 */
(function () {
  'use strict';

  var activeSocket = null;
  var currentServerId = null;

  var modal = document.getElementById('console-modal');
  var closeBtn = document.getElementById('console-modal-close');
  var terminalOutput = document.getElementById('console-output');
  var commandForm = document.getElementById('console-command-form');
  var commandInput = document.getElementById('console-command-input');
  var serverNameEl = document.getElementById('console-modal-server-name');

  function appendLog(text, isSystem) {
    if (!terminalOutput) return;
    var line = document.createElement('div');
    line.className = isSystem ? 'text-amber-400 font-bold' : 'text-slate-200';
    line.textContent = text;
    terminalOutput.appendChild(line);
    terminalOutput.scrollTop = terminalOutput.scrollHeight;
  }

  function closeConsole() {
    if (activeSocket) {
      try {
        activeSocket.close();
      } catch (e) {}
      activeSocket = null;
    }
    if (modal) modal.classList.add('hidden');
    currentServerId = null;
  }

  async function openConsole(serverId, serverName) {
    if (!modal) return;
    closeConsole();

    currentServerId = serverId;
    if (serverNameEl) serverNameEl.textContent = serverName || '#' + serverId;
    if (terminalOutput) terminalOutput.innerHTML = '';
    appendLog('Connecting to server console...', true);
    modal.classList.remove('hidden');

    try {
      var res = await fetch('/dashboard/server/' + serverId + '/console-token', {
        headers: { 'Accept': 'application/json' }
      });
      var data = await res.json();
      if (!res.ok || !data.ok) {
        appendLog('Failed to obtain console token: ' + ((data.error && data.error.message) || 'Error'), true);
        return;
      }

      var token = data.data.token;
      var socketUrl = data.data.socket;

      if (!socketUrl || !token) {
        appendLog('Daemon socket URL or token missing from panel response.', true);
        return;
      }

      appendLog('Connecting to daemon WebSocket: ' + socketUrl, true);
      activeSocket = new WebSocket(socketUrl);

      activeSocket.onopen = function () {
        appendLog('Daemon connected. Authenticating...', true);
        activeSocket.send(JSON.stringify({ event: 'auth', args: [token] }));
      };

      activeSocket.onmessage = function (event) {
        try {
          var msg = JSON.parse(event.data);
          if (msg.event === 'auth success') {
            appendLog('--- Authenticated to container console ---', true);
          } else if (msg.event === 'console output') {
            (msg.args || []).forEach(function (str) {
              appendLog(str, false);
            });
          } else if (msg.event === 'status') {
            appendLog('Server state changed: ' + (msg.args && msg.args[0]), true);
          }
        } catch (err) {
          appendLog(event.data, false);
        }
      };

      activeSocket.onerror = function () {
        appendLog('WebSocket error encountered.', true);
      };

      activeSocket.onclose = function () {
        appendLog('Console stream disconnected.', true);
      };

    } catch (err) {
      appendLog('Network error connecting to console: ' + err.message, true);
    }
  }

  if (commandForm && commandInput) {
    commandForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var cmd = commandInput.value.trim();
      if (!cmd || !activeSocket || activeSocket.readyState !== WebSocket.OPEN) return;

      appendLog('> ' + cmd, true);
      activeSocket.send(JSON.stringify({ event: 'send command', args: [cmd] }));
      commandInput.value = '';
    });
  }

  if (closeBtn) closeBtn.addEventListener('click', closeConsole);

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-open-console]');
    if (!btn) return;
    var serverId = btn.getAttribute('data-open-console');
    var serverName = btn.getAttribute('data-server-name');
    openConsole(serverId, serverName);
  });
})();
