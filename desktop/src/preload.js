const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('relaydeckDesktop', {
  getStatus: () => ipcRenderer.invoke('relaydeck:get-status'),
  restartServices: () => ipcRenderer.invoke('relaydeck:restart-services'),
  openLogs: () => ipcRenderer.invoke('relaydeck:open-logs'),
  minimizeWindow: () => ipcRenderer.invoke('relaydeck:minimize-window'),
  closeWindow: () => ipcRenderer.invoke('relaydeck:close-window'),
  onStatusChanged: (callback) => {
    if (typeof callback !== 'function') {
      return () => {};
    }
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('relaydeck:status-changed', listener);
    return () => ipcRenderer.removeListener('relaydeck:status-changed', listener);
  },
});
