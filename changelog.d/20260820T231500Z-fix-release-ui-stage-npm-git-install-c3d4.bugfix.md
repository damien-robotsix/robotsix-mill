`Release` can publish a mill image again. The `ui` stage installed `@robotsix/ui`
via an npm git URL with `--ignore-scripts`; because that package declares
`files: ["dist"]` and builds `dist` in `prepare`, npm skipped the build and then
packed only the declared files, leaving a node_modules entry holding LICENSE and
README and nothing else. The subsequent `vite build` had no config and no
sources, fell back to application mode, and failed with `Could not resolve entry
module "index.html"`. The stage now clones the pinned commit and builds from
source.
