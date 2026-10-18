/**
 * "just now", "5 min ago", "3 h ago", "2 d ago" for a unix epoch (seconds).
 * @param {number} epoch
 * @param {number} now unix seconds
 */
export function lastSeenText(epoch, now) {
  const s = Math.max(0, now - epoch);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
