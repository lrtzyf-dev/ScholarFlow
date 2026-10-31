export function tableText(rows: string[][], format: 'csv' | 'tsv') {
  const delimiter = format === 'csv' ? ',' : '\t'
  return rows.map(row => row.map(value => {
    // Spreadsheet applications can execute formula-like CSV cells on opening.
    if (format === 'csv' && /^\s*[=+@-]/.test(value) && !/^\s*-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*$/.test(value)) value = `'${value}`
    if (value.includes(delimiter) || /["\r\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`
    return value
  }).join(delimiter)).join('\r\n')
}

export async function copyAnswerText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    return
  } catch { /* Some desktop webviews expose only the legacy clipboard API. */ }
  const active = document.activeElement as HTMLElement | null
  const editing = active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement ? active : null
  const inputSelection = editing && editing.selectionStart !== null ? {
    start: editing.selectionStart, end: editing.selectionEnd!, direction: editing.selectionDirection!,
    top: editing.scrollTop, left: editing.scrollLeft,
  } : null
  const selection = window.getSelection()
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : []
  const input = document.createElement('textarea')
  input.value = text
  input.setAttribute('readonly', '')
  input.style.cssText = 'position:fixed;left:-10000px;top:0;opacity:0'
  document.body.appendChild(input)
  try {
    input.select()
    if (!document.execCommand('copy')) throw new Error('Clipboard unavailable')
  } finally {
    input.remove()
    active?.focus({ preventScroll: true })
    if (selection) {
      selection.removeAllRanges()
      for (const range of ranges) selection.addRange(range)
    }
    if (editing && inputSelection) {
      editing.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction)
      editing.scrollTop = inputSelection.top
      editing.scrollLeft = inputSelection.left
    }
  }
}
