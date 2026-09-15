const {spawnSync} = require('node:child_process');
const path = require('node:path');
const root = path.join(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
function run(args, extraEnv = {}) {
  const result = spawnSync(python, args, {cwd: root, stdio: 'inherit', env: {...process.env,...extraEnv}});
  if(result.error) throw result.error;
  if(result.status !== 0) process.exit(result.status || 1);
}
run(['-m','PyInstaller','--noconfirm','--onedir','--name','dola-backend',
  '--distpath','.desktop-build/dist','--workpath','.desktop-build/work','--specpath','.desktop-build',
  '--paths','.', '--add-data',`${path.join(root,'web')};web`,
  '--add-data',`${path.join(root,'extensions','dola30')};extensions/dola30`,
  '--collect-all','patchright','--collect-all','tzdata','desktop_backend.py']);
run(['-m','patchright','install','chromium'], {PLAYWRIGHT_BROWSERS_PATH:path.join(root,'.desktop-build','browsers')});
