# Dola Render Gateway

> العربية: دليل التشغيل خطوة بخطوة في [README_AR.md](README_AR.md).

Dola Render Gateway la bo dieu phoi tao video qua Dola AI, gom backend FastAPI, dashboard quan ly account/task/API key va ban desktop Windows dong goi bang Electron.

Project nay duoc toi uu cho luong cong viec thuc te:

- Chay nhieu account Dola song song bang Chromium profile rieng biet.
- Tao video qua dashboard hoac API tuong thich kieu OpenAI.
- Ho tro Seedance 2.5, ty le khung hinh, thoi luong `10s`, `15s`, `30s`.
- Quan ly task queue, retry/resume/cancel, nhom task theo account.
- Luu cau hinh proxy trong Settings cua app, khong can sua `.env.local` thu cong khi dung desktop.
- Ghi log dang nhap account de doc duoc loi khi Chromium mo len roi tat.
- Tai hang loat video hoan thanh moi nhat theo tung account thanh file ZIP.

## Trang thai hien tai

- Ban desktop moi nhat: `1.0.2`.
- File cai dat Windows: [Dola-Studio-Setup-1.0.2-x64.exe](E:/dola-render-gateway/dist-desktop/Dola-Studio-Setup-1.0.2-x64.exe)
- Ban portable/unpacked: [Dola Studio.exe](<E:/dola-render-gateway/dist-desktop/win-unpacked/Dola Studio.exe>)
- Dashboard CLI mac dinh: `http://127.0.0.1:8000/`

Ban desktop la cach nen dung tren may ca nhan hoac may khac. CLI/dev server phu hop khi can sua code, test nhanh hoac debug backend.

## Cau truc chinh

```text
dola-render-gateway/
|-- server.py              # FastAPI API + admin dashboard routes
|-- browser_pool.py        # Dieu phoi account, khoa profile, quota, concurrency
|-- browser.py             # Khoi tao Chromium/Patchright persistent context
|-- video_worker_ui.py     # Tu dong thao tac UI Dola de tao video
|-- video_worker.py        # Poll trang thai va tai video
|-- add_account.py         # Them/login account bang Chromium
|-- store.py               # SQLite tasks, API keys, quota va lich su video
|-- proxy_settings.py      # Validate/test proxy tu Settings
|-- video_archive.py       # Tao ZIP tai hang loat video da hoan thanh
|-- diagnostics.py         # Log dang nhap co redact thong tin nhay cam
|-- config.py              # Cau hinh tu env hoac .env.local
|-- web/index.html         # Dashboard single-page
|-- desktop/               # Electron shell va script dong goi
|-- extensions/dola30/     # Extension dung cho luong Create Videos/30s
|-- accounts/              # Chromium profile khi chay CLI
|-- downloads/             # Video tai ve khi chay CLI
|-- tasks.db               # SQLite task DB khi chay CLI
`-- pool_usage.db          # SQLite quota/account usage khi chay CLI
```

## Chay bang app Windows

Dung file cai dat:

```text
E:\dola-render-gateway\dist-desktop\Dola-Studio-Setup-1.0.2-x64.exe
```

Luu y quan trong:

- App chua code-sign, nen Windows/Smart App Control co the can cho phep thu cong.
- Khi update, hay thoat app hoan toan, ke ca icon tray, va nen cancel/doi xong task dang chay.
- Installer khong tu copy account, database hay secret tu ban CLI. Moi may se co data rieng.
- Neu copy sang may khac, chi can copy file `Dola-Studio-Setup-1.0.2-x64.exe`, khong can copy ca repo.
- User data desktop nam ngoai thu muc cai dat tai `%APPDATA%\dola-render-gateway\data`.
- Khi go cai dat, data nguoi dung duoc giu lai.

Trong app co menu mo nhanh:

- Thu muc data desktop.
- Log backend: `%APPDATA%\dola-render-gateway\data\desktop-backend.log`.
- Log dang nhap account: `%APPDATA%\dola-render-gateway\data\logs\account-login.log`.

Desktop backend dung port loopback rieng luu trong file `.desktop-port`, khong dung truc tiep port CLI `8000`. Neu port desktop da bi process khac chiem, app se bao loi thay vi tu kill process do.

## Proxy

Mac dinh project van co proxy `http://127.0.0.1:7890` de phu hop moi truong cu. Neu may khong dung proxy/VPN local nay, vao `Settings` trong app va tat proxy, roi bam luu.

Quy tac proxy hien tai:

- Ho tro `http`, `https`, `socks5`.
- Can co host va port ro rang, vi du `http://127.0.0.1:7890`.
- Chua ho tro proxy co username/password.
- Nut kiem tra proxy chi mo mot Chromium trong sach de truy cap Dola, khong login account va khong luu cau hinh.
- Thay doi proxy chi ap dung cho browser moi mo; task dang chay khong bi cat ngang.

Khi chay CLI, co the cau hinh trong [`.env.local`](E:/dola-render-gateway/.env.local):

```ini
DOLA_PROXY=
```

De trong nghia la khong dung proxy. Khong dat chu `null`.

## Them account

Trong dashboard/app, vao tab `Accounts` va them account Google/Dola. Moi account co mot Chromium profile rieng.

Neu luc login Chromium mo len roi tu tat, hay xem:

```text
%APPDATA%\dola-render-gateway\data\logs\account-login.log
```

Log nay ghi stage loi, exception va stack da redact email/password/token/cookie. Duong dan may local van co the lo ten user Windows, nen nen kiem tra truoc khi gui cho nguoi khac.

## Tao video

Dashboard co cac luong chinh:

- `Generate Video`: tao le mot video.
- `Batch Gen`: dien prompt cho nhieu account va chay song song.
- `Task Queue`: xem tien do, cancel, retry, resume, clear failed.
- `Accounts`: quan ly account va tai video hang loat.
- `API Keys`: tao API key cho client ben ngoai.
- `Settings`: concurrency, proxy va mot so cau hinh runtime.

Ghi chu thoi luong/model:

- `15s` chay o chat Dola binh thuong, chuyen sang Pro/Seedance 2.5; khong vao Create Videos.
- `10s` va `30s` dung luong Create Videos.
- Prompt gui cho Dola duoc bo sung tien to `tao video:` theo luong automation de Dola hieu dung yeu cau.
- Moi account chi co mot worker dung profile tai mot thoi diem, tranh loi khoa Chromium profile.

## Tai video hang loat

Trong tab `Accounts`, nut tai hang loat tao file ZIP gom video hoan thanh moi nhat cua moi account hien co.

Chi tiet hanh vi:

- Moi account toi da mot video moi nhat co status completed va file local con ton tai.
- Account chua co video hoan thanh se bi bo qua va duoc tinh vao so skipped.
- Neu batch moi nhat cua account bi failed/chua xong, ZIP co the lay video thanh cong cu hon cua account do.
- Day khong phai chuc nang export toan bo lich su.

## Chay nhanh bang script

Chi can Python 3.10+ (khuyen dung 3.12), khong can Node/npm:

- Windows: double-click `start-windows.bat`.
- macOS/Linux: `bash start-mac-linux.sh`.

Lan chay dau, script tao `.venv`, cai `requirements.txt` va Patchright Chromium, tao `.env.local` khong dung proxy neu chua co, roi mo dashboard `http://127.0.0.1:8000/` (chi nghe tren `127.0.0.1`). Cac lan sau chi khoi dong server; dependency duoc cai lai khi `requirements.txt` thay doi. Doi port: `start-windows.bat 8001` hoac `bash start-mac-linux.sh 8001`.

Huong dan tieng Arab: [README_AR.md](README_AR.md).

## Chay CLI/dev server

Yeu cau:

- Windows x64.
- Python trong `.venv` cua project.
- Node/npm neu can chay hoac build desktop.

Cai dependency neu can tao moi moi truong:

```powershell
cd E:\dola-render-gateway
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m patchright install chromium
npm install
```

Chay backend CLI:

```powershell
cd E:\dola-render-gateway
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
```

Mo dashboard:

```text
http://127.0.0.1:8000/
```

Neu gap loi port:

```text
[Errno 10048] error while attempting to bind on address ('0.0.0.0', 8000)
```

nghia la da co process khac dang dung port `8000`. Tat server cu hoac chay port khac, vi du:

```powershell
.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8001
```

## API client

Backend co API tao video tuong thich dang OpenAI:

```http
POST /v1/videos/generations
GET  /v1/videos/{task_id}
GET  /videos/{file}
```

Payload tao video co cac truong chinh:

- `prompt`: noi dung prompt.
- `model`: `seedance-2.0` hoac `seedance-2.5`.
- `duration`: `10s`, `15s`, `30s`.
- `ratio`: vi du `9:16`, `16:9`.
- `reference_image_urls`: danh sach URL anh tham chieu, neu co.

Neu chua cau hinh API key nao, backend co the chay che do local/dev. Khi da tao key trong dashboard hoac dat `DOLA_API_KEYS`, client can gui:

```http
Authorization: Bearer <api-key>
```

## Cau hinh moi truong

`config.py` doc `.env.local` mac dinh trong thu muc project. Desktop dung file rieng tai `%APPDATA%\dola-render-gateway\data\.env.local`.

Bien hay dung:

```ini
DOLA_PROXY=http://127.0.0.1:7890
DOLA_API_KEYS=
DOLA_ADMIN_KEY=
DOLA_MAX_CONCURRENCY=10
DOLA_MAX_PENDING_TASKS=100
DOLA_VIDEO_TIMEOUT=300
DOLA_CONVERSATION_ID_TIMEOUT=45
DOLA_DB_PATH=tasks.db
DOLA_DOWNLOAD_DIR=downloads
DOLA_HEADLESS=1
DOLA_EXTENSION_ENABLED=1
DOLA_LIMIT_RESET_TZ=Asia/Tokyo
```

Khi dung app Windows, nen sua proxy trong tab Settings thay vi sua `.env.local`.

## Build desktop

Build ban portable/unpacked:

```powershell
cd E:\dola-render-gateway
npm run desktop:pack
```

Build installer NSIS:

```powershell
cd E:\dola-render-gateway
npm run desktop:installer
```

Output chinh:

- `dist-desktop\win-unpacked\Dola Studio.exe`
- `dist-desktop\Dola-Studio-Setup-1.0.2-x64.exe`

Thu muc `win-unpacked` phai duoc giu nguyen ca folder; file `.exe` ben trong khong portable rieng le. Installer la file nen copy sang may khac.

## Test

Mot so lenh test da dung cho project:

```powershell
.venv\Scripts\python.exe -m pytest tests
npx playwright test
node desktop\smoke.cjs
node desktop\settings-smoke.cjs
node desktop\log-smoke.cjs
```

Tuy bo moi truong, co the can chay build desktop truoc khi chay cac smoke test desktop.

## Du lieu va an toan

- Account profile CLI nam trong `accounts/`.
- Video CLI nam trong `downloads/`.
- Database CLI gom `tasks.db` va `pool_usage.db`.
- Data desktop nam trong `%APPDATA%\dola-render-gateway\data`.
- Khong nen mo cung mot profile account dong thoi tu CLI va desktop.
- Khong nen retry tu dong khi submission chua duoc xac minh, vi co the tao trung task tren Dola.
- Log da redact thong tin nhay cam, nhung van nen doc lai truoc khi chia se.

## Tai lieu lien quan

- [desktop/README.md](E:/dola-render-gateway/desktop/README.md): ghi chu rieng cho ban desktop Windows.
- [DEVELOPMENT.md](E:/dola-render-gateway/DEVELOPMENT.md): kien truc va pipeline ky thuat.
- [HUONG_DAN_CHAY_PROJECT.md](E:/dola-render-gateway/HUONG_DAN_CHAY_PROJECT.md): ghi chu van hanh Windows cu.
- [PRO_PIPELINE_PLAN.md](E:/dola-render-gateway/PRO_PIPELINE_PLAN.md): plan pipeline Pro nhieu prompt/chat.

## License

Project dung cho muc dich noi bo, nghien cuu va kiem thu.
