import type { EngineInterface, Register } from 'claude-code'

type PresenceState = 'idle' | 'thinking' | 'tools' | 'waiting'

// Built in so nobody is asked for anything; a userConfig value overrides it.
const DEFAULT_CLIENT_ID = '1557991133559128104'
const DEFAULT_IMAGE_BASE = 'https://raw.githubusercontent.com/MridulNegi2005/claude-discord-presence/main/assets'
const APP_ID = /^\d{17,20}$/
const ASKS_PERSON = new Set(['AskUserQuestion'])

let runDir = ''
let beat = {
  sessionId: '',
  state: 'idle' as PresenceState,
  startedAt: 0,
  lastActive: 0,
  heartbeat: 0,
  closed: false,
}
let isTurnRunning = false
let toolsInFlight = 0

async function writeBeat($: EngineInterface) {
  if (!runDir) return
  beat.heartbeat = Date.now()
  await $.fs.write(`${runDir}/${beat.sessionId}.json`, JSON.stringify(beat))
}

async function setState($: EngineInterface, state: PresenceState) {
  if (state === beat.state) return
  beat.state = state
  beat.lastActive = Date.now()
  await writeBeat($)
}

async function configDir($: EngineInterface) {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
  return ((await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${home}/.claude`).replace(/\\/g, '/')
}

// One helper per session; a lock inside makes one of them own Discord and the
// rest stand by. The engine ends this child when the session ends.
async function runHelper($: EngineInterface, clientId: string, imageBase: string, projectsDir: string) {
  let announced = false
  try {
    const helper = $.process.spawn({
      argv: ['python', `${$.plugin.root}/scripts/presence.py`, clientId, runDir, projectsDir, imageBase],
    })
    for await (const { text } of helper) {
      $.ui.log(`discord-presence: ${text.trim()}`, { to: 'debug' })
      if (!announced && text.includes('connected')) {
        announced = true
        $.ui.toast('Discord presence connected')
      }
    }
  } catch (err) {
    $.ui.log(`discord-presence: helper failed to start: ${String(err)}`, { to: 'debug' })
  }
}

export const register: Register = (on, options) => {
  const clientId = String(options.clientId || DEFAULT_CLIENT_ID).trim()
  const imageBase = String(options.imageBase || DEFAULT_IMAGE_BASE).trim()

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!APP_ID.test(clientId)) {
      $.ui.log('discord-presence: no Discord Application ID set; presence is off', { to: 'debug' })
      return started
    }

    const dir = await configDir($)
    runDir = `${dir}/discord-presence`
    beat = {
      sessionId: await $.session.id(),
      state: 'idle',
      startedAt: (await $.session.usage()).startedAt,
      lastActive: Date.now(),
      heartbeat: 0,
      closed: false,
    }
    isTurnRunning = false
    toolsInFlight = 0
    await writeBeat($)
    $.clock.every(60_000, () => void writeBeat($))
    void runHelper($, clientId, imageBase, `${dir}/projects`)

    return started
  })

  on('turn.start', async ($, e, next) => {
    isTurnRunning = true
    beat.lastActive = Date.now()
    await setState($, 'thinking')
    await writeBeat($)
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    toolsInFlight += 1
    await setState($, ASKS_PERSON.has(e.tool) ? 'waiting' : 'tools')
    try {
      return await next(e)
    } finally {
      toolsInFlight = Math.max(0, toolsInFlight - 1)
      if (toolsInFlight === 0 && isTurnRunning) await setState($, 'thinking')
    }
  }).catch(($, e, next) => next(e))

  on('classic.PermissionRequest', async ($, e, next) => {
    await setState($, 'waiting')
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      isTurnRunning = false
      toolsInFlight = 0
      await setState($, 'idle')
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    beat.closed = true
    await writeBeat($)
    return next(e)
  })
}
