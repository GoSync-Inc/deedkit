"""Engine runtimes: modules the worker imports by name. Each has `load(paths, options) -> runtime` with
`run(job) -> dict` and `unload()`. The library behind a runtime is imported inside `load`, never at module level."""
