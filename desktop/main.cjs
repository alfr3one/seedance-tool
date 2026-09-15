const {app, BrowserWindow, dialog, Menu, Tray, nativeImage, shell, session} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
if(process.env.DOLA_DESKTOP_USER_DATA_DIR) app.setPath('userData', process.env.DOLA_DESKTOP_USER_DATA_DIR);
let backend, win, tray, quitting = false, origin;
const root = path.join(__dirname, '..');
const dataDir = process.env.DOLA_DESKTOP_DATA_DIR || path.join(app.getPath('userData'), 'data');

function startBackend() {
  fs.mkdirSync(dataDir, {recursive: true});
  const log = fs.createWriteStream(path.join(dataDir, 'desktop-backend.log'), {flags: 'a'});
  const exe = app.isPackaged ? path.join(process.resourcesPath, 'backend', 'dola-backend.exe')
    : path.join(root, '.venv', 'Scripts', 'python.exe');
  const args = app.isPackaged ? [] : ['-u', path.join(root, 'desktop_backend.py')];
  args.push('--data-dir', dataDir);
  const env = {...process.env, PYTHONIOENCODING: 'utf-8'};
  if(app.isPackaged) env.PLAYWRIGHT_BROWSERS_PATH = path.join(process.resourcesPath, 'browsers');
  backend = spawn(exe, args, {cwd: dataDir, env, windowsHide: true, stdio: ['pipe','pipe','pipe']});
  backend.stdout.pipe(log, {end: false}); backend.stderr.pipe(log, {end: false});
  backend.stdin.on('error', () => {});
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Backend chưa sẵn sàng sau 60 giây.')), 60000);
    const lines = readline.createInterface({input: backend.stdout});
    lines.on('line', line => {
      if(!line.startsWith('DOLA_DESKTOP_READY ')) return;
      try {
        const {port} = JSON.parse(line.slice('DOLA_DESKTOP_READY '.length));
        if(!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid backend port');
        clearTimeout(timer); resolve(`http://127.0.0.1:${port}`);
      } catch(e) {clearTimeout(timer); reject(e);}
    });
    backend.once('error', e => {clearTimeout(timer); reject(e);});
    backend.once('exit', code => {
      clearTimeout(timer); log.end();
      reject(new Error(`Backend đã dừng (${code}). Xem ${dataDir}`));
      if(origin && !quitting) {
        dialog.showErrorBox('Backend đã dừng', 'Không tự gửi lại prompt. Hãy mở lại ứng dụng và kiểm tra Task Queue.');
        quitting = true; app.quit();
      }
    });
  });
}

async function requestQuit() {
  const {response} = await dialog.showMessageBox(win, {
    type: 'question', title: 'Đóng Dola Studio',
    message: 'Chạy nền để tiếp tục task, hay thoát ứng dụng?',
    detail: 'Thoát sẽ dừng backend. Lần mở sau ứng dụng khôi phục task theo dữ liệu đã lưu; không đảm bảo một lượt chưa được xác nhận đã gửi thành công.',
    buttons: ['Chạy nền', 'Thoát ứng dụng', 'Quay lại'], defaultId: 0, cancelId: 2
  });
  if(response === 0) win.hide();
  if(response === 1) {quitting = true; app.quit();}
}

if(!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => {if(win) {win.show(); if(win.isMinimized())win.restore(); win.focus();}});
  app.whenReady().then(async () => {
    origin = await startBackend();
    // Backend persists its port to preserve the localStorage origin.
    session.defaultSession.setPermissionRequestHandler((_wc,_permission,cb) => cb(false));
    win = new BrowserWindow({width: 1400,height: 900,show:false,title:'Dola Studio',
      webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true}});
    win.webContents.setWindowOpenHandler(({url}) => {
      if(url.startsWith(origin + '/videos/')) shell.openExternal(url);
      return {action:'deny'};
    });
    win.webContents.on('will-navigate', (event,url) => {if(new URL(url).origin !== origin) event.preventDefault();});
    win.on('close', event => {if(!quitting){event.preventDefault(); requestQuit();}});
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      {label:'Ứng dụng',submenu:[
        {label:'Mở thư mục dữ liệu',click:()=>shell.openPath(dataDir)},
        {label:'Mở log đăng nhập account',click:async()=>{
          const file=path.join(dataDir,'logs','account-login.log');
          if(fs.existsSync(file)) await shell.openPath(file);
          else await dialog.showMessageBox(win,{message:'Chưa có log đăng nhập. Log được tạo khi bạn thêm account.'});
        }},
        {label:'Mở log backend',click:()=>shell.openPath(path.join(dataDir,'desktop-backend.log'))},
        {label:'Hiện cửa sổ',click:()=>win.show()},
        {label:'Thoát',click:requestQuit}]},
      {label:'Chỉnh sửa',submenu:[{role:'undo'},{role:'redo'},{role:'cut'},{role:'copy'},{role:'paste'},{role:'selectAll'}]},
      {label:'Hiển thị',submenu:[{role:'reload'},{role:'resetZoom'},{role:'zoomIn'},{role:'zoomOut'}]}
    ]));
    const icon = nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==');
    tray = new Tray(icon); tray.setToolTip('Dola Studio — chạy nền');
    tray.setContextMenu(Menu.buildFromTemplate([{label:'Mở Dola Studio',click:()=>win.show()},{label:'Thoát',click:requestQuit}]));
    tray.on('double-click',()=>win.show());
    await win.loadURL(origin); win.show();
  }).catch(e => {dialog.showErrorBox('Không thể mở Dola Studio',e.message);quitting=true;app.quit();});
  app.on('before-quit', event => {
    if(backend && backend.exitCode === null) {
      event.preventDefault(); quitting = true;
      const child = backend; backend = null;
      child.once('exit',()=>app.quit()); child.stdin.end('shutdown\n');
    }
  });
  app.on('window-all-closed',()=>{});
}
