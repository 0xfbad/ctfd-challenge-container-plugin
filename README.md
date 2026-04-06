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
git clone <repo-url>
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

Then import them from the admin config page at `/admin/config` under the Challenge Containers section, click "Import Contexts" and the plugin scans for contexts on the host, shows reachability, and lets you set a public hostname before importing. Weight and enabled can be changed after

For single-server deployments you don't need to configure anything. On first boot with an empty contexts table the plugin checks if the local Docker socket at `/var/run/docker.sock` is reachable, and if so creates a `local` context automatically using the machine's hostname as the public address. If you delete it and restart CTFd it comes back

### Pre-pulling images

Images need to be on the Docker host before a challenge can use them. The config page has an Image Availability section that shows a matrix of which challenge images exist on which contexts, with pull buttons for anything missing

You can also bulk-pull via the API

```bash
curl -X POST /containers/api/pull \
  -H "Content-Type: application/json" \
  -d '{"image": "your-challenge:latest"}'
```

Pass `context_name` in the body to pull to a specific context only

### Database

The plugin creates its tables automatically on first load, no manual migration needed. It creates `docker_contexts` for the context pool, `container_challenges` for challenge definitions, `container_info` for active container metadata, `container_settings` for plugin configuration, and `container_history` for permanent lifecycle records

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

### Post-solve auto-expiry

When a player submits a correct flag the plugin shortens their running container's expiration to 90 seconds from now. This frees resources quickly after a solve without killing the container instantly, giving the player a moment to see the result. The delay is configurable via the `post_solve_expiry_seconds` setting, set it to 0 to disable. Containers with no expiration (`expires = 0`) are left alone

### Container history

`ContainerInfoModel` rows get deleted when containers stop, so there's no way to look at usage patterns after the fact. The `ContainerHistoryModel` table persists lifecycle data permanently. A row is inserted on every container create with the container ID, challenge, user/team, docker context, and creation timestamp. When the container ends the row gets updated with a `stopped_at` timestamp and a reason

Reasons track how the container ended: `stopped` (user or admin killed it), `expired` (expiry job), `purged` (admin purge all), `reconciled` (stale record cleaned up on startup), or `solved` (post-solve auto-expiry). The solved reason gets set when the player submits a correct flag and the actual `stopped_at` timestamp gets filled in later when the expiry job kills it

Foreign keys to challenges, users, and teams use `ON DELETE SET NULL` so history survives if those records get deleted

## Load balancing

The plugin uses least-connections scoring to distribute containers across Docker contexts. For each healthy context it calculates `weight / (active_count + 1)` where active_count is tracked in memory via `select_and_reserve` / `release_slot` calls rather than querying the database on every scheduling decision. Highest score wins, ties broken alphabetically. A context with weight 2 at zero containers scores 2.0 while weight 1 at zero containers scores 1.0, so the heavier context gets picked first, but as it accumulates containers the score drops and lighter contexts start getting traffic

Context selection and slot reservation happen atomically under a lock so two concurrent requests can't race for the same slot. Slots get released when containers are killed (by users, admins, or the expiry job) and recovered on startup via reconciliation

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

## Architecture

Docker integration is split into three files that mirror the remote-desktop plugin's layout

- `DockerHostManager` owns Docker operations: thread-local client caching, endpoint resolution, container run/kill/check, image listing, per-context semaphores
- `Orchestrator` owns scheduling: weighted scoring, health booleans, slot counts. References the host manager for pings but never creates containers directly
- `ContainerManager` composes the two and adds the multi-context fallback loop (try pinned context or fall through healthy ones), the expiration scheduler, and the public API that routes call

## Thread safety

Thread-local Docker clients use a generation counter that gets bumped whenever context configs change, so stale connections get dropped and recreated transparently. The host manager and orchestrator each have their own lock, both only held during the final state swap, not during I/O like Docker pings

Uses `threading.BoundedSemaphore` and `threading.Lock` directly, gunicorn's gevent workers monkey-patch the threading module so these cooperate with the event loop without explicit gevent imports

## Scheduling

The plugin uses APScheduler with `BackgroundScheduler` for background jobs. Two independent jobs run

- Expiry check: every `expiration_check_interval` seconds (default 5), queries the database for containers past their expiration, kills them, and releases their load balancer slots
- Health check: every 30 seconds, pings each Docker context and flips health booleans, unhealthy contexts stay in the tracking dict and get re-enabled automatically when they start responding again

Both jobs use `misfire_grace_time=30` and `coalesce=True` so if the scheduler falls behind it catches up without firing duplicate runs

## Context health

Contexts stay in the health tracking dict even when unreachable, their health boolean gets flipped to false instead of removing them from the pool entirely. The load balancer skips unhealthy contexts when picking where to schedule new containers. On the next health check pass (every 30 seconds) the plugin pings each context and flips the boolean back to true if it responds, automatically re-enabling it for scheduling without any manual intervention or config reload

Health transitions get logged as `host_healthy` / `host_unhealthy` events so they show up in the admin event stream and in CTFd's logs

Endpoint resolution has a fallback chain: scan `~/.docker/contexts/meta/` matching by the `Name` field in each `meta.json` (Docker names these dirs by SHA256 hash, not context name, so you have to scan), then try `ssh://{hostname}` from the DB, then the local socket. A context with no SSH hostname still works if there's a matching Docker context file or a local socket

If a user checks their container status while the host is unreachable they see a "host temporarily unreachable" message instead of having their container record deleted, so they don't lose their session if the host recovers

## Startup reconciliation

On startup the plugin reconciles the database with Docker, querying all `ContainerInfoModel` rows and checking each against the Docker API to see if the container is still running. Containers that are still alive get their load balancer slots reserved so the scheduler has accurate counts from the start. Records for containers that no longer exist get deleted so you don't accumulate stale rows after a CTFd restart or crash

## Event logging

The event logger provides a thread-safe event stream for the admin dashboard. Each event has a type, message, level (info/warning/error), timestamp, human-readable datetime, optional user info, and a metadata dict for domain-specific fields like container_id, challenge_id, and team info. Events also get written to Python's logging module so they show up in CTFd's logs

The admin dashboard gets a real-time SSE stream backed by a bounded queue (100 events) per listener, new connections receive the last 200 events immediately so the dashboard has history on load. If a browser disconnects ungracefully and events pile up the oldest ones get dropped instead of growing memory forever. The stream uses SSE comment keepalives (`": keepalive\n\n"`) every 30 seconds to detect dead connections. The event buffer holds up to 2000 events for the recent events API

## Container log viewer

The admin dashboard has a logs button (terminal icon) on each running container row. Clicking it opens a modal that fetches the last 200 lines of stdout/stderr from the container via the Docker API. The tail count is configurable via the `?tail=N` query parameter on the API endpoint (max 1000). Useful for debugging challenge images without shelling into the Docker host

## Analytics dashboard

The Container Stats page (`/containers/admin/stats`, linked from the admin plugin menu) provides four ECharts visualizations built from `ContainerHistoryModel` data. All charts accept a time range selector (24h, 7d, 30d, all time)

The activity chart shows container creates and stops over time, bucketed hourly for the 24h view and daily for longer ranges. The top users chart ranks users by total container time with container count and unique challenge count in tooltips. The challenge stats chart shows containers per challenge with a restart rate overlay (containers per unique user, useful for spotting challenges that crash frequently). The solve times chart cross-references CTFd's `Solves` table with container history to compute how long each player took from container creation to flag submission, displayed as average bars with individual solve times as scatter points

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
| thread_pool_size | 4 | worker threads for Docker operations |
| max_concurrent_creates | 2 | parallel container creation limit per host |
| expiration_check_interval | 5 | seconds between expiry sweeps |
| rate_limit_requests | 500 | max requests per rate limit interval |
| rate_limit_interval | 10 | rate limit window in seconds |
| freshness_secret | (auto-generated) | HMAC key for freshness tokens, clear to disable |
| post_solve_expiry_seconds | 90 | seconds until container expires after a correct solve, 0 to disable |

Rate limit changes require a CTFd restart because the decorator values are evaluated at import time. Memory, CPU, and expiration limits are configured per-challenge in the challenge settings

### Docker contexts

Managed through the admin config page at `/admin/config`. Contexts get imported from Docker's context metadata on the host via the Import button, each one has a name matching a Docker context, an optional SSH hostname, a public hostname (what users see, required), a weight, and an enabled flag. A `local` context is auto-seeded on first boot when the Docker socket is available. The table shows status (UP/DOWN/DEGRADED/DISABLED) and active container counts per host. Reload reconnects all contexts without restarting CTFd

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
- `GET /containers/api/logs/<container_id>` fetch container stdout/stderr (optional `?tail=N`)

### Analytics

- `GET /containers/admin/stats` analytics dashboard page
- `GET /containers/api/analytics/activity` container creates/stops over time
- `GET /containers/api/analytics/top_users` top 20 users by total container time
- `GET /containers/api/analytics/challenges` per-challenge container stats
- `GET /containers/api/analytics/solve_times` time-to-solve per challenge

All analytics endpoints accept `?range=24h|7d|30d|all` (default `7d`)

### Contexts

- `GET /containers/api/contexts` list active context names
- `GET /containers/api/contexts/list` all contexts with health, container counts, socket status
- `GET /containers/api/contexts/discover` scan host for importable Docker contexts
- `POST /containers/api/contexts/add` import a context
- `PUT /containers/api/contexts/update/<id>` update context settings
- `DELETE /containers/api/contexts/delete/<id>` remove context
- `GET /containers/api/contexts/test/<id>` test context connectivity
- `POST /containers/api/contexts/reload` reconnect all contexts
- `GET /containers/api/images/matrix` which challenge images exist on which contexts

### Events

- `GET /containers/api/events/stream` SSE stream of container events
- `GET /containers/api/events/recent` last 50 events as JSON

### Settings

- `GET /containers/api/settings` all settings as JSON
- `PUT /containers/api/settings` bulk upsert

## Troubleshooting

**Containers not starting**: check that Docker contexts are configured and the image is pulled on the relevant host, use the Test button in the admin context UI to verify connectivity, check that the context has enough capacity (semaphore limit)

**Containers on wrong host**: each challenge can be pinned to a specific Docker context in the challenge settings, if it's unset the load balancer picks automatically based on least-connections scoring

**Images not found**: images must exist on the challenge's assigned context before users can start instances, use the Image Availability scan on the config page to see what's where and pull what's missing. For private images you'll need to `docker save` / `docker load` onto each host or push to a private registry the hosts can reach

**Containers not expiring**: check that `expiration_minutes` is set to non-zero for the challenge, `expires = 0` means never expire which is intentional, verify the scheduler is running by checking CTFd logs for expiry job messages

**Containers piling up on one host**: the load balancer uses weighted least-connections scoring, check that your context weights are set appropriately in the admin UI, a context with weight 2 gets twice the score bonus compared to weight 1

**Host went down during CTF**: the health check runs every 30 seconds and flips unhealthy contexts out of the scheduling pool, when the host comes back the next health check re-enables it automatically, check CTFd logs for `host_unhealthy` and `host_healthy` events or look at the admin event stream
