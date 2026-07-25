# This file is DEAD CODE and unused (nothing imports register_auth_routes).
# It was an early, now-incorrect duplicate of the auth routes that live in
# web/server.py - it checks a local SQLite password hash instead of the
# real Firebase Auth REST API/Admin SDK that server.py's /login,
# /api/auth/signup, and /api/auth/google now correctly use.
#
# Kept only as an empty placeholder (this sandbox can't delete files) -
# please delete web/auth_routes.py entirely on your machine, since a
# stale second copy of these routes sitting in the repo is exactly the
# kind of thing that causes hours of confusing debugging if anyone ever
# accidentally wires it back in.
