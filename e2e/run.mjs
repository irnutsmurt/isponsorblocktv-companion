// End-to-end UI test: first-run NAS workflow on an emulated phone, then layout checks.
// Uses system Chromium (viewport/touch EMULATION, not a physical device) and a FAKE lounge API.
import { chromium } from 'playwright-core'
import fs from 'node:fs'
import path from 'node:path'

const BASE = process.env.BASE || 'http://localhost:8399'
const TOKEN = process.env.SETUP_TOKEN
const LOG = process.env.UPSTREAM_LOG_FILE
const SHOTS = process.env.SHOTS || './shots'
const PASSWORD = 'correct horse battery staple, extra long'
fs.mkdirSync(SHOTS, { recursive: true })
const results = []
const ok = (name, cond, extra = '') => { results.push({ name, ok: !!cond, extra }); console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`) }
const ts = () => new Date().toISOString().replace('T', ' ').replace('Z', '').replace('.', ',').slice(0, 23)
const append = (lines) => fs.appendFileSync(LOG, lines.map((l) => l + '\n').join(''))
const SCREEN_B = 'FAKESCREEN' + 'B'.repeat(54)
const SCREEN_A = 'FAKESCREEN' + 'A'.repeat(54)

async function layoutChecks(page, label) {
  const r = await page.evaluate(() => {
    const doc = document.documentElement
    const overflowX = doc.scrollWidth - doc.clientWidth
    const small = []
    for (const el of document.querySelectorAll('button, .btn, input:not([type=hidden]), select, .nav-link, .bottom-nav a, summary')) {
      const b = el.getBoundingClientRect()
      const style = getComputedStyle(el)
      if (!b.width || !b.height || style.visibility === 'hidden' || el.closest('dialog:not([open])') || el.closest('[hidden]')) continue
      if (el.type === 'checkbox' || el.type === 'radio') {
        const lab = el.closest('label'); const lb = lab ? lab.getBoundingClientRect() : b
        if (lb.height < 44) small.push(`${el.tagName}#${el.id || ''}(${Math.round(lb.width)}x${Math.round(lb.height)})`)
        continue
      }
      if (b.height < 43.5 || b.width < 43.5) small.push(`${el.tagName}.${(el.className || '').toString().split(' ')[0]}:${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 20)}(${Math.round(b.width)}x${Math.round(b.height)})`)
    }
    return { overflowX, small: small.slice(0, 8) }
  })
  ok(`${label}: no horizontal page scroll`, r.overflowX <= 1, `overflow ${r.overflowX}px`)
  ok(`${label}: touch targets ≥44px`, r.small.length === 0, r.small.join(', '))
}

const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] })
try {
  // ---------------- Phone: complete first-TV workflow with touch ----------------
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true,
    userAgent: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36' })
  const p = await phone.newPage()
  await p.goto(BASE)
  await p.getByRole('heading', { name: 'Create the administrator' }).waitFor()
  await layoutChecks(p, 'phone setup page')
  await p.screenshot({ path: path.join(SHOTS, 'phone-01-setup.png'), fullPage: true })
  await p.getByLabel('Setup token').fill('wrong-token-value')
  await p.getByLabel('Username').fill('admin')
  await p.locator('#new-password').fill(PASSWORD)
  await p.locator('#confirm-password').fill(PASSWORD)
  await p.getByRole('button', { name: 'Show password' }).first().tap()
  ok('password visibility toggle', (await p.locator('#new-password').getAttribute('type')) === 'text')
  await p.getByRole('button', { name: 'Create account' }).tap()
  await p.getByText('Setup token is invalid or expired').first().waitFor()
  ok('wrong setup token rejected with explanation', true)
  await p.getByLabel('Setup token').fill(TOKEN)
  await p.getByRole('button', { name: 'Create account' }).tap()
  await p.getByRole('heading', { name: 'Save your recovery code' }).waitFor()
  const code = await p.locator('.code-box').textContent()
  ok('recovery code shown once', /^[A-Z0-9]{5}(-[A-Z0-9]{5}){3}$/.test(code.trim()), code)
  await p.screenshot({ path: path.join(SHOTS, 'phone-02-recovery.png'), fullPage: true })
  await p.getByLabel('I have stored the recovery code somewhere safe').check()
  await p.getByRole('button', { name: 'Continue to setup' }).tap()
  await p.getByRole('heading', { name: 'Overview' }).waitFor()
  await p.getByText('Let’s connect your first TV').first().waitFor()
  await layoutChecks(p, 'phone overview (first run)')
  await p.screenshot({ path: path.join(SHOTS, 'phone-03-overview-firstrun.png'), fullPage: true })
  await p.getByRole('link', { name: 'Start setup' }).tap()
  await p.getByRole('heading', { name: 'Getting started' }).waitFor()
  await p.getByText('The web UI can write config.json').first().waitFor()
  await p.getByLabel('TV code').fill('9999 9999 9999')
  await p.getByRole('button', { name: 'Pair', exact: true }).tap()
  await p.getByText('YouTube did not recognise this code').first().waitFor()
  ok('invalid TV code explained', true)
  await p.getByLabel('TV code').fill('')
  await p.getByLabel('TV code').pressSequentially('123456789012')
  ok('code auto-formatted', (await p.getByLabel('TV code').inputValue()) === '1234 5678 9012')
  await layoutChecks(p, 'phone pairing step (keyboard)')
  await p.screenshot({ path: path.join(SHOTS, 'phone-04-pair-code.png'), fullPage: true })
  await p.getByRole('button', { name: 'Pair', exact: true }).tap()
  await p.getByText('YouTube recognised this TV').first().waitFor()
  ok('paired state distinct from saved', await p.getByText('1. Paired with YouTube').isVisible())
  await p.getByLabel('Device name').fill('Bedroom')
  await p.screenshot({ path: path.join(SHOTS, 'phone-05-pair-name.png'), fullPage: true })
  await p.getByRole('button', { name: 'Save', exact: true }).tap()
  await p.getByText('Saved. Restart iSponsorBlockTV to apply').first().waitFor({ timeout: 20000 })
  ok('without Docker: saved but not reported active', !(await p.getByText('Saved, applied and verified').isVisible()))
  const cfg1 = JSON.parse(fs.readFileSync(path.join(path.dirname(LOG), 'config.json'), 'utf8'))
  ok('config.json created with first TV', cfg1.devices.length === 1 && cfg1.devices[0].name === 'Bedroom' && cfg1.devices[0].screen_id === SCREEN_B)
  await p.screenshot({ path: path.join(SHOTS, 'phone-06-manual-restart.png'), fullPage: true })
  // Simulate the user restarting iSponsorBlockTV from the NAS UI: upstream prints its shutdown, then loads the device.
  append(['Cancelling tasks and exiting...', 'Exited'])
  await p.waitForTimeout(1500)
  append([`${ts()} - iSponsorBlockTV-${SCREEN_B} - INFO - Refreshed auth, lounge id token FAKE`])
  await p.getByText('Saved, applied and verified').first().waitFor({ timeout: 30000 })
  ok('first TV verified active after restart', true)
  await p.screenshot({ path: path.join(SHOTS, 'phone-07-verified.png'), fullPage: true })
  await p.getByRole('button', { name: 'Done' }).tap()

  // Second TV from the Devices page
  await p.getByRole('link', { name: 'Devices' }).tap()
  await p.getByRole('heading', { name: 'Devices' }).waitFor()
  await p.getByRole('button', { name: 'Add a TV' }).first().tap()
  await p.getByLabel('TV code').pressSequentially('111122223333')
  await p.getByRole('button', { name: 'Pair', exact: true }).tap()
  await p.getByText('YouTube recognised this TV').first().waitFor()
  const longName = 'Living room television next to the very long bookshelf by the window'
  await p.getByLabel('Device name').fill(longName)
  await p.getByRole('button', { name: 'Save', exact: true }).tap()
  await p.getByText('Saved. Restart iSponsorBlockTV to apply').first().waitFor({ timeout: 20000 })
  append(['Cancelling tasks and exiting...', 'Exited'])
  await p.waitForTimeout(1500)
  append([`${ts()} - iSponsorBlockTV-${SCREEN_B} - INFO - Refreshed auth, lounge id token FAKE`, `${ts()} - iSponsorBlockTV-${SCREEN_A} - INFO - Refreshed auth, lounge id token FAKE`])
  await p.getByText('Saved, applied and verified').first().waitFor({ timeout: 30000 })
  const cfg2 = JSON.parse(fs.readFileSync(path.join(path.dirname(LOG), 'config.json'), 'utf8'))
  ok('second TV added without overwriting the first', cfg2.devices.length === 2 && cfg2.devices[0].screen_id === SCREEN_B && cfg2.devices[1].screen_id === SCREEN_A)
  await p.getByRole('button', { name: 'Done' }).tap()
  // Activity to show in UI
  append([
    `${ts()} - iSponsorBlockTV-${SCREEN_A} - INFO - Connected to device Living Room TV (${longName})`,
    `${ts()} - iSponsorBlockTV-${SCREEN_A} - INFO - Playing video dQw4w9WgXcQ with 1 segments`,
    `${ts()} - iSponsorBlockTV-${SCREEN_A} - INFO - Skipping segment: seeking to 95.5`,
    `${ts()} - iSponsorBlockTV-${SCREEN_A} - INFO - Ad has started, muting`,
  ])
  await p.waitForTimeout(2500)
  await p.reload()
  await p.getByRole('heading', { name: 'Devices' }).waitFor()
  await p.getByText('Playing').first().waitFor({ timeout: 10000 })
  await layoutChecks(p, 'phone devices (long names)')
  await p.screenshot({ path: path.join(SHOTS, 'phone-08-devices.png'), fullPage: true })
  for (const [label, url, heading] of [['activity', '/activity', 'Activity'], ['channels', '/channels', 'Channels'], ['config', '/config', 'Configuration'], ['diagnostics', '/diagnostics', 'Diagnostics'], ['account', '/account', 'Account & security'], ['overview', '/', 'Overview']]) {
    await p.goto(BASE + url)
    await p.getByRole('heading', { name: heading, exact: true }).waitFor()
    await p.waitForTimeout(800)
    await layoutChecks(p, `phone ${label}`)
    await p.screenshot({ path: path.join(SHOTS, `phone-09-${label}.png`), fullPage: true })
  }
  // Activity details open + live update keeps it open
  await p.goto(BASE + '/activity')
  await p.locator('.feed-item').first().waitFor()
  await p.locator('.feed-item').first().tap()
  const expanded = await p.locator('.feed-item').first().getAttribute('aria-expanded')
  append([`${ts()} - iSponsorBlockTV-${SCREEN_A} - INFO - Ad has ended, unmuting`])
  await p.getByText(/new entr(y|ies): show/).waitFor({ timeout: 15000 })
  ok('live update does not disturb open details', expanded === 'true' && (await p.locator('.feed-item').first().getAttribute('aria-expanded')) === 'true')

  // Config edit with draft surviving reload, conflict detection
  await p.goto(BASE + '/config')
  await p.getByRole('heading', { name: 'Configuration' }).waitFor()
  await p.getByLabel('Intro').check()
  await p.reload()
  await p.getByRole('heading', { name: 'Configuration' }).waitFor()
  ok('unsaved config draft survives reload', await p.getByLabel('Intro').isChecked())
  const cfgPath = path.join(path.dirname(LOG), 'config.json')
  const ext = JSON.parse(fs.readFileSync(cfgPath, 'utf8')); ext.minimum_skip_length = 3; fs.writeFileSync(cfgPath, JSON.stringify(ext, null, 4))
  await p.getByText('config.json changed outside this page').first().waitFor({ timeout: 25000 })
  ok('external config change detected; save blocked', await p.getByRole('button', { name: 'Save', exact: true }).isDisabled())
  await p.screenshot({ path: path.join(SHOTS, 'phone-10-config-conflict.png'), fullPage: true })

  // Landscape phone
  await p.setViewportSize({ width: 844, height: 390 })
  await p.goto(BASE + '/')
  await p.getByRole('heading', { name: 'Overview' }).waitFor()
  await layoutChecks(p, 'phone landscape overview')
  await p.screenshot({ path: path.join(SHOTS, 'phone-11-landscape.png'), fullPage: true })

  // 320 px
  await p.setViewportSize({ width: 320, height: 640 })
  for (const url of ['/', '/devices', '/activity', '/config', '/diagnostics', '/account', '/channels']) {
    await p.goto(BASE + url)
    await p.waitForTimeout(900)
    await layoutChecks(p, `320px ${url}`)
  }
  await p.screenshot({ path: path.join(SHOTS, 'w320-devices.png'), fullPage: true })

  // Theme preference persists
  await p.goto(BASE + '/')
  const themeBtn = p.getByRole('button', { name: /^Theme:/ }).first()
  await themeBtn.tap(); await themeBtn.tap()
  await p.reload()
  ok('dark theme preference persists', (await p.evaluate(() => document.documentElement.getAttribute('data-theme'))) === 'dark')
  await p.screenshot({ path: path.join(SHOTS, 'w320-dark.png'), fullPage: true })

  // Sign out → session invalid
  const cookies = await phone.cookies()
  await p.getByRole('button', { name: 'Sign out' }).first().tap()
  await p.getByRole('heading', { name: 'Sign in' }).waitFor()
  const r = await fetch(BASE + '/api/auth/me', { headers: { cookie: cookies.map((c) => `${c.name}=${c.value}`).join('; ') } })
  ok('logout invalidates server-side session', r.status === 401)
  await phone.close()

  // ---------------- Desktop & tablet ----------------
  for (const [label, vp] of [['desktop', { width: 1440, height: 900 }], ['tablet', { width: 820, height: 1180 }]]) {
    const ctx = await browser.newContext({ viewport: vp })
    const d = await ctx.newPage()
    await d.goto(BASE)
    await d.getByLabel('Username').fill('admin')
    await d.locator('#current-password').fill(PASSWORD)
    await d.getByLabel(/Remember this device/).check()
    await d.keyboard.press('Enter')
    await d.getByRole('heading', { name: 'Overview' }).waitFor()
    const sc = (await ctx.cookies()).find((c) => c.name.includes('isbtv_session'))
    ok(`${label}: remembered cookie is HttpOnly with ~30 day expiry`, sc && sc.httpOnly && sc.expires > Date.now() / 1000 + 29 * 86400)
    for (const url of ['/', '/devices', '/activity', '/channels', '/config', '/diagnostics', '/account']) {
      await d.goto(BASE + url)
      await d.waitForTimeout(900)
      await layoutChecks(d, `${label} ${url}`)
      await d.screenshot({ path: path.join(SHOTS, `${label}-${url === '/' ? 'overview' : url.slice(1)}.png`), fullPage: true })
    }
    // keyboard navigation reaches main nav with visible focus
    await d.goto(BASE + '/')
    await d.getByRole('heading', { name: 'Overview' }).waitFor()
    await d.keyboard.press('Tab')
    const focusedSkip = await d.evaluate(() => document.activeElement?.textContent)
    ok(`${label}: first Tab reaches skip link`, focusedSkip === 'Skip to content', `focused: ${focusedSkip}`)
    const outline = await d.evaluate(() => getComputedStyle(document.activeElement).outlineStyle)
    ok(`${label}: focused element has a visible outline`, outline && outline !== 'none', outline)
    await ctx.close()
  }
  // Reduced motion & no hover dependence are CSS-level; check prefers-reduced-motion disables animation
  const rm = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 800, height: 600 } })
  const rp = await rm.newPage(); await rp.goto(BASE)
  const anim = await rp.evaluate(() => { const s = document.createElement('span'); s.className = 'spinner'; document.body.appendChild(s); return getComputedStyle(s).animationName })
  ok('reduced motion disables animation', anim === 'none')
  await rm.close()
} catch (e) {
  console.error('ERROR', e)
  ok('no unexpected errors', false, String(e).slice(0, 300))
} finally {
  await browser.close()
}
const failed = results.filter((r) => !r.ok)
fs.writeFileSync(path.join(SHOTS, 'results.json'), JSON.stringify(results, null, 2))
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
process.exit(failed.length ? 1 : 0)
