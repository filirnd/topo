const { app, BrowserWindow, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const https = require('https');
const path = require('path');

const REPOSITORY = 'filirnd/topo';
const LOCAL_HOST = '127.0.0.1';

let backend = null;
let mainWindow = null;

function backendCommand() {
  if (app.isPackaged) {
    return {
      command: path.join(process.resourcesPath, 'backend', 'tapo-backend.exe'),
      args: [],
      cwd: process.resourcesPath,
      ffmpeg: path.join(process.resourcesPath, 'backend', 'ffmpeg.exe'),
    };
  }

  const projectRoot = path.resolve(__dirname, '..');
  return {
    command: process.env.TOPO_BACKEND || 'python',
    args: [path.join(projectRoot, 'tapo_cloud_app.py')],
    cwd: projectRoot,
    ffmpeg: process.env.TOPO_FFMPEG || 'ffmpeg',
  };
}

function openExternal(uri) {
  if (uri && /^https?:\/\//i.test(uri)) shell.openExternal(uri);
}

function applicationIcon() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'assets', 'topo.png')
    : path.join(__dirname, '..', 'data', 'io.github.filirnd.topo.png');
}

function createWindow(port) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    title: 'Topo - desktop app for Tapo camera',
    icon: applicationIcon(),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.webContents.on('will-navigate', (event, uri) => {
    if (!uri.startsWith(`http://${LOCAL_HOST}:`)) {
      event.preventDefault();
      openExternal(uri);
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.loadURL(`http://${LOCAL_HOST}:${port}/?ui=${Date.now()}`);
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    setTimeout(checkForUpdate, 1800);
  });
}

function startBackend() {
  const runtime = backendCommand();
  const environment = {
    ...process.env,
    TOPO_FFMPEG: runtime.ffmpeg,
    PYTHONUNBUFFERED: '1',
  };
  backend = spawn(runtime.command, [...runtime.args, '--host', LOCAL_HOST, '--port', '0'], {
    cwd: runtime.cwd,
    env: environment,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let output = '';
  let windowCreated = false;
  const handleOutput = (chunk) => {
    output += chunk.toString();
    const match = output.match(/available at http:\/\/127\.0\.0\.1:(\d+)/);
    if (match && !windowCreated) {
      windowCreated = true;
      createWindow(Number(match[1]));
    }
    if (output.length > 8192) output = output.slice(-4096);
  };
  backend.stdout.on('data', handleOutput);
  backend.stderr.on('data', (chunk) => console.error(`[Topo backend] ${chunk}`));
  backend.once('error', (error) => {
    dialog.showErrorBox('Topo could not start', error.message);
    app.quit();
  });
  backend.once('exit', (code) => {
    if (!windowCreated && code !== 0) {
      dialog.showErrorBox('Topo could not start', `The cloud backend exited with code ${code}.`);
      app.quit();
    }
  });
}

function compareVersions(left, right) {
  const parse = (value) => String(value).replace(/^v/i, '').split('.').map((part) => Number.parseInt(part, 10) || 0);
  const a = parse(left);
  const b = parse(right);
  for (let index = 0; index < 3; index += 1) {
    if ((a[index] || 0) !== (b[index] || 0)) return (a[index] || 0) - (b[index] || 0);
  }
  return 0;
}

function fetchLatestRelease() {
  return new Promise((resolve, reject) => {
    const request = https.get(`https://api.github.com/repos/${REPOSITORY}/releases/latest`, {
      headers: { 'User-Agent': 'Topo-Windows-App', Accept: 'application/vnd.github+json' },
    }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        if (response.statusCode !== 200) return reject(new Error(`GitHub returned HTTP ${response.statusCode}`));
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    });
    request.setTimeout(8000, () => request.destroy(new Error('GitHub update check timed out')));
    request.on('error', reject);
  });
}

async function checkForUpdate() {
  if (!app.isPackaged || !mainWindow) return;
  try {
    const release = await fetchLatestRelease();
    if (release.draft || release.prerelease || compareVersions(release.tag_name, app.getVersion()) <= 0) return;
    const installer = (release.assets || []).find((asset) => /\.exe$/i.test(asset.name));
    if (!installer) return;
    const choice = await dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: 'Topo update available',
      message: `Topo ${release.tag_name} is available.`,
      detail: 'Download the installer from GitHub and run it to update Topo.',
      buttons: ['Download update', 'View release', 'Later'],
      defaultId: 0,
      cancelId: 2,
    });
    if (choice.response === 0) openExternal(installer.browser_download_url);
    if (choice.response === 1) openExternal(release.html_url);
  } catch (error) {
    console.warn(`[Topo update] ${error.message}`);
  }
}

app.whenReady().then(startBackend);
app.on('before-quit', () => {
  if (backend && !backend.killed) backend.kill();
});
app.on('window-all-closed', () => app.quit());
