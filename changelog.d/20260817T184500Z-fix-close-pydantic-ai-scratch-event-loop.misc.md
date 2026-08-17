fix: close the pydantic-ai scratch event loop after agent runs so its default-executor threads no longer leak (the OOM that was mass-blocking tickets at SPAWN_LIMIT)
