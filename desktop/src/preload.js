const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('relaydeckDesktop', {
  openAdmin: () => ipcRenderer.invoke('relaydeck:open-admin'),
  showLogs: () => ipcRenderer.invoke('relaydeck:show-logs'),
  getStatus: () => ipcRenderer.invoke('relaydeck:get-status'),
});
