# robotsix-board

Shared kanban-board frontend library: column-per-status board of cards with a move-between-columns action, auto-refresh, and a click-through detail panel. Owns the board HTML/CSS/JS chrome, parameterized by a small data adapter (column order, card fields, move endpoint) and a render mode (server-rendered fragments vs JSON+JS hydration). Consumed by robotsix-mill (FastAPI + static files) and robotsix-auto-mail (stdlib BaseHTTPRequestHandler + inline Jinja).
