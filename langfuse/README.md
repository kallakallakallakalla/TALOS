# Langfuse local instance

This folder contains a local Langfuse Docker Compose deployment. All published
ports bind to `127.0.0.1`, so the services are reachable only from this machine.

## Start and stop

```sh
cd langfuse
docker compose up -d
docker compose ps
docker compose down
```

Open <http://localhost:3000> in the Orca browser. `credentials.md` contains the
local administrator login, project API keys, and connection details. Keep that
file private; it is excluded from Git.

Compose volumes preserve data when the stack is stopped with `docker compose
down`.
