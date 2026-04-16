# Challenge Containers

CTFd plugin that provisions per-user Docker containers for challenges across a pool of Docker hosts, with automatic port assignment, expiration timers, and lifecycle management

## How it works

When a user starts a challenge the plugin picks the least-loaded Docker host, creates a container with dynamic port mapping, and returns connection details. Users connect directly to the container's mapped port. The plugin supports TCP, SSH, and web connection types. Containers expire after a configurable duration and users can renew the timer a limited number of times

For multi-host setups the plugin load-balances across Docker contexts using weighted least-connections scoring. Health checks run every 30 seconds and automatically remove/restore hosts from the pool. Single-server deployments work out of the box with a local context auto-created on first boot

## Setup

1. Clone into `CTFd/CTFd/plugins/`, restart CTFd
2. Mount Docker access in your `docker-compose.yml`:

```yaml
services:
  ctfd:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ~/.ssh:/root/.ssh:ro
      - ~/.docker:/root/.docker:ro
```

The socket talks to the local daemon, the SSH keys tunnel to remote hosts, and the docker config has context metadata. For remote contexts you'll also want `network_mode: host` so SSH connections can reach your Docker hosts

3. Create Docker contexts for remote hosts:

```bash
docker context create server1 --docker "host=ssh://user@server1.example.com"
```

4. Import contexts from `/admin/config` under the Challenge Containers section. The import UI shows reachability and lets you set a public hostname before importing

5. Images need to be on the Docker host before a challenge can use them. The config page has an Image Availability section showing which images exist on which contexts, with pull buttons for anything missing

## Challenge settings

Select "Container" challenge type when creating a challenge. The typical web challenge only needs an image and point value, everything else has sensible defaults

- Docker Context: which host runs containers (default Auto, load-balanced)
- Image: fuzzy searchable by name or tag
- Port: internal container port (default 80), host port assigned randomly
- Connection Type: `tcp`, `ssh`, or `web` (default web)
- SSH Credentials: username/password, or `auto` to generate a random password
- Expiration: container lifetime (default 30 minutes)
- Max Renewals: timer resets allowed (default 2, 0 disables)
- Command, Volumes, Max Memory, Max CPU under Advanced options

## Plugin settings

Managed through `/admin/config`, no config files

| Key | Default | Description |
|-----|---------|-------------|
| max_containers_per_user | 4 | simultaneous container limit per user |
| thread_pool_size | 4 | worker threads for Docker operations |
| max_concurrent_creates | 2 | parallel container creation limit per host |
| expiration_check_interval | 5 | seconds between expiry sweeps |
| rate_limit_requests | 45 | max requests per rate limit interval |
| rate_limit_interval | 60 | rate limit window in seconds |
| default_expiration_seconds | 1800 | default container lifetime for new challenges |
| default_max_renewals | 2 | default renewal limit for new challenges |
| freshness_secret | (auto-generated) | HMAC key for freshness tokens, clear to disable |
| freshness_token_length | 6 | character length of per-user freshness tokens (4-16), changing invalidates running flags |
| post_solve_expiry_seconds | 90 | seconds until container expires after a correct solve, 0 to disable |

Rate limit changes require a CTFd restart

## Container security

Every container gets `cap_drop=ALL`, `no-new-privileges`, a pids limit of 256, and `auto_remove=True`. SSH challenges automatically get the capabilities sshd needs (SETUID, SETGID, CHOWN, etc). Additional capabilities can be added per-challenge via the `cap_add` field. Volume mounts are validated against a blocklist covering `/proc`, `/sys`, `/dev`, `/var/run`, `/run`, and sensitive files like `/etc/shadow`

## Freshness tokens

Static flags are vulnerable to flag sharing. The plugin can inject a deterministic per-user token into each container as `FRESHNESS_TOKEN`, derived from HMAC-SHA256 so restarting a container doesn't change the flag

Set up a "freshness" type flag in CTFd with `%TOKEN%` as a placeholder, like `ctf{some_flag_%TOKEN%}`. On the container side, build the flag from the environment variable in your entrypoint:

```dockerfile
CMD FLAG="ctf{some_flag_${FRESHNESS_TOKEN}}" python server.py
```

When someone submits another participant's token the submission gets rejected and the event is logged as a flag sharing attempt. The admin dashboard has a dedicated flag sharing table and chart

## Compose stacks

Challenges that need multiple containers on a shared network (like ARP spoofing labs) are supported. The plugin creates a per-user bridge network and spins up all containers on it. Users connect to the entry container, companions run alongside it

```yaml
type: container
ctype: ssh
image: arp-attacker:latest
port: 22
ssh_username: ctf
ssh_password_mode: auto
cap_add: NET_ADMIN,NET_RAW

services:
  sender:
    image: arp-sender:latest
    command: /send.sh
    cap_add: NET_RAW
  receiver:
    image: arp-receiver:latest
    command: /recv.sh

network:
  subnet: 10.10.10.0/24
  ips:
    entry: 10.10.10.30
    sender: 10.10.10.10
    receiver: 10.10.10.20
```

Kill, renew, and expire operations work on the entire stack. The dashboard shows one row per stack with a companion count indicator

## Admin dashboard

The dashboard at `/containers/dashboard` shows active containers, event stream, flag sharing attempts, and summary stats. Container rows have buttons for kill, renew, and log viewing (last 200 lines of stdout/stderr). Stats cards refresh every 10 seconds

The analytics section has charts for top users by container time, containers per challenge with restart rates, solve times, flag sharing timeline, and a usage heatmap. All charts accept a time range selector

## API endpoints

### User

- `POST /containers/api/request` start a container
- `POST /containers/api/view_info` check container status
- `POST /containers/api/stop` stop your container
- `POST /containers/api/renew` renew container timer
- `GET /containers/api/get_connect_type/<challenge_id>` connection type for a challenge

### Admin

- `GET /containers/dashboard` admin dashboard
- `GET /containers/api/running_containers` running containers as JSON
- `POST /containers/api/kill` kill a container
- `POST /containers/api/purge` kill all containers
- `POST /containers/api/admin_extend` renew a container (admin)
- `POST /containers/api/clear_history` delete all history records
- `GET /containers/api/stats/summary` stat card data
- `GET /containers/api/user_flags` user flag data
- `GET /containers/api/flag_sharing` flag sharing events
- `GET /containers/api/logs/<container_id>` container stdout/stderr (`?tail=N`, max 1000)

### Images

- `POST /containers/api/pull` pull image to contexts
- `GET /containers/api/images` list all images
- `GET /containers/api/images/<context_name>` images for a specific context
- `GET /containers/api/images/matrix` image availability across contexts
- `GET /containers/api/images/cache` cached matrix without scanning
- `GET /containers/api/images/status` per-image availability (`?image=name`)

### Contexts

- `GET /containers/api/contexts` active context names
- `GET /containers/api/contexts/list` all contexts with health and container counts
- `GET /containers/api/contexts/discover` scan for importable contexts
- `POST /containers/api/contexts/add` import a context
- `PUT /containers/api/contexts/update/<id>` update context
- `DELETE /containers/api/contexts/delete/<id>` remove context
- `GET /containers/api/contexts/test/<id>` test connectivity
- `POST /containers/api/contexts/reload` reconnect all contexts

### Analytics

- `GET /containers/api/analytics/top_users` top 20 users by container time
- `GET /containers/api/analytics/challenges` per-challenge container stats
- `GET /containers/api/analytics/solve_times` time to solve per challenge
- `GET /containers/api/analytics/activity` activity data
- `GET /containers/api/analytics/flag_sharing` flag sharing timeline
- `GET /containers/api/analytics/heatmap` container launches by hour/weekday

All analytics endpoints accept `?range=24h|7d|30d|all` (default `7d`)

### Events

- `GET /containers/api/events/stream` SSE event stream
- `GET /containers/api/events/recent` last 50 events as JSON

### Settings

- `GET /containers/api/settings` all settings
- `PUT /containers/api/settings` bulk upsert

## Development

```
ruff format --check .
ruff check .
mypy .
vulture .
pytest tests/ -v
```

## Troubleshooting

**Containers not starting**: check that the image is pulled on the relevant host and the context is reachable (use the Test button in admin)

**Images not found**: images must exist on the assigned context before users can start instances, use Image Availability on the config page to see what's where

**Containers not expiring**: check CTFd logs for expiry job messages, all containers have a mandatory expiration

**Host went down**: health checks run every 30 seconds, unhealthy contexts drop out of the pool and come back automatically when reachable again

**Too many open files**: raise the fd limit with `ulimits: { nofile: { soft: 65536, hard: 65536 } }` in docker-compose.yml
