#!/usr/bin/env bash
# =============================================================================
# Case study: deploy LUA (https://github.com/vaskers5/LUA) on a rented GPU
# and generate 2K/4K images with FLUX.1-dev + latent upscaling.
#
# LUA's footprint (FLUX.1-dev bf16 fully on-GPU, no offload):
#   ~33.7 GB weights + ~1 GB LUA (fp32) + activations  =>  ~40 GB peak at x4
#   => a 48 GB card (L40S / RTX 6000Ada / RTX A6000 / A40) is the sweet spot.
#   ~35 GB of HuggingFace downloads on first run       =>  80 GB disk, fast inet.
#
# Requirements: ~/.vast_api_key, an HF token with FLUX.1-dev access, LUA repo.
# Total cost of the full session is typically $0.30-0.80.
# =============================================================================
set -euo pipefail

LUA_DIR="${LUA_DIR:-$HOME/work/research/LUA}"
HF_TOKEN="${HF_TOKEN:?export HF_TOKEN=... (needs access to black-forest-labs/FLUX.1-dev)}"
OUT_DIR="${OUT_DIR:-./lua_outputs}"

# ---- 1. Rent: 48GB-class GPUs, ranked by effective session cost -------------
# (--json gives us the instance id for scripting)
INSTANCE=$(vastkit rent \
  --gpu "L40S" --gpu "RTX 6000Ada" --gpu "RTX A6000" --gpu "A40" \
  --vram 45 --disk 80 --max-price 1.0 --inet-down 500 --cuda 12.4 \
  --image pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel \
  --label lua-flux --hours 1 --download-gb 40 \
  --onstart 'apt-get update -qq && apt-get install -y -qq rsync || true' \
  --yes --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "rented instance: $INSTANCE"
trap 'echo "cleaning up"; vastkit destroy "$INSTANCE" --yes' EXIT

# ---- 2. Ship the LUA repo ---------------------------------------------------
vastkit push "$INSTANCE" "$LUA_DIR" /workspace/LUA

# ---- 3. Install python deps (torch ships with the image) --------------------
vastkit exec "$INSTANCE" --cwd /workspace/LUA -- \
  pip install -q -r requirements.txt hf_transfer

# ---- 4. Generate, detached (survives SSH drops; ~35GB HF download first) ----
vastkit exec "$INSTANCE" --detach --job lua --cwd /workspace/LUA \
  --env HF_TOKEN="$HF_TOKEN" \
  --env HF_HOME=/workspace/hf \
  --env HF_HUB_ENABLE_HF_TRANSFER=1 -- \
  'python inference.py --prompt "a mountain landscape, cinematic" --head x2 --seed 1 --output outputs/landscape_2k.png && \
   python inference.py --prompt "portrait of an astronaut, studio light" --head x4 --seed 2 --output outputs/astronaut_4k.png'

# ---- 5. Follow the log until done (exit code = job exit code) ---------------
vastkit logs "$INSTANCE" --job lua -f

# ---- 6. Pull the images and stop the meter (trap destroys the instance) -----
vastkit pull "$INSTANCE" /workspace/LUA/outputs/ "$OUT_DIR/"
echo "images in $OUT_DIR"
