# Challenge Containers

CTFd plugin that provisions per-user Docker containers for challenges across a pool of Docker hosts,
with automatic port assignment, expiration timers, and lifecycle management. Multi-host setups
load-balance across Docker contexts with health checks; single-server deployments work out of the box.

## Install

Clone into `CTFd/CTFd/plugins/` on a fresh CTFd installation and restart CTFd.

This build supports new installations only. Start it with an empty database that has no
challenge-container tables.

## Settings

Managed live through `/admin/config` under the Challenge Containers section, no config files.

## Development

```
python -m pytest tests
```
