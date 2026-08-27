Revert the `ci_fix_max_iterations` default bump from 3 to 5: the deployed mill pins this key, so the default is shadowed, and the shipped default should match what production runs.
