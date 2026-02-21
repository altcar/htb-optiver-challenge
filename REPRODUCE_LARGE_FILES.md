# Large Files (>100MB) and Reproduction Notes

This repo contains a few very large files (>= 100MB). Some are reproducible artifacts (e.g., pip downloads), while others are model weights that cannot be deterministically regenerated from this repo alone.

## Detected files (>= 100MB)

(This list intentionally excludes `.git/` internals; those are just Git storage for tracked files and not useful as standalone artifacts.)

Path | Size | Notes | Reproducible?
---|---:|---|---
`pregame/your_optiver_workspace/pip-unpack-0y3z6z6u/torch-2.9.0-cp310-cp310-manylinux_2_28_x86_64.whl` | 858.1 MiB | PyTorch wheel downloaded/unpacked by pip | Yes (download)
`pregame/your_optiver_workspace/tmpmve6xrzc` | 446.2 MiB | Looks like an incomplete/temporary ZIP stream containing `nvidia/cudnn/...` files; not a valid ZIP (missing central directory) | Not exactly (ephemeral temp)
`pregame/your_optiver_workspace/model-export/model/pytorch_model.bin` | 255.5 MiB | HuggingFace Transformers model weights (DistilBERT seq-class head) | Not exactly (training not included)
`endgame/arbStrat2sentiment/model-export/model/pytorch_model.bin` | 255.5 MiB | Same blob as the above (duplicate) | Not exactly (training not included)
`endgame/arbStrat3/model-export/model/pytorch_model.bin` | 255.5 MiB | Same blob as the above (duplicate) | Not exactly (training not included)

Notes:
- The three `pytorch_model.bin` files are the exact same content (they point to the same git blob).

## How to reproduce the reproducible ones

### 1) PyTorch wheel (`torch-2.9.0-cp310-cp310-manylinux_2_28_x86_64.whl`)

This file is reproducible by downloading the wheel from PyPI using pip.

To fetch *that exact wheel build* regardless of your current OS, use pip’s cross-platform download flags:

```bash
python -m pip download --only-binary=:all: --no-deps \
  --platform manylinux_2_28_x86_64 \
  --python-version 310 --implementation cp --abi cp310 \
  torch==2.9.0 \
  -d ./repro_wheels
```

You should then see a file named:

`./repro_wheels/torch-2.9.0-cp310-cp310-manylinux_2_28_x86_64.whl`

If you specifically want it under the same repo path (not recommended, since `pip-unpack-*` is a temp folder), copy it:

```bash
mkdir -p pregame/your_optiver_workspace/pip-unpack-0y3z6z6u
cp ./repro_wheels/torch-2.9.0-cp310-cp310-manylinux_2_28_x86_64.whl \
  pregame/your_optiver_workspace/pip-unpack-0y3z6z6u/
```

### 2) `pregame/your_optiver_workspace/tmpmve6xrzc` (pip temp / partial download)

This file starts with ZIP local headers (`PK\x03\x04`) and contains paths like:

- `nvidia/cudnn/include/cudnn.h`
- `nvidia/cudnn/lib/libcudnn.so.9`

But it is **not a valid ZIP archive** (no end-of-central-directory record), which strongly suggests it is a temporary / interrupted pip download.

Because the filename is random and the content is likely incomplete, it is **not meaningfully reproducible as an identical file**.

Is it auto-created by `pip install`?

- Pip commonly creates random temp files during installs/downloads/unpacks.
- Usually these live under your OS temp dir, but depending on environment variables and how pip was invoked, they can end up in the working directory.
- If the install is interrupted, you can be left with large partial temp files like this.

Functional replacement (recommended): re-install the relevant dependencies and let pip re-download what it needs.

Example (clean environment):

```bash
python -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -r pregame/your_optiver_workspace/requirements.txt
./.venv/bin/python -m pip install torch
```

On Linux, installing CUDA-enabled torch may also pull NVIDIA wheels (which contain `nvidia/cudnn/...`).

## About `pytorch_model.bin` (model-export weights)

The repo includes local HuggingFace model directories:

- `pregame/your_optiver_workspace/model-export/model/`
- `endgame/arbStrat2sentiment/model-export/model/`
- `endgame/arbStrat3/model-export/model/`

Each contains a `config.json` + tokenizer files + `pytorch_model.bin`.

These weights *look like* a trained DistilBERT model used for inference (see `endgame/arbStrat3/sentiment.py` and `endgame/arbStrat2sentiment/sentiment.py`).

### Reproducibility status

This repo does **not** include the training script / hyperparameters used to create these exact weights, so the `pytorch_model.bin` files are **not reproducible byte-for-byte** from the current codebase.

If your goal is a functional reproduction, you can retrain a DistilBERT regression model on `pregame/your_optiver_workspace/training.csv` and then export with `save_pretrained(...)`, but the resulting weights will generally differ unless you also match the original training setup exactly.

Minimal sketch of what the export step would look like:

```python
from transformers import DistilBertForSequenceClassification, DistilBertTokenizerFast

model = DistilBertForSequenceClassification.from_pretrained(
    "distilbert-base-uncased",
    num_labels=5,
    problem_type="regression",
)
tokenizer = DistilBertTokenizerFast.from_pretrained("distilbert-base-uncased")

# ...train model on (text, 5-dim target) pairs...

model.save_pretrained("model-export/model")
tokenizer.save_pretrained("model-export/model")
```

If you want, I can add a concrete training script to the repo that consumes `training.csv` and regenerates `model-export/model/` (functional reproduction; not guaranteed identical weights).

## Where HuggingFace models are downloaded in this repo

These code paths call `transformers` APIs that download models/tokenizers from HuggingFace Hub (unless already cached):

- `endgame/arbStrat3/controllerFixReconnect.py` uses `pipeline(..., model="distilbert-base-uncased-finetuned-sst-2-english", tokenizer=...)`
- `endgame/arbStrat3/controllerEdge.py` uses `pipeline(..., model="distilbert-base-uncased-finetuned-sst-2-english", tokenizer=...)`
- `endgame/arbStrat3/controllerEdge.py` also downloads an embeddings model via `SentenceTransformer("all-MiniLM-L6-v2")`
- `endgame/hugoarbones/research.py` uses `MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"` (used with `pipeline`)
- `endgame/hugoarbones/market_maker.py` uses `AutoTokenizer.from_pretrained("nlptown/bert-base-multilingual-uncased-sentiment")` and `AutoModelForSequenceClassification.from_pretrained(...)`
- `endgame/hi/usingtransformer.py` loads whatever `model_name` you pass via `pipeline(..., model=model_name, ...)` and/or `AutoTokenizer.from_pretrained(model_name)` + `AutoModelForCausalLM.from_pretrained(model_name)`
- `pregame/your_optiver_workspace/Classifier Introduction.ipynb` downloads models in notebook cells, including:
  - `Qwen/Qwen2.5-0.5B-Instruct`
  - `typeform/distilbert-base-uncased-mnli`

To pre-download a specific model (example: DistilBERT SST-2):

```bash
python - <<'PY'
from transformers import pipeline
pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")
print("downloaded")
PY
```
