# Running Evently locally

These steps are for macOS. Run every `task` command from `backend/`.

## First-time setup

Do this once per machine.

```sh
# 1. Install the tools
brew install go-task python@3.13 node kubectl
cd backend
task tools:install      # kind, helm, podman, and a rootful 8 GB Podman machine

# 2. Install the backend and create backend/.env
task setup

# 3. Install the frontend
task fe:install
```

Then open `backend/.env` and set `ANTHROPIC_API_KEY`. To use OpenAI instead, set `LLM_PROVIDER=openai` and `OPENAI_API_KEY`. The other keys are optional. `task env:check` shows which keys are set.

## Start the app

**Terminal 1: the backend**

```sh
cd backend
podman machine start    # skip if it is already running
task k8s:up             # cluster, image, Postgres, Redis, Elasticsearch, API
task k8s:port-forward   # leave this running
```

**Terminal 2: the frontend**

```sh
cd backend
task fe:dev             # leave this running
```

Open <http://localhost:5173>.

| What | URL |
| --- | --- |
| App | <http://localhost:5173> |
| API docs | <http://localhost:8000/docs> |
| API health, through the port-forward | <http://localhost:8000/api/v1/health> |
| API health, through the ingress | <http://api.evently.localtest.me/api/v1/health> |

`task k8s:status` shows the pods and `task k8s:logs` follows the API logs.

## Run without Kubernetes

This is lighter and reloads the API when code changes. It uses a local SQLite file and does not start Postgres, Redis or Elasticsearch.

```sh
cd backend
task dev:all            # API on :8000, frontend on :5173
```

Stop the port-forward first, because both use port 8000.

## After you change something

| Change | Command |
| --- | --- |
| Backend code | `task k8s:image:load && task k8s:restart` |
| `backend/.env` | `task k8s:secrets:dev` |
| Frontend code | Nothing, it reloads automatically |

## Stop the app

Press Ctrl-C in both terminals. The cluster keeps running in the background.

```sh
podman machine stop     # stop the VM and free its memory
task k8s:nuke           # delete the evently namespace and its data
task k8s:cluster:down   # delete the whole cluster and its data
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `podman is not running` | `podman machine start` |
| `task k8s:up` cannot reach the cluster after a reboot | `podman start evently-control-plane`, then run `task k8s:up` again |
| Ingress URL returns 503 just after a deploy | Wait about 30 seconds. If it keeps happening, the VM is short on memory: `podman machine stop && podman machine set --memory 8192 && podman machine start` |
| Port 80 cannot be bound when creating the cluster | The Podman machine must be rootful: `podman machine stop && podman machine set --rootful && podman machine start` |
| Frontend shows API errors | Check that `task k8s:port-forward` is still running. It exits when the API pod restarts, so start it again. |
