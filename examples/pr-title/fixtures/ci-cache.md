# speed up builds

The test job spent about four minutes reinstalling dependencies on every
run. Caches the uv environment keyed on the lockfile and splits the lint
job out so it can run in parallel.
