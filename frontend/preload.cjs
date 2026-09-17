const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('autodub', Object.freeze({
  openDirectory: () => ipcRenderer.invoke('dialog:openDirectory'),
  openFiles: () => ipcRenderer.invoke('dialog:openFiles'),
}));
