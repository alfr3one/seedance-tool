// Real packaged Chromium with an intentionally unavailable local proxy.
// Uses disposable data and dummy credentials; never attempts a real login.
const {_electron:electron}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const assert=require('node:assert/strict');
(async()=>{
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'dola-log-test-'));
  const app=await electron.launch({executablePath:path.resolve('dist-desktop/win-unpacked/Dola Studio.exe'),
    env:{...process.env,DOLA_DESKTOP_USER_DATA_DIR:path.join(temp,'user'),
      DOLA_DESKTOP_DATA_DIR:path.join(temp,'data'),DOLA_PROXY:'http://127.0.0.1:1',DOLA_ADMIN_KEY:'',DOLA_API_KEYS:''},timeout:90000});
  try{
    const page=await app.firstWindow({timeout:90000});
    await page.waitForLoadState();
    const result=await page.evaluate(async()=>{
      const response=await fetch('/api/admin/accounts',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:'logtest',email:'dummy@example.com',password:'dummy-password',totp:'dummy-secret'})});
      return response.status;
    });
    assert.equal(result,202);
    let job;
    for(let i=0;i<25;i++){
      job=await page.evaluate(async()=> (await (await fetch('/api/admin/jobs')).json()).jobs.logtest);
      if(job?.status==='failed')break;
      await new Promise(r=>setTimeout(r,500));
    }
    assert.equal(job.status,'failed');
    assert.match(job.error,/ERR_PROXY_CONNECTION_FAILED/);
    await page.getByRole('button',{name:'Accounts',exact:true}).click();
    await page.locator('.task-error').filter({hasText:'ERR_PROXY_CONNECTION_FAILED'}).waitFor();
    const log=fs.readFileSync(path.join(temp,'data','logs','account-login.log'),'utf8');
    assert.match(log,/failed_stage=dola_navigation/);
    for(const value of ['dummy-password','dummy-secret','dummy@example.com'])assert.ok(!log.includes(value));
    const labels=await app.evaluate(({Menu})=>Menu.getApplicationMenu().items[0].submenu.items.map(x=>x.label));
    assert.ok(labels.includes('Mở log đăng nhập account'));
    console.log('PASS packaged login failure: visible UI error, persistent log, redaction, log menu. Logs:',temp);
  }finally{await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
