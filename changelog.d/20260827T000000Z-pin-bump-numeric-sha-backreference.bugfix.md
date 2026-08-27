`pin_bump` could not bump a pin to a commit whose SHA starts with a digit. The
`re.sub` replacement was the template `rf"\1{new_rev}\2"`, so the SHA's own
leading digits were re-read as part of the group reference: 96 of the 256
two-hex-char prefixes raised `invalid group reference N` and another 64
silently emitted an octal escape that swallowed the group-1 prefix and two SHA
characters, producing a corrupt `[tool.uv.sources]` line. 62.5% of real bumps
were affected — including the `robotsix-llmio` bump that would have delivered
the fix for the invalid level-1 model id. Substitution now uses a function
replacement, which `re` does not parse.
