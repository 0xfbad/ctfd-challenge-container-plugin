# challenge containers

ctfd plugin for per user or per team docker challenges, freshness flags, and generated handouts

install this repo under `CTFd/plugins/challenge-containers`, install `requirements.txt`, then restart ctfd and configure docker contexts under `/admin/config`

for generated handouts, set `PERSONALIZED_FILES_ROOT` outside `UPLOAD_FOLDER`, share that directory with the file worker, and run `python src/files/worker.py` with access to the generator docker daemon

generator images run `/generate` and write the configured output files into `OUTPUT_DIR`, the worker supplies `FRESHNESS_TOKEN`, `FRESHNESS_SEED`, and `FLAGS_JSON`, and supplies `FLAG` when there is one freshness flag, build the example with `docker build -t handout-example src/files/examples`

challenge mounts require preexisting read only docker volumes declared in `CHALLENGE_CONTAINERS_VOLUME_POLICY_JSON`, volume names and labels must match the infrastructure policy

startup creates missing tables and adds the instance ssh password column, other incompatible schemas require a reviewed migration before upgrading

run `ruff check .`, `ruff format --check .`, and `mypy .` for local checks, integration tests live in an untracked local `tests/` directory of the deployment checkout, its compose files configure the worker
