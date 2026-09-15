const {_electron:electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
(async()=>{
  const data=fs.mkdtempSync(path.join(os.tmpdir(),'dola-settings-test-'));
  const env={...process.env,DOLA_DESKTOP_USER_DATA_DIR:path.join(data,'user'),DOLA_DESKTOP_DATA_DIR:path.join(data,'data')};
  delete env.DOLA_PROXY;
  for(let pass=0;pass<2;pass++){
    const app=await electron.launch({executablePath:path.resolve('dist-desktop/win-unpacked/Dola Studio.exe'),env,timeout:90000});
    try{
      const page=await app.firstWindow({timeout:90000});await page.waitForLoadState();
      await page.getByRole('button',{name:'Settings'}).click();
      await page.waitForFunction(()=>document.querySelector('#s_info').textContent.includes('Timeout'));
      if(pass===0){
        await page.locator('#s_proxy_enabled').check();
        await page.locator('#s_proxy').fill('http://127.0.0.1:1');
        await page.locator('#s_proxy_test').click();
        await page.waitForFunction(()=>document.querySelector('#s_proxy_status').textContent.includes('ERR_PROXY_CONNECTION_FAILED'));
        await page.locator('#s_proxy_enabled').uncheck();await page.locator('#s_proxy_save').click();
        await page.waitForFunction(()=>document.querySelector('#s_proxy_status').textContent.includes('Đã lưu'));
        assert.match(fs.readFileSync(path.join(data,'data','.env.local'),'utf8'),/DOLA_PROXY=\s*$/);
      }else assert.equal(await page.locator('#s_proxy_enabled').isChecked(),false);
      await page.getByRole('button',{name:'Accounts',exact:true}).click();
      if(pass===0){
      await page.locator('#downloadLatestBtn').click();
      await page.waitForFunction(()=>document.querySelector('#downloadLatestStatus').textContent.includes('Không có file'));
      assert.equal(await page.locator('#downloadLatestBtn').isEnabled(),true);
      fs.mkdirSync(path.join(data,'data','accounts','ziptest'),{recursive:true});
      fs.writeFileSync(path.join(data,'data','downloads','ziptest.mp4'),Buffer.from('test-video'));
      execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c',
        "import sys; from store import TaskStore; s=TaskStore(sys.argv[1]); s.create('ziptest','seedance-2.5','test','9:16',15,account='ziptest'); s.update('ziptest',status='completed',finished_at=1,video_url='http://localhost/videos/ziptest.mp4'); s._conn.close()",
        path.join(data,'data','tasks.db')]);
      }
      const zip=path.join(data,`download-${pass}.zip`);
      await app.evaluate(({session},file)=>{
        globalThis.zipStatus='waiting';
        session.defaultSession.once('will-download',(_event,item)=>{
          item.setSavePath(file);
          item.once('done',(_event,state)=>{globalThis.zipStatus=state;});
        });
      },zip);
      await page.locator('#downloadLatestBtn').click();
      for(let i=0;i<50;i++){
        if(await app.evaluate(()=>globalThis.zipStatus)==='completed')break;
        await new Promise(r=>setTimeout(r,200));
      }
      assert.equal(await app.evaluate(()=>globalThis.zipStatus),'completed');
      execFileSync(path.resolve('.venv/Scripts/python.exe'),['-c',
        "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); assert z.namelist()==['ziptest.mp4']; assert z.read('ziptest.mp4')==b'test-video'",zip]);
    }finally{await app.close();}
  }
  console.log('PASS: proxy test, disable/save/restart, empty ZIP feedback, actual ZIP download/content');
})().catch(e=>{console.error(e);process.exitCode=1;});
