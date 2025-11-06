# Individualized Containers Plugin

CTFd plugin for per-user/team Docker container instances with multi-host support and automatic lifecycle management

## What It Does

Each participant gets their own containerized environment for challenges. The plugin handles container creation, dynamic port allocation, expiration, and cleanup. Multi-host support allows distributing challenges across different Docker hosts by assigning each challenge to a specific context

## Architecture

### Multi-Host Support

Each challenge is assigned to a specific Docker context during creation. All containers for that challenge spawn on the assigned host. This allows manually distributing challenges across infrastructure based on resource requirements or isolation needs

The plugin tracks which context hosts each container for subsequent operations (status checks, renewal, termination). Context configuration includes hostname (for connection strings) and weight (stored for potential future use or manual capacity planning)

### Container Lifecycle

Containers are created with `--rm` for automatic cleanup on stop. The plugin stores metadata (context, port, expiration) in the database. A background scheduler (5s interval) kills expired containers, triggering Docker's auto-removal. Port allocation randomly selects from 1024-65536 with availability checking

### Environment Variables

Each container receives:
- `CHALLENGE_ID`: challenge identifier
- `TEAM_ID`: team identifier (if team mode)
- `USER_ID`: user identifier

## Requirements

- Docker contexts configured on CTFd host
- Network access between CTFd and all Docker hosts
- Docker socket or SSH access to remote daemons

## Setup

### Docker Contexts

Configure contexts on the machine running CTFd:

```bash
docker context create local --docker "host=unix:///var/run/docker.sock"
docker context create server1 --docker "host=ssh://user@server1.example.com"
```

### CTFd Container Access

Mount necessary resources into CTFd container:

Local socket only:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

Remote contexts via SSH:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - ~/.ssh:/root/.ssh:ro
  - ~/.docker:/root/.docker:ro
```

For remote contexts, use `network_mode: host` or ensure proper network routing to Docker hosts

### Database Setup

The plugin creates these tables automatically on first load:
- `docker_contexts` (context pool configuration)
- `container_challenges` (challenge definitions)
- `container_info` (active container metadata)
- `container_settings` (plugin settings)

Restart CTFd after installation to trigger migrations

### Configure Contexts

Navigate to `/containers/admin/contexts`:

1. Add each context by name (must match `docker context ls` output)
2. Set public hostname (where users connect, can differ per host)
3. Set weight (stored for reference, useful for manual capacity planning)
4. Test connectivity via "Test" button

The context name identifies the Docker daemon, the hostname is what users see in connection strings. Challenges will be manually assigned to contexts during creation

## Usage

### Challenge Configuration

Select "Container" challenge type and configure:

- **Docker Context**: Required, choose which host runs this challenge's containers
- **Image**: Docker image (must exist on selected context)
- **Port**: Internal container port
- **Command**: Optional override command
- **Volumes**: JSON object for volume mounts
- **Connection Type**: `tcp`, `ssh`, or `web`
- **SSH Credentials**: Username/password for ssh type
- **Expiration**: Minutes until auto-kill (0 = never, default 30)
- **Max Memory**: MB limit per container
- **Max CPU**: Core limit as decimal (1.5 = 1.5 cores)

All containers for a challenge spawn on its assigned context. Resource limits and expiration are per-challenge, allowing different profiles for different workloads

### User Workflow

Users click "Start Instance" to spawn a container. The UI shows connection details (hostname:port from the assigned context), expiration time, and controls for "Stop Instance" and "Renew Instance". Renewing resets the expiration timer to the challenge's configured duration

## API Endpoints

### Context Management
- `GET /containers/admin/contexts` - Admin UI for managing contexts
- `GET /containers/api/contexts/list` - List all contexts
- `POST /containers/api/contexts/add` - Add new context
- `PUT /containers/api/contexts/update/<id>` - Update context weight/status
- `DELETE /containers/api/contexts/delete/<id>` - Remove context
- `GET /containers/api/contexts/test/<id>` - Test context connectivity

### Container Operations
- `POST /containers/api/request` - Request container instance
- `POST /containers/api/stop` - Stop your container
- `POST /containers/api/renew` - Extend expiration time
- `GET /containers/api/status` - Check container status

### Admin Operations
- `GET /containers/dashboard` - View all running containers
- `POST /containers/api/kill` - Kill specific container
- `POST /containers/api/purge` - Kill all containers

## Troubleshooting

**Containers not starting**
- Check that Docker contexts are properly configured: `docker context ls`
- Test each context manually: `docker context use <name> && docker ps`
- Check the "Test" button in the admin UI for each context
- Verify network connectivity to remote hosts

**Containers on wrong host**
- Check the challenge's assigned Docker Context in the challenge settings
- Ensure context names in admin UI exactly match `docker context ls` output

**Port conflicts**
- The plugin randomly selects from 1024-65536
- Too many containers on one host can cause conflicts
- Reassign some challenges to different contexts

**Images not found**
- Images must exist on the challenge's assigned context
- Pull images on the Docker host before assigning challenges to that context
- Check available images per context via admin UI

**Containers not expiring**
- Check that expiration_minutes is set to non-zero for the challenge
- The background scheduler runs every 5 seconds
- Verify the scheduler is running (check CTFd logs)
- Remember that 0 means never expire, which is intentional
