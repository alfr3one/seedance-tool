# Dola Studio Windows (preview)

Run `dist-desktop/win-unpacked/Dola Studio.exe`. Keep the entire folder together;
the executable alone is not portable. Python and Patchright Chromium are bundled.
This preview is not code-signed. The single-file installer is
`dist-desktop/Dola-Studio-Setup-1.0.2-x64.exe`, built with `npm run desktop:installer`.
Copy only this Setup executable to another Windows x64 machine, then install.
The wizard lets you choose a directory and creates Desktop/Start Menu shortcuts.
User data is preserved on uninstall. Exit the app (including tray) and finish or
cancel active tasks before installing an update. The installer does not migrate
CLI data or automatically launch the app after installation.

Data is stored in `%APPDATA%/dola-render-gateway/data`, outside the application.
The App menu opens the actual data directory. Existing CLI accounts, databases,
and secrets are deliberately NOT copied or bundled. Add accounts in the new app;
do not open the same profile from the CLI and desktop at the same time.
The existing proxy default remains `http://127.0.0.1:7890`; configure or disable it
in Settings, then click Lưu proxy. Changes apply to newly opened browsers without
interrupting existing tasks. Kiểm tra kết nối uses a separate empty browser and
does not save settings or log into an account. Authenticated proxies are not yet supported.
Accounts > Tải video hàng loạt (ZIP) includes the latest completed video per current
account. Missing local files are skipped and counted. This may include older batches
if an account has no more recent successful task; it is not an export of all history.

Closing the window offers background mode (tray) or exit. Exit stops the owned
backend; startup uses the existing task recovery behavior. Do not assume an
unverified submission is safe to retry. Logs: `data/desktop-backend.log`.
Login diagnostics: `data/logs/account-login.log` (2 MB, 3 rotated backups).
Use App menu > Mở log đăng nhập account or Mở log backend. Login logs include
timestamp, account, stage and sanitized exception stack, never cookie/page dumps.
Error messages are shown directly in Accounts. Review logs before sharing them;
local filesystem paths can still identify your Windows username.
The backend uses a saved loopback-only port, separate from CLI port 8000. If that
port is occupied, startup fails instead of killing another process or switching
origins and losing form cache. No automatic updates or silent account migration.

Build on Windows with Node/npm and the project's Python virtual environment:

```
npm install
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller tzdata opencv-python
npm run desktop:pack
node desktop/smoke.cjs
```

For development: `npm run desktop`. Packaging explicitly includes only app assets,
backend dependencies and Chromium, not `.env.local`, account profiles or databases.
Electron security follows sandbox/context isolation guidance:
https://www.electronjs.org/docs/latest/tutorial/security
