Added a daily config pin-drift check. `config/config.json` pins ~288 settings and
a pin always beats the model default, so changing a `Field(default=…)` is a no-op
in production until someone edits the pin too. That silently reverted the move to
weekly periodics — twelve generators ran daily for weeks at roughly 7× the
intended ticket volume — and a change disabling the per-ticket spend caps. Both
were found by hand, long after. The pass reports only drift not listed in
`config_pin_drift_baseline`, so deliberate operator choices stay quiet.
