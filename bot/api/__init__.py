"""Dashboard API routers. Shared state (the journal, the engines, locks)
lives in bot.dashboard and is looked up there at call time, so tests and
the app see one copy of it."""
