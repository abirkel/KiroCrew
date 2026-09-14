/**
 * Screenshot harness for the Remote Crew list's Rename item, source/transport
 * badges, and the two in-flight refusals.
 *
 * Four frames, one per claim a reader has to be able to verify:
 *
 *  1. The row menu beside rows of every kind — an EC2-provisioned crew over SSM,
 *     an EC2-provisioned crew over SSH, and a hand-added SSH crew — so the source
 *     badge and the transport badge can be told apart, and Rename sits above
 *     Edit settings with its own glyph.
 *  2. Rename on a row: the shared edit form under a "Rename <crew>" heading,
 *     Name editable and focused, every other field behind a collapsed
 *     read-only disclosure.
 *  3. The draft refusal: one row holds typed changes, Rename on another row is
 *     refused at THAT row with the save-or-cancel wording.
 *  4. The frozen form: a save is in flight (its PATCH never answers here), so
 *     the live edit form's fields, Save and Cancel are disabled and look it.
 *     Nothing else on the page is locked; leaving the page aborts the request.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures — no gateway, no crews.
 *
 * Usage: npm run build && node scripts/capture-remote-crew-rename-types.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-crew-rename-types'
mkdirSync(OUT, { recursive: true })

const crew = (id, name, extra) => ({
  id, name, ssh_host: '', remote_port: 5476, local_port: 0, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'disconnected', local_port: 0, remote_port: 5476 },
  ...extra,
})

const CREWS = [
  crew('ec2-ssm', 'build-farm', {
    connection_method: 'ssm', ssm_target: 'i-0a1b2c3d4e5f60718', ssm_run_as: 'ec2-user',
    aws_profile: 'dev', aws_region: 'us-west-2', provisioner_id: 'aws_ec2',
  }),
  crew('ec2-ssh', 'gpu-box', { ssh_host: 'gpu-box.internal', provisioner_id: 'aws_ec2' }),
  crew('manual', 'dev-box-1', { ssh_host: 'dev-box-1' }),
  crew('manual-2', 'dev-box-2', { ssh_host: 'dev-box-2', remote_port: 7788 }),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

// Frame 4 needs a save whose request never settles. The route handler simply
// never answers a PATCH once this flag is up.
let holdPatch = false
const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/instances' && method === 'GET') {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ active: true, instances: CREWS, warm_set_cap: 10, sso: SSO }),
    })
    return true
  }
  const one = /^\/api\/instances\/([^/]+)$/.exec(path)
  if (one && method === 'PATCH') {
    if (holdPatch) return true // deliberately left pending
    const found = CREWS.find((c) => c.id === decodeURIComponent(one[1]))
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(found) })
    return true
  }
  if (path === '/api/cloud/launch') {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) })
    return true
  }
  if (path === '/api/cloud/provisioners') {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ provisioners: [{ id: 'aws_ec2', kind: 'aws_ec2', label: 'Amazon EC2', posix_only: true, steps: [] }] }),
    })
    return true
  }
  if (path.startsWith('/api/cloud/')) {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    return true
  }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1200, height: 860 }, deviceScaleFactor: 2 })
const page = await context.newPage()
await stubDashboardApi(page, { extra })
logPageProblems(page)

await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
await page.getByText('dev-box-1', { exact: true }).first().waitFor({ timeout: 20000 })
await page.waitForTimeout(500)

const rowOf = (name) => page.locator('[data-crew-id]').filter({ hasText: name }).first()
const openMenu = async (name) => {
  await page.getByRole('button', { name: `More actions for ${name}` }).click()
  await page.getByRole('menuitem', { name: 'Rename', exact: true }).waitFor({ timeout: 10000 })
  await page.waitForTimeout(300)
}
const listClip = async (padBottom = 20) => {
  const rows = page.locator('[data-crew-id]')
  const first = await rows.first().boundingBox()
  const last = await rows.last().boundingBox()
  const pad = 12
  return {
    x: Math.max(0, first.x - pad), y: Math.max(0, first.y - 48),
    width: Math.min(1200, first.width + 2 * pad), height: Math.min(860, last.y + last.height + padBottom) - Math.max(0, first.y - 48),
  }
}

// ---- Frame 1: badges + the row menu ----------------------------------------
const badgesText = await rowOf('build-farm').innerText()
if (!/EC2/.test(badgesText) || !/SSM/.test(badgesText)) fail(`build-farm row shows neither EC2 nor SSM badge: ${JSON.stringify(badgesText)}`)
const manualText = await rowOf('dev-box-1').innerText()
if (/EC2/.test(manualText)) fail('hand-added dev-box-1 must not carry an EC2 badge')
if (!/SSH/.test(manualText)) fail('hand-added dev-box-1 must carry the SSH badge')
await openMenu('gpu-box')
const items = (await page.getByRole('menuitem').allInnerTexts()).map((s) => s.trim())
console.log('MENU', JSON.stringify(items))
if (items[0] !== 'Rename') fail(`Rename is not the first row-menu item: ${JSON.stringify(items)}`)
await page.screenshot({ path: `${OUT}/01-types-and-row-menu.png`, clip: await listClip(200) })
console.log('wrote 01')

// ---- Frame 2: Rename opens the shared form, other fields behind a closed disclosure
await page.getByRole('menuitem', { name: 'Rename', exact: true }).click()
const renameForm = page.getByRole('group', { name: 'Rename gpu-box' })
await renameForm.waitFor({ timeout: 10000 })
await page.waitForTimeout(300)
const nameBox = renameForm.getByRole('textbox', { name: 'Name', exact: true })
if (!(await nameBox.evaluate((el) => document.activeElement === el))) fail('Name field is not focused under Rename')
if (await nameBox.evaluate((el) => el.readOnly)) fail('Name field must stay editable under Rename')
// The other fields sit inside a <details> that starts closed; the frame shows
// the one-field form with the disclosure summary, not a wall of inputs.
const disclosure = renameForm.locator('details')
if ((await disclosure.count()) !== 1) fail('Rename form must carry exactly one read-only disclosure')
else {
  if (await disclosure.evaluate((el) => el.open)) fail('the read-only disclosure must start closed')
  const summary = (await disclosure.locator('summary').innerText()).trim()
  if (!/Other settings \(read-only\)/.test(summary)) fail(`disclosure summary wording: ${JSON.stringify(summary)}`)
}
await page.screenshot({ path: `${OUT}/02-rename-form-name-only.png`, clip: await listClip(20) })
console.log('wrote 02')
await renameForm.getByRole('button', { name: 'Cancel' }).click()
await renameForm.waitFor({ state: 'hidden', timeout: 5000 })

// ---- Frame 3: draft refusal at the clicked row ----------------------------
await openMenu('dev-box-1')
await page.getByRole('menuitem', { name: 'Edit settings' }).click()
const editForm = page.getByRole('group', { name: 'Edit dev-box-1' })
await editForm.waitFor({ timeout: 10000 })
const editHost = editForm.getByRole('textbox', { name: /SSH host/ })
await editHost.fill('dev-box-1-corrected')
await openMenu('dev-box-2')
await page.getByRole('menuitem', { name: 'Rename', exact: true }).click()
const draftRefusal = rowOf('dev-box-2').getByRole('alert')
await draftRefusal.waitFor({ timeout: 10000 })
const draftText = (await draftRefusal.innerText()).trim()
console.log('DRAFT REFUSAL', JSON.stringify(draftText))
if (!/Save or cancel the open edit before editing another instance/.test(draftText)) fail(`draft refusal wording: ${JSON.stringify(draftText)}`)
if (!(await editForm.isVisible())) fail('the dev-box-1 edit form must stay open across the refused Rename')
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/03-draft-refusal-at-row.png`, clip: await listClip(20) })
console.log('wrote 03')

// ---- Frame 4: the live form freezes during its own save --------------------
holdPatch = true
await editForm.getByRole('button', { name: 'Save changes' }).click()
const savingButton = editForm.getByRole('button', { name: /Saving/ })
await savingButton.waitFor({ timeout: 10000 })
if (!(await savingButton.isDisabled())) fail('Save must be disabled while its request is pending')
if (!(await editHost.isDisabled())) fail('the live edit form fields must be disabled while saving')
const editName = editForm.getByRole('textbox', { name: 'Name', exact: true })
if (!(await editName.isDisabled())) fail('the live edit form name must be disabled while saving')
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/04-saving-form-frozen.png`, clip: await listClip(20) })
console.log('wrote 04')

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
