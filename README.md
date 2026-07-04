# vastkit

**Search, rent and drive GPU servers on [Vast.ai](https://vast.ai) — from your terminal, with zero dependencies.**

[![CI](https://github.com/RazinAleksandr/vastkit/actions/workflows/ci.yml/badge.svg)](https://github.com/RazinAleksandr/vastkit/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

`vastkit` is a single-purpose toolkit for the everyday research workflow: *find the cheapest GPU that actually fits my job, get a shell on it in one command, ship my code, run it detached, pull the results, kill the meter.* It is pure Python standard library — `pip install` it anywhere (laptop, CI, a bastion VPS) with no virtualenv ceremony.

```console
$ vastkit rent --gpu L40S --gpu "RTX 6000Ada" --vram 45 --disk 80 --max-price 1.0 --yes
[1/5] renting offer 1234567: L40S 48GB @ $0.640/hr (Sweden, SE)
  instance 8901234 created; waiting for boot ...
  [64s] status=running
  ready: ssh -p 41234 root@185.x.x.x ($0.640/hr)

instance 8901234 is ready
  gpu:    L40S x1
  price:  $0.640/hr
  ssh:    ssh -p 41234 -i ~/.ssh/id_ed25519 root@185.x.x.x

billing runs until you destroy it:  vastkit destroy 8901234
```

## Why not the official CLI?

The official `vastai` CLI is a fine low-level API mirror. `vastkit` is opinionated tooling for actually *working* on rented machines:

| | `vastkit` |
|---|---|
| **Zero dependencies** | Pure stdlib. No `requests`, no packaging conflicts, installs in seconds anywhere Python 3.9+ exists. |
| **Honest cost ranking** | Sorts by *effective session cost* = rent + bandwidth. A $0.40/hr host charging $15/TB loses to a $0.55/hr host with free traffic when your job starts by downloading 50 GB of weights. |
| **Rent that survives reality** | Listed offers are often already gone, and some machines accept your money but never boot. `vastkit rent` walks the ranked offers, bounds each boot attempt, destroys duds, and hands you a verified SSH endpoint. |
| **SSH keys handled** | Auto-detects your keypair and attaches the public key to the instance via API — no dashboard clicking. |
| **Jobs that survive disconnects** | `exec --detach` runs under `nohup` with remote logs and exit codes; `logs -f` re-attaches from anywhere and exits with the job's code. |
| **Transfers that always work** | `push`/`pull` use rsync and silently fall back to tar-over-ssh when the image lacks rsync. |
| **Money guardrails** | `--max-price` caps searches, renting asks for confirmation, `ls`/`destroy` show accrued cost, `whoami` shows your balance. |

## Install

```bash
pip install git+https://github.com/RazinAleksandr/vastkit.git
# or, for development
git clone https://github.com/RazinAleksandr/vastkit && cd vastkit && pip install -e .
# or run it with no install at all
python -m vastkit --help
```

## Setup (one time)

1. **API key** — from [cloud.vast.ai/cli](https://cloud.vast.ai/cli/):

   ```bash
   echo 'YOUR_KEY' > ~/.vast_api_key && chmod 600 ~/.vast_api_key
   # or: export VAST_API_KEY=...
   ```

2. **SSH key** — any keypair in `~/.ssh` works; `vastkit` auto-detects it (`id_ed25519`/`id_rsa` first, else the newest pair) and attaches the public key to every instance it rents. Override with `--ssh-key` or `$VASTKIT_SSH_KEY`.

3. Verify:

   ```bash
   vastkit whoami        # prints account + credit balance
   ```

## Quickstart: a complete session

```bash
# 1. Look at the market (est. session = 2h rent + 40GB of model downloads)
vastkit search --gpu L40S --vram 45 --disk 80 --max-price 1.0 --hours 2 --download-gb 40

# 2. Rent the best offer and wait for SSH (auto-retries dead offers)
vastkit rent --gpu L40S --vram 45 --disk 80 --max-price 1.0 --label myexp --yes

# 3. Ship your project
vastkit push 8901234 ./my-project /workspace/my-project

# 4. Install deps, run training detached (survives SSH drops / laptop sleep)
vastkit exec 8901234 --cwd /workspace/my-project -- pip install -r requirements.txt
vastkit exec 8901234 --detach --job train --cwd /workspace/my-project \
    --env WANDB_MODE=offline -- python train.py --epochs 10

# 5. Watch it (Ctrl-C is safe; the job keeps running)
vastkit logs 8901234 --job train -f

# 6. Grab results and stop the meter
vastkit pull 8901234 /workspace/my-project/checkpoints ./checkpoints
vastkit destroy 8901234 --yes
```

## Commands

| Command | What it does |
|---|---|
| `search` | Query + rank offers. Filters: `--gpu` (repeatable), `--vram`, `--disk`, `--max-price`, `--max-bw $/TB`, `--inet-down`, `--reliability`, `--cuda`, `--geo EU\|SE,NO`, raw `--filter k=op:v`. |
| `rent` | Search (or `--offer ID`), then create → attach SSH key → wait until SSH answers. `--image`, `--disk`, `--label`, `--env K=V`, `--onstart`/`--onstart-file`, `--attempts`, `--boot-timeout`. |
| `ls` | Instances with status, $/hr, age, accrued cost. |
| `status [ID]` | Details + live SSH reachability (exit code reflects readiness). |
| `wait [ID]` | Block until running + SSH-reachable. |
| `ssh [ID] [-- CMD]` | Interactive shell, or one-off command. |
| `exec ID -- CMD` | Run a command (`--env`, `--cwd`). `--detach --job NAME` for long jobs. Everything after `--` is passed verbatim. |
| `jobs ID` / `logs ID [-f]` | List detached jobs / tail or follow a job's log. |
| `push ID LOCAL REMOTE` | Upload (rsync → tar fallback). `--exclude`, `--delete`. |
| `pull ID REMOTE LOCAL` | Download results. |
| `stop ID` / `start ID` | Hibernate (GPU freed, storage still billed) / resume. |
| `destroy [IDs] [--all] [--label X]` | Terminate and stop billing; prints what each instance cost. |
| `whoami` | Validate the API key, show credit balance. |

Every command takes `--json` for scripting, `--api-key`, and `--ssh-key`. Instance id can be omitted when you have exactly one instance.

## How ranking works

```
effective session cost = dph_total × --hours  +  --download-gb × (internet_down_cost_per_tb / 1000)
```

`--sort` modes: `effective` (default, cheapest real session), `price` (raw $/hr), `speed` (highest DLPerf), `value` (DLPerf per session dollar). The search table shows both the sticker price and the session estimate so surprises show up *before* you rent. For a **hard** cutoff instead of a soft penalty, `--max-bw 10` drops every host charging more than $10/TB for downloads before ranking — some charge $10–20/TB, which no hourly discount can offset on weight-heavy jobs. Only `verified: true`, `rentable: true`, on-demand offers are considered; anything else is available through `--filter`, e.g. `--filter datacenter=eq:true --filter gpu_arch=eq:ada`.

## GPU name cheat-sheet

Vast names GPUs with spaces exactly as the console shows them: `RTX 4090`, `RTX 5090`, `RTX A6000`, `RTX 6000Ada`, `L40S`, `A40`, `A100 SXM4`, `A100 PCIE`, `H100 SXM5`, `H200`. Pass several `--gpu` flags to search across models and let ranking pick.

## Python API

Everything the CLI does is a small library call away:

```python
from vastkit import VastAPI, RentSpec, build_query, rent
from vastkit.ranking import rank_offers
from vastkit import remote

api = VastAPI()  # key from $VAST_API_KEY or ~/.vast_api_key

offers = rank_offers(
    api.search_offers(build_query(gpus=["L40S"], min_vram_gb=45, max_dph=1.0)),
    sort="effective", hours=2, download_gb=40,
)
inst = rent(api, offers, RentSpec(disk=80, label="myexp"))

remote.push(inst.ssh_host, inst.ssh_port, "./my-project", "/workspace/my-project")
remote.start_detached(inst.ssh_host, inst.ssh_port,
                      "python train.py", job="train", cwd="/workspace/my-project")
# ... poll remote.poll_detached / remote.read_log ...
remote.pull(inst.ssh_host, inst.ssh_port, "/workspace/my-project/out", "./out")
api.destroy_instance(inst.id)
```

## Case study

[`examples/deploy_lua_flux.sh`](examples/deploy_lua_flux.sh) reproduces a real research deployment end-to-end: renting a 48 GB GPU, deploying [LUA](https://github.com/vaskers5/LUA) (FLUX.1-dev + latent upscaler), generating 2K/4K images, and pulling them back — for well under a dollar.

## Design notes

- **Throttled + retried**: the Vast API rate-limits hard; all calls keep a 2 s spacing and retry 429/5xx with backoff.
- **Stateless**: the API is the single source of truth — no local registry files to drift. Anything you rent is visible to `ls` from any machine with your key.
- **Safe by default**: renting and destroying prompt on a TTY and *require* `--yes` when scripted; searches are capped at `--max-price` (default $2/hr).
- **Boring transport**: plain `ssh`/`rsync`/`tar` subprocesses with keepalives — the same commands you'd type, debuggable with your eyes.

## License

MIT © Aleksandr Razin
