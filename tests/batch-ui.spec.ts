import { test, expect } from '@playwright/test';
import { createServer, Server } from 'node:http';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

let server: Server;
let baseUrl: string;

test('clears prompts once while preserving failed and active rows across reload', async ({page}) => {
  const accounts=Array.from({length:4},(_,i)=>({name:`acc${i+1}`,scheduling:true,busy:false,used_today:0,limit:10,remaining:10}));
  await page.route('**/api/admin/accounts',r=>r.fulfill({json:{accounts}}));
  await page.route('**/api/admin/stats',r=>r.fulfill({json:{per_day:[],per_account:[]}}));
  await page.route('**/v1/videos/generations',async r=>{
    const name=r.request().postDataJSON().target_account;
    if(name==='acc2')return r.fulfill({status:500,json:{detail:'test failure'}});
    return r.fulfill({json:{id:`video_${name}`,status:'queued'}});
  });
  await page.route('**/v1/videos/video_*',r=>r.fulfill({json:r.request().url().endsWith('acc1')
    ?{status:'completed',video_url:'https://example.com/video.mp4'}:{status:'processing'}}));
  await page.goto(baseUrl);
  await page.getByRole('button',{name:'Batch Gen'}).click();
  await expect(page.locator('[id^="batch_prompt_"]')).toHaveCount(4);
  for(let i=1;i<=4;i++)await page.locator(`#batch_prompt_acc${i}`).fill(`prompt ${i}`);
  await page.locator('#batch_refs_acc1').fill('https://example.com/ref.jpg');
  await page.locator('#batch_dur_acc1').selectOption('15');
  for(let i=1;i<=3;i++)await page.locator(`#batch_run_acc${i}`).click();
  await expect(page.locator('#batch_status_acc1')).toContainText('Done');
  await expect(page.locator('#batch_status_acc2')).toContainText('Failed');
  await page.locator('#batchClearPromptsBtn').click();
  for(const i of [1,4])await expect(page.locator(`#batch_prompt_acc${i}`)).toHaveValue('');
  for(const i of [2,3])await expect(page.locator(`#batch_prompt_acc${i}`)).toHaveValue(`prompt ${i}`);
  await expect(page.locator('#batch_refs_acc1')).toHaveValue('https://example.com/ref.jpg');
  await expect(page.locator('#batch_dur_acc1')).toHaveValue('15');
  await page.reload();
  await page.getByRole('button',{name:'Batch Gen'}).click();
  await expect(page.locator('#batch_prompt_acc1')).toHaveValue('');
  await expect(page.locator('#batch_status_acc2')).toContainText('Failed');
  await page.locator('#batchClearPromptsBtn').click();
  await expect(page.locator('#batch_prompt_acc2')).toHaveValue('prompt 2');
  await expect(page.locator('#batch_prompt_acc3')).toHaveValue('prompt 3');
});

for (const count of [3, 10]) {
  test(`pastes ten numbered prompts into ${count} accounts and caches them`, async ({page}) => {
    const accounts=Array.from({length:count},(_,i)=>({
      name:`acc${i+1}`,scheduling:true,busy:false,used_today:0,limit:10,remaining:10,
    }));
    await page.route('**/api/admin/accounts',r=>r.fulfill({json:{accounts}}));
    await page.route('**/api/admin/stats',r=>r.fulfill({json:{per_day:[],per_account:[]}}));
    let submissions=0;
    await page.route('**/v1/videos/generations',r=>{submissions++;return r.fulfill({json:{}});});
    await page.goto(baseUrl);
    await page.getByRole('button',{name:'Batch Gen'}).click();
    await expect(page.locator('[id^="batch_prompt_"]')).toHaveCount(count);
    await page.locator('#batch_dur_acc1').selectOption('15');
    const text=Array.from({length:10},(_,i)=>`Prompt ${i+1}\tScene ${i+1}\nsecond line ${i+1}`).join('\r\n');
    await page.locator('#batch_paste').evaluate((el,text)=>{
      const data=new DataTransfer();data.setData('text/plain',text);
      el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    },text);
    for(let i=1;i<=count;i++)await expect(page.locator(`#batch_prompt_acc${i}`)).toHaveValue(`Scene ${i}\nsecond line ${i}`);
    await expect(page.locator('#batch_dur_acc1')).toHaveValue('15');
    if(count===3)await expect(page.locator('#batchPasteStatus')).toContainText('7 extra');
    expect(submissions).toBe(0);
    await page.reload();
    await page.getByRole('button',{name:'Batch Gen'}).click();
    await expect(page.locator('#batch_prompt_acc1')).toHaveValue('Scene 1\nsecond line 1');
    await expect(page.locator('#batch_paste')).toHaveValue(text.replace(/\r\n/g,'\n'));
    // Native paste can bypass the clipboard handler: recover on input.
    const replacement=text.replace(/Scene /g,'New scene ');
    await page.locator('#batch_paste').fill(replacement);
    await page.locator('#batch_paste').evaluate(el=>el.dispatchEvent(
      new InputEvent('input',{inputType:'insertFromPaste',bubbles:true})));
    await expect(page.locator('#batch_prompt_acc1')).toHaveValue('New scene 1\nsecond line 1');
    // Explicit recovery when the entire list was pasted into one account row.
    await page.locator('#batch_paste').fill('');
    await page.locator('#batch_prompt_acc1').fill(text);
    await page.locator('#batchDistributeBtn').click();
    await expect(page.locator('#batch_prompt_acc1')).toHaveValue('Scene 1\nsecond line 1');
    await expect(page.locator(`#batch_prompt_acc${count}`)).toHaveValue(`Scene ${count}\nsecond line ${count}`);
  });
}

test.beforeAll(async () => {
  const html = readFileSync(join(process.cwd(), 'web', 'index.html'));
  server = createServer((request, response) => {
    if (request.url === '/' || request.url === '/index.html') {
      response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
      response.end(html);
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Test server did not start');
  baseUrl = `http://127.0.0.1:${address.port}`;
});

test.afterAll(async () => {
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
});

test('submits ten independently configured videos to ten target profiles', async ({page}) => {
  const accounts = Array.from({length: 10}, (_, index) => ({
    name: `acc${index + 1}`,
    email: `acc${index + 1}@example.com`,
    scheduling: true,
    cooling: false,
    rate_limited: false,
    quota_blocked: false,
    busy: false,
    used_today: 0,
    limit: 10,
    remaining: 10,
    credit_balance: null,
  }));
  const submitted: Record<string, unknown>[] = [];

  await page.route('**/api/admin/accounts', route => route.fulfill({json: {accounts}}));
  await page.route('**/api/admin/stats', route => route.fulfill({
    json: {today_completed: 0, today_failed: 0, success_rate: null,
      available_accounts: 10, total_accounts: 10, total_remaining: 100,
      per_day: [], per_account: []}
  }));
  await page.route('**/v1/videos/generations', async route => {
    const body = route.request().postDataJSON();
    submitted.push(body);
    await route.fulfill({json: {id: `video_${body.target_account}`, status: 'queued'}});
  });
  await page.route('**/v1/videos/video_*', route => route.fulfill({
    json: {id: route.request().url().split('/').pop(), status: 'processing'}
  }));

  await page.goto(baseUrl);
  await page.getByRole('button', {name: 'Batch Gen'}).click();
  await expect(page.locator('[id^="batch_prompt_"]')).toHaveCount(10);
  await page.locator('#batch_apikey').fill('test-api-key');

  for (let index = 1; index <= 10; index++) {
    await page.locator(`#batch_prompt_acc${index}`).fill(`prompt ${index}`);
  }
  await page.locator('#batch_refs_acc1').fill('https://example.com/a.jpg\nhttps://example.com/b.jpg');
  await page.locator('#batch_model_acc1').selectOption('seedance-2.5');
  await page.locator('#batch_ratio_acc1').selectOption('16:9');
  await page.locator('#batch_dur_acc1').selectOption('30');

  await page.locator('#batchRunAllBtn').click();
  await expect.poll(() => submitted.length).toBe(10);

  expect(new Set(submitted.map(item => item.target_account))).toEqual(
    new Set(accounts.map(account => account.name))
  );
  expect(submitted.find(item => item.target_account === 'acc1')).toMatchObject({
    prompt: 'prompt 1',
    model: 'seedance-2.5',
    ratio: '16:9',
    duration: 30,
    reference_images: ['https://example.com/a.jpg', 'https://example.com/b.jpg'],
  });
});

test('preserves batch drafts when profiles become busy and after reload', async ({page}) => {
  let accounts = Array.from({length: 2}, (_, index) => ({
    name: `acc${index + 1}`,
    email: `acc${index + 1}@example.com`,
    scheduling: true,
    cooling: false,
    rate_limited: false,
    quota_blocked: false,
    busy: false,
    used_today: 0,
    limit: 10,
    remaining: 10,
    credit_balance: null,
  }));

  await page.route('**/api/admin/accounts', route => route.fulfill({json: {accounts}}));
  await page.route('**/api/admin/stats', route => route.fulfill({
    json: {today_completed: 0, today_failed: 0, success_rate: null,
      available_accounts: 2, total_accounts: 2, total_remaining: 20,
      per_day: [], per_account: []}
  }));

  await page.goto(baseUrl);
  await page.getByRole('button', {name: 'Batch Gen'}).click();
  await expect(page.locator('[id^="batch_prompt_"]')).toHaveCount(2);
  await page.locator('#batch_apikey').fill('cached-api-key');
  await page.locator('#batch_prompt_acc1').fill('persistent prompt');
  await page.locator('#batch_refs_acc1').fill('https://example.com/reference.jpg');
  await page.locator('#batch_model_acc1').selectOption('seedance-2.5');
  await page.locator('#batch_ratio_acc1').selectOption('16:9');
  await page.locator('#batch_dur_acc1').selectOption('15');

  await page.getByRole('button', {name: 'Dashboard'}).click();
  accounts = accounts.map(account => ({...account, busy: true}));
  await page.getByRole('button', {name: 'Batch Gen'}).click();

  await expect(page.locator('[id^="batch_prompt_"]')).toHaveCount(2);
  await expect(page.locator('#batch_prompt_acc1')).toHaveValue('persistent prompt');
  await expect(page.locator('#batch_refs_acc1')).toHaveValue('https://example.com/reference.jpg');
  await expect(page.locator('#batch_model_acc1')).toHaveValue('seedance-2.5');
  await expect(page.locator('#batch_ratio_acc1')).toHaveValue('16:9');
  await expect(page.locator('#batch_dur_acc1')).toHaveValue('15');
  await expect(page.locator('#batch_run_acc1')).toBeDisabled();
  await expect(page.locator('#batchSummary')).toContainText('Ready profiles: 0 / 10');

  await page.reload();
  await page.getByRole('button', {name: 'Batch Gen'}).click();
  await expect(page.locator('#batch_apikey')).toHaveValue('cached-api-key');
  await expect(page.locator('#batch_prompt_acc1')).toHaveValue('persistent prompt');
  await expect(page.locator('#batch_model_acc1')).toHaveValue('seedance-2.5');
  await expect(page.locator('#batch_dur_acc1')).toHaveValue('15');
});
