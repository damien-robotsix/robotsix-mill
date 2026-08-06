Bump `cryptography` to 50.0.0, clearing GHSA-m2h6-j472-rp4c and
GHSA-g6cj-pr64-35w5 (a Bleichenbacher oracle in PKCS#7 EnvelopedData
decryption). The vulnerability scan had been failing on `main` since
2026-08-03, which tripped mill's own target-branch-debt guard and blocked
13 tickets from merging. The fix version landed inside the rolling 7-day
`exclude-newer` window, so it needed a per-package override to be
resolvable — safe to remove once the window has passed.
