const STORAGE_KEY = 'scholarflow:reading-text-scale'
const MIN_SCALE = 0.8
const MAX_SCALE = 1.6
const STEP = 0.1

function clampScale(value: number) {
  const stepped = Math.round(value / STEP) * STEP
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(stepped.toFixed(1))))
}

export function getReadingTextScale() {
  const stored = Number(localStorage.getItem(STORAGE_KEY))
  return Number.isFinite(stored) && stored > 0 ? clampScale(stored) : 1
}

export function applyReadingTextScale() {
  document.documentElement.style.setProperty('--reading-text-scale', String(getReadingTextScale()))
}

export function changeReadingTextScale(direction: -1 | 0 | 1) {
  const current = getReadingTextScale()
  const next = direction === 0 ? 1 : clampScale(current + direction * STEP)
  localStorage.setItem(STORAGE_KEY, String(next))
  document.documentElement.style.setProperty('--reading-text-scale', String(next))
  return next
}
