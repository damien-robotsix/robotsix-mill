Bumped `pypdf` to 6.15.0, closing GHSA-fp3f-mc75-235c and GHSA-fwg2-594c-jp42
(both resource exhaustion). The release landed inside the rolling `exclude-newer`
window, so it needed a per-package override — the same shape as the cryptography
override above it.
