// Exercises the real packaged Electron/Python app with empty disposable data.
const {_electron: electron} = require('playwright');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
(async()=>{
  const data = fs.mkdtempSync(path.join(os.tmpdir(),'dola-desktop-test-'));
  const exe = path.resolve('dist-desktop/win-unpacked/Dola Studio.exe');
  let oldOrigin;
  for(let attempt=0;attempt<2;attempt++) {
    const app = await electron.launch({executablePath:exe,env:{...process.env,
      DOLA_DESKTOP_USER_DATA_DIR:path.join(data,'user'),DOLA_DESKTOP_DATA_DIR:path.join(data,'data')},timeout:90000});
    try {
      const page = await app.firstWindow({timeout:90000});
      await page.waitForLoadState();
      await page.getByRole('button',{name:'Batch Gen'}).click();
      const origin = new URL(page.url()).origin;
      if(attempt===0) {
        oldOrigin=origin;
        await page.locator('#batch_paste').fill('saved desktop draft');
        await page.getByRole('button',{name:'Dashboard'}).click();
      } else {
        assert.equal(origin,oldOrigin);
        assert.equal(await page.locator('#batch_paste').inputValue(),'saved desktop draft');
      }
      assert.equal(await page.evaluate(()=>typeof window.require),'undefined');
      const prefs = await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences());
      assert.equal(prefs.sandbox,true); assert.equal(prefs.contextIsolation,true);
      const status=await page.evaluate(async()=>{
        const r=await fetch('/api/admin/accounts');return {status:r.status,body:await r.json()};
      });
      assert.equal(status.status,200); assert.equal(status.body.accounts.length,0);
      const second=spawn(exe,[],{windowsHide:true,env:{...process.env,
        DOLA_DESKTOP_USER_DATA_DIR:path.join(data,'user'),DOLA_DESKTOP_DATA_DIR:path.join(data,'data')}});
      await new Promise((resolve,reject)=>{
        const timeout=setTimeout(()=>{second.kill();reject(new Error('Second instance did not exit'));},10000);
        second.on('error',e=>{clearTimeout(timeout);reject(e);});
        second.on('exit',()=>{clearTimeout(timeout);resolve();});
      });
      assert.equal((await fetch(origin+'/openapi.json')).status,200);
      await app.evaluate(({dialog,BrowserWindow})=>{
        dialog.showMessageBox=async()=>({response:0}); BrowserWindow.getAllWindows()[0].close();
      });
      await new Promise(resolve=>setTimeout(resolve,300));
      assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].isVisible()),false);
      assert.equal((await fetch(origin+'/openapi.json')).status,200);
      await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].show());
      await page.screenshot({path:path.join(data,`desktop-${attempt}.png`)});
    } finally {await app.close();}
    await new Promise(resolve=>setTimeout(resolve,1000));
    await assert.rejects(fetch(oldOrigin+'/openapi.json'));
  }
  console.log('PASS: packaged startup, API, isolation, background, shutdown, restart, draft persistence. Artifacts:',data);
})().catch(e=>{console.error(e);process.exitCode=1;});
