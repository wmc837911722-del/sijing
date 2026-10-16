const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('businessmap', {
  platform: process.platform,
  request: (method, path, body) => ipcRenderer.invoke('businessmap:request', { method, path, body }),
  chooseDirectory: () => ipcRenderer.invoke('businessmap:choose-directory'),
  chooseFile: () => ipcRenderer.invoke('businessmap:choose-file'),
  chooseRepositoryFile: (kind) => ipcRenderer.invoke('businessmap:choose-repository-file', kind),
  windowControl: (action) => ipcRenderer.invoke('businessmap:window-control', action),
  openVault: () => ipcRenderer.invoke('businessmap:open-vault'),
  openExternal: (url) => ipcRenderer.invoke('businessmap:open-external', url),
});
