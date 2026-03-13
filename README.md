# Individualized Containers Plugin

CTFd plugin that provisions per-user Docker containers for challenges across a pool of Docker hosts, each participant gets their own isolated environment with automatic port assignment, expiration timers, and lifecycle management

## How it works

When a user starts a challenge the plugin picks the least-loaded healthy Docker context, acquires the per-context creation semaphore so concurrent requests don't overwhelm the daemon, hits the Docker API through the SDK's SSH tunnel, creates the container with dynamic port mapping and security hardening, reads the mapped port back, writes a `ContainerInfoModel` row to the database with the container ID and expiration timestamp, and returns the connection details to the user. Each challenge can be pinned to a specific Docker context or left unassigned so the load balancer picks one automatically

Users connect directly to the container's mapped port on the runner host. The plugin supports TCP, SSH, and web connection types, each challenge gets configured with credentials and connection info that the frontend displays

## Setup

### Installing the plugin

Clone this repo into CTFd's plugin directory, the folder name doesn't matter but it needs to sit directly under `CTFd/CTFd/plugins/`

```bash
cd CTFd/CTFd/plugins
git clone <repo-url> challenge_containers
```

CTFd picks up plugins on startup so you'll need to restart after cloning

### Docker access

The CTFd container needs access to Docker, both for the local socket and for remote hosts over SSH. Add these volumes to your CTFd service in `docker-compose.yml`

```yaml
services:
  ctfd:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ~/.ssh:/root/.ssh:ro
      - ~/.docker:/root/.docker:ro
```

The docker socket lets the SDK talk to the local daemon, the SSH keys let it tunnel to remote hosts, and the docker config directory has the context metadata files the plugin reads to resolve endpoints

If you're only using remote contexts and don't need a local daemon you can skip the socket mount, but you still need the SSH and docker config mounts

For remote contexts to work from inside the CTFd container you'll also want `network_mode: host` or equivalent network access so the SSH connections can reach your Docker hosts

### Docker contexts

Set up contexts on the machine running CTFd (or inside the container if you mounted the config)

```bash
docker context create server1 --docker "host=ssh://user@server1.example.com"
docker context create server2 --docker "host=ssh://user@server2.example.com"
```

Then add them through the admin dashboard at `/containers/admin/contexts`, each context needs a name matching what you created above, a hostname (SSH connection target), an optional public hostname (what users see in connection strings, falls back to the SSH hostname if unset), and a weight for load balancing

The plugin also validates the local Docker socket on startup and adds it as a "default" context automatically if it responds to a ping

### Pre-pulling images

Images need to be on the Docker host before a challenge can use them. The admin settings page has a pre-pull section where you type an image name and hit Pull, it sends the pull to all configured contexts and shows you the result per host

You can also do this via the API if you want to script it

```bash
curl -X POST /containers/api/pull \
  -H "Content-Type: application/json" \
  -d '{"image": "your-challenge:latest"}'
```

Pass `context_name` in the body to pull to a specific context only

### Database

The plugin creates its tables automatically on first load, no manual migration needed. It creates `docker_contexts` for the context pool, `container_challenges` for challenge definitions, `container_info` for active container metadata, and `container_settings` for plugin configuration

## Container lifecycle

### Creation

1. User clicks "Start Instance" on a challenge page
2. Plugin acquires the per-challenge+team (or per-challenge+user) creation lock so duplicate requests serialize
3. Checks if a container already exists for this user/team on this challenge, returns connection info if so
4. Picks a Docker context, either the one pinned to the challenge or the least-loaded healthy context via weighted scoring
5. Acquires the per-context creation semaphore (limits concurrent creates per host, default 2)
6. Creates the container via the Docker SDK with dynamic port mapping, security hardening (`cap_drop=ALL`, `no-new-privileges`, pids limit 256), resource limits from the challenge config, and environment variables (`CHALLENGE_ID`, `TEAM_ID`, `USER_ID`)
7. Reads the mapped host port back from the Docker API
8. Writes a `ContainerInfoModel` row with container ID, port, expiration, and context name
9. Returns connection details (hostname, port, connection type, credentials)

If the DB commit fails after Docker creates the container, the plugin rolls back the transaction and kills the orphaned container so you don't end up with phantom containers that the plugin doesn't know about

### Destruction

User clicks "Stop Instance" or admin force-kills from the dashboard. Plugin kills the container via the Docker API, logs the event, and deletes the DB row. Containers are created with `auto_remove=True` so Docker cleans up the filesystem when they stop

### Expiration

Each challenge has a configurable expiration in minutes (default 30, 0 means never expire). An APScheduler job runs on a configurable interval (default 5s) to query the database for containers past their expiration timestamp and kills them. Users can renew their session from the UI which resets the timer to the challenge's configured duration

## Load balancing

The plugin uses least-connections scoring to distribute containers across Docker contexts. For each available context it calculates `weight / (active_count + 1)` where active_count comes from querying `ContainerInfoModel` for containers currently on that context. Highest score wins, ties broken alphabetically. A context with weight 2 at zero containers scores 2.0 while weight 1 at zero containers scores 1.0, so the heavier context gets picked first, but as it accumulates containers the score drops and lighter contexts start getting traffic

This is better than round-robin because it accounts for actual load, if containers have different lifetimes or some get killed early the balancer naturally routes to wherever has capacity rather than blindly cycling through contexts

## Container security

Every container gets hardened defaults regardless of challenge configuration

- `cap_drop=["ALL"]` drops all Linux capabilities
- `security_opt=["no-new-privileges:true"]` prevents setuid/setgid escalation
- `pids_limit=256` caps the process count to prevent fork bombs
- `auto_remove=True` so Docker cleans up the filesystem when the container stops

## Freshness tokens

Challenges with static flags are vulnerable to flag sharing between participants. The plugin can inject a deterministic per-user token into each container as the `FRESHNESS_TOKEN` environment variable, challenge authors use it to build unique flags and a custom CTFd flag type handles validation by recomputing the expected flag for whoever submitted it

The token is 4 lowercase alphanumeric chars (`[a-z0-9]`) derived from HMAC-SHA256. Same inputs always produce the same output so restarting a container doesn't change the flag. The `freshness_secret` key gets auto-generated on first boot and stored in plugin settings, no manual configuration needed

To use it you set up the flag in CTFd with the "freshness" type and put `%TOKEN%` where you want the token substituted, like `ctf{this_is_a_flag_%TOKEN%}`. On the challenge side you build the flag from the environment variable in your entrypoint

```dockerfile
FROM python:3.12-slim
COPY server.py /app/server.py
WORKDIR /app
CMD FLAG="ctf{this_is_a_flag_${FRESHNESS_TOKEN}}" python server.py
```

The server code just reads `FLAG` from the environment like normal, nothing changes there

```python
import os
flag = os.environ.get('FLAG')
```

When a user submits a flag that matches the template structure but contains another participant's token the submission gets rejected and they're told the flag belongs to someone else. The event gets logged as `flag_sharing` with warning level so admins can see it in the dashboard. The expensive all-users check only runs when the submission matches the flag pattern but with the wrong token, normal incorrect guesses skip it

Clearing `freshness_secret` in the admin settings disables the feature entirely, no tokens get injected into containers and the `attempt()` override falls through to normal flag checking

## Race condition protection

Container creation is serialized per challenge+team (or challenge+user in user mode) using a per-key lock, so if someone mashes the start button or sends concurrent requests only one container gets created and the second request waits on the lock. This prevents the duplicate container problem where two requests both pass the existence check and each spin up a container

The per-context creation semaphore (default limit 2) is a separate concern, it limits how many containers can be created simultaneously on a single Docker host so a burst of users hitting start on different challenges doesn't overwhelm the daemon with parallel SSH connections

## Thread safety

Thread-local Docker clients use a generation counter that gets bumped whenever context configs change, so stale connections from old configs get dropped and recreated transparently across all threads without needing to reach into each thread's local storage. The context config map and weights are protected by `_context_lock`, the lock is only held during the final swap of computed state, not during slow I/O like Docker pings

When gevent is available (which it is in the default CTFd deployment) the thread pool uses `gevent.threadpool.ThreadPool` and semaphores use `gevent.lock.BoundedSemaphore` so everything cooperates with the event loop properly. Falls back to `ThreadPoolExecutor` and `threading.BoundedSemaphore` otherwise

## Scheduling

The plugin uses APScheduler for background jobs. Under gunicorn with gevent it uses `GeventScheduler`, otherwise `BackgroundScheduler`. Two independent jobs run

- Expiry check: every `expiration_check_interval` seconds (default 5), queries the database for containers past their expiration and kills them
- Health check: every 30 seconds, pings each Docker context and removes unhealthy ones from the pool, then tries to reconnect previously removed contexts and adds them back if they respond

Both jobs use `misfire_grace_time=30` and `coalesce=True` so if the scheduler falls behind it catches up without firing duplicate runs

## Context health

Contexts get marked unhealthy when the connectivity test fails (SSH tunnel or Docker daemon ping). Unhealthy contexts get removed from the scheduling pool so new containers don't get routed there. On the next health check pass (every 30 seconds) the plugin tries to reconnect any previously removed contexts by re-reading the Docker context meta file or falling back to the DB hostname, pinging, and adding them back to the pool if they respond

If a user checks their container status while the host is unreachable they see a "host temporarily unreachable" message instead of having their container record deleted, so they don't lose their session if the host recovers

## Startup reconciliation

On startup the plugin reconciles the database with Docker, querying all `ContainerInfoModel` rows and checking each against the Docker API to see if the container is still running. Records for containers that no longer exist get deleted so you don't accumulate stale rows after a CTFd restart or crash

## Event logging

The event logger provides a thread-safe event stream for the admin dashboard. Each event has a type, message, level (info/warning/error), timestamp, human-readable datetime, optional user info, and a metadata dict for domain-specific fields like container_id, challenge_id, and team info. Events also get written to Python's logging module so they show up in CTFd's logs

The admin dashboard gets a real-time SSE stream backed by a bounded queue (100 events) per listener, if a browser disconnects ungracefully and events pile up the oldest ones get dropped instead of growing memory forever. The event buffer holds up to 2000 events for the recent events API

## Configuration

### Challenge settings

Select "Container" challenge type when creating a challenge, the form defaults to `web` connection type, port `80`, and "Auto (load-balanced)" context so the typical web challenge only needs a point value and an image picked from the search box

The image field is a fuzzy search input instead of a flat dropdown, it loads available images from the selected context (or from all contexts when Auto is selected) and you type a few characters to filter by name or tag, results are scored so prefix matches rank higher than substring hits and you can navigate with arrow keys

Core fields sit at the top, runner fields like context and expiration come next, and command, volumes, and resource limits are tucked under a collapsible "Advanced options" section that auto-expands on the update form if any of those fields already have values

- Docker Context: which host runs this challenge's containers, defaults to Auto which lets the load balancer pick
- Image: Docker image, fuzzy searchable by name or tag (must exist on the selected context or any context when Auto)
- Port: internal container port, the host port gets assigned by Docker automatically (default 80)
- Command: optional override command
- Volumes: JSON object for volume mounts
- Connection Type: `tcp`, `ssh`, or `web` (default web)
- SSH Credentials: username/password for ssh type
- Expiration: minutes until auto-kill (0 means never, default 30)
- Max Memory: MB limit per container
- Max CPU: core limit as decimal (1.5 means 1.5 cores)

### Plugin settings

Managed through the admin settings page, no config files needed

| Key | Default | Description |
|-----|---------|-------------|
| max_containers_per_user | 4 | simultaneous container limit per user |
| default_memory_mb | 0 | MB per container, 0 for unlimited |
| default_cpu_limit | 0 | core limit as decimal, 0 for unlimited |
| thread_pool_size | 4 | worker threads for Docker operations |
| max_concurrent_creates | 2 | parallel container creation limit per host |
| expiration_check_interval | 5 | seconds between expiry sweeps |
| rate_limit_requests | 500 | max requests per rate limit interval |
| rate_limit_interval | 10 | rate limit window in seconds |
| freshness_secret | (auto-generated) | HMAC key for freshness tokens, clear to disable |

Rate limit changes require a CTFd restart because the decorator values are evaluated at import time

Per-challenge memory, CPU, and expiration settings override the defaults when configured on the challenge itself

### Docker contexts

Managed through the admin dashboard at `/containers/admin/contexts`, each context has a name (matching a Docker context on the host), a hostname (SSH target), an optional public hostname (what users see in connection strings), a weight for load balancing, and an enabled flag. Add, edit, delete, and test connectivity all from the UI without restarting CTFd

## API endpoints

### User

- `POST /containers/api/request` request container instance
- `POST /containers/api/view_info` check container status and connection info
- `POST /containers/api/stop` stop your container
- `POST /containers/api/renew` extend expiration time
- `GET /containers/api/get_connect_type/<challenge_id>` get connection type for a challenge

### Admin

- `GET /containers/dashboard` view all running containers
- `GET /containers/api/running_containers` running containers as JSON
- `POST /containers/api/kill` kill specific container
- `POST /containers/api/purge` kill all containers
- `POST /containers/api/pull` pre-pull image to contexts
- `GET /containers/api/images` list images across all contexts
- `GET /containers/api/images/<context>` list images for a specific context

### Contexts

- `GET /containers/admin/contexts` admin UI for managing contexts
- `GET /containers/api/contexts` list active context names
- `GET /containers/api/contexts/list` list all contexts with details
- `POST /containers/api/contexts/add` add new context
- `PUT /containers/api/contexts/update/<id>` update context settings
- `DELETE /containers/api/contexts/delete/<id>` remove context
- `GET /containers/api/contexts/test/<id>` test context connectivity

### Events

- `GET /containers/api/events/stream` SSE stream of container events
- `GET /containers/api/events/recent` last 50 events as JSON

### Settings

- `GET /containers/api/settings` all settings as JSON
- `PUT /containers/api/settings` bulk upsert

## Troubleshooting

**Containers not starting**: check that Docker contexts are configured and the image is pulled on the relevant host, use the Test button in the admin context UI to verify connectivity, check that the context has enough capacity (semaphore limit)

**Containers on wrong host**: each challenge can be pinned to a specific Docker context in the challenge settings, if it's unset the load balancer picks automatically based on least-connections scoring

**Images not found**: images must exist on the challenge's assigned context before users can start instances, use the pre-pull feature in plugin settings to push images to all contexts before the CTF starts

**Containers not expiring**: check that `expiration_minutes` is set to non-zero for the challenge, `expires = 0` means never expire which is intentional, verify the scheduler is running by checking CTFd logs for expiry job messages

**Containers piling up on one host**: the load balancer uses weighted least-connections scoring, check that your context weights are set appropriately in the admin UI, a context with weight 2 gets twice the score bonus compared to weight 1

**Host went down during CTF**: the health check runs every 30 seconds and automatically removes unreachable contexts from the pool, when the host comes back the next health check recovers it, check CTFd logs for messages like "health check: removing unhealthy context" and "health check: recovered context"
