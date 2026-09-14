/**
 * Frame-trust helpers shared by the screenshot harnesses in `website/scripts/`.
 *
 * A capture harness is only worth reading if a saved frame is EVIDENCE. Two ways
 * a frame lies, and one guard each:
 *
 *   - the surface never rendered, so the frame shows an empty panel or the wrong
 *     one. `assertRendered` refuses to save unless every named node is present
 *     AND carries text, and prints what it matched so the reader can check the
 *     harness photographed what it claims.
 *   - the frame is blank or enormous. `record` measures the PNG and refuses
 *     below a bytes-per-pixel floor or above an edge budget.
 *
 * Each harness owns its fixtures and its probe list; only the judging is shared,
 * which is what keeps a "frame saved" claim meaning the same thing across all of
 * them (and what keeps jscpd from finding the same 25 lines in each one).
 */
import { readFileSync } from 'node:fs'

/** Longest allowed PNG edge — a frame past this is unreadable in a PR body. */
export const MAX_EDGE = 2000
/** Blank-frame floor, in milli-bytes per pixel. A flat fill compresses far below this. */
export const MIN_MBPP = 15

/** PNG dimensions straight from the IHDR — no image library needed. */
export function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

/**
 * One entry for `assertRendered`'s probe list.
 *
 * `attr` reads an attribute instead of the text, for a node whose evidence is
 * an `href` or a `class` (an icon-only button has no text at all).
 */
export function probe(selector, locator, opts = {}) {
  return { selector, locator, min: opts.min, attr: opts.attr }
}

/**
 * Require every probe to match at least `min` nodes AND to carry text.
 *
 * Returns the evidence for `record` to print. Throws rather than returning a
 * verdict: a harness must not be able to save the frame anyway.
 *
 * It deliberately cannot express ABSENCE — a zero-count probe is exactly how it
 * detects a blank surface — so "must not be there" belongs in `assertAbsent`.
 */
export async function assertRendered(name, probes) {
  const found = []
  for (const { selector, locator, min = 1, attr } of probes) {
    const count = await locator.count()
    if (count < min) {
      throw new Error(`frame ${name}: probe \`${selector}\` matched ${count} node(s), need >= ${min} — surface did not render; fix the fixture, do not save the frame`)
    }
    const texts = []
    for (let i = 0; i < Math.min(count, min + 2); i++) {
      const v = attr
        ? await locator.nth(i).getAttribute(attr).catch(() => null)
        : await locator.nth(i).innerText().catch(() => '')
      const t = (v || '').trim()
      if (t) texts.push(`${attr ? `${attr}=` : ''}${t.replace(/\s+/g, ' ').slice(0, 70)}`)
    }
    if (texts.length === 0) {
      throw new Error(`frame ${name}: probe \`${selector}\` matched ${count} node(s) but every one is EMPTY — blank surface; fix the fixture, do not save the frame`)
    }
    found.push({ selector, count, text: texts.join(' ⏐ ') })
  }
  return found
}

/** The absence half: assert a locator matches nothing, and say so on stdout. */
export async function assertAbsent(name, label, locator) {
  const count = await locator.count()
  if (count > 0) {
    throw new Error(`frame ${name}: ${label} matched ${count} node(s) and must match none`)
  }
  console.log(`      asserted ABSENT ${label}`)
}

/** Measure a saved PNG, print it with its evidence, and refuse a bad frame.
 *  Internal to `shotFrame`: a harness that needs a different verdict shape
 *  (`capture-pierre-files-tab.mjs` records extra fields) keeps its own. */
function record(file, evidence) {
  const { w, h } = pngSize(file)
  const bytes = readFileSync(file).length
  const mbpp = Math.round((bytes * 1000) / (w * h))
  const over = w > MAX_EDGE || h > MAX_EDGE
  const blank = mbpp < MIN_MBPP
  console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} milli-bytes/px${over ? `  OVER ${MAX_EDGE}px` : ''}${blank ? `  LIKELY BLANK (< ${MIN_MBPP})` : ''}`)
  for (const e of evidence) console.log(`      asserted ${e.selector}  ×${e.count}  →  ${e.text}`)
  if (over) throw new Error(`frame ${file}: ${w}x${h} exceeds the ${MAX_EDGE}px edge budget`)
  if (blank) throw new Error(`frame ${file}: below the blank-frame floor — re-shoot, do not ship`)
  return { file, w, h }
}

/** assert → screenshot → measure, the sequence every frame goes through. */
export async function shotFrame(locator, outDir, name, probes) {
  const evidence = await assertRendered(name, probes)
  const file = `${outDir}/${name}.png`
  await locator.screenshot({ path: file })
  return record(file, evidence)
}
