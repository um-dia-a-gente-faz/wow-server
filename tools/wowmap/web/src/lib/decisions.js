/**
 * One row of the decision log: what the action was, its confidence as a percentage, whether it failed.
 * @param {{action: string|null, args: Record<string, unknown>, ok: boolean, error: string|null,
 *          confidence: number|null, reason: string|null}} d
 */
export function decisionRow(d) {
  const args = Object.entries(d.args || {}).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
  const short = args.length > 40 ? args.slice(0, 39) + '…' : args;
  return {
    action: (d.action ?? 'none') + (short ? ` (${short})` : ''),
    confidence: d.confidence == null ? '' : `${Math.round(d.confidence * 100)}%`,
    failed: !d.ok,
    reason: d.reason ?? (d.ok ? '' : d.error ?? 'failed')
  };
}
